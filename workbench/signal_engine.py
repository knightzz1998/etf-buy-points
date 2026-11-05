#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""博主波段法信号引擎

口径与仓库既有实现保持一致：
  - 周线 KDJ(9,3,3)：按周高/周低/周收盘计算，周线与 pandas resample("W-FRI") 对齐
  - 5/17 日线：简单移动平均
  - 第一买点：周线 J 近 6 周谷底 <= 20 且本周 J 回勾 + 现价 > MA17 + MA5 > MA17
  - 四档信号：空头离场 / 修复期观望 / 多头内按 J>=80、三条件、破 5 日线区分买点、加仓、减仓

本模块只做纯计算，不取数、不落盘。日线传入的历史不含当日，当日价格由实时快照补入，
因此盘中得到的是动态均线与动态周线 J。
"""
from __future__ import annotations

import numpy as np

J_TROUGH = 20.0      # 近 6 周 J 谷底阈值
J_OVERBOUGHT = 80.0  # 周线 J 超买阈值
MIN_BARS = 130       # 周线 KDJ 至少需要的日线条数
NEAR_MA17_PCT = 3.0  # 距 MA17 在此范围内且条件接近时列入“接近买点”

# 信号编码：前端按此配色
SIGNAL_BUY = "buy"        # 🟢 可以买入
SIGNAL_ADD = "add"        # 🔵 可以加仓
SIGNAL_TRIM = "trim"      # 🟡 可以减仓
SIGNAL_EXIT = "exit"      # 🔴 该离场
SIGNAL_WAIT = "wait"      # ⚪ 观望（修复期/数据不足）

SIGNAL_TEXT = {
    SIGNAL_BUY: "可以买入",
    SIGNAL_ADD: "可以加仓",
    SIGNAL_TRIM: "可以减仓",
    SIGNAL_EXIT: "该离场",
    SIGNAL_WAIT: "观望",
}


def _to_week_start(dates: np.ndarray) -> np.ndarray:
    """把交易日映射到所在周的周一（datetime64[D]）。周末无交易，等价于 pandas W-FRI 分组。"""
    d = dates.astype("datetime64[D]")
    days = d.astype(np.int64)
    weekday = (days + 3) % 7  # 1970-01-01 为周四，Monday=0
    return (days - weekday).astype("datetime64[D]")


def weekly_ohlc(dates: np.ndarray, o: np.ndarray, h: np.ndarray,
                l: np.ndarray, c: np.ndarray) -> dict:
    """按周聚合日线，返回周线数组（含未走完的当前周）。"""
    if len(dates) == 0:
        empty = np.empty(0)
        return {"start": np.empty(0, dtype="datetime64[D]"), "o": empty,
                "h": empty, "l": empty, "c": empty}
    starts = _to_week_start(dates)
    # 周起点单调不减，只需找出每个新周的第一个位置
    change = np.flatnonzero(np.concatenate(([True], starts[1:] != starts[:-1])))
    w_start = starts[change]
    w_o = o[change]
    w_c = c[np.append(change[1:], len(c)) - 1]
    w_h = np.maximum.reduceat(h, change)
    w_l = np.minimum.reduceat(l, change)
    return {"start": w_start, "o": w_o, "h": w_h, "l": w_l, "c": w_c}


def kdj_weekly(w_h: np.ndarray, w_l: np.ndarray, w_c: np.ndarray) -> tuple:
    """周线 KDJ(9,3,3)，返回 (K, D, J)。窗口不足的位置为 nan。"""
    n = len(w_c)
    if n == 0:
        return np.empty(0), np.empty(0), np.empty(0)
    k = np.full(n, np.nan)
    d = np.full(n, np.nan)
    j = np.full(n, np.nan)
    alpha = 1.0 / 3.0
    prev_k, prev_d = 50.0, 50.0
    for i in range(n):
        lo = max(0, i - 8)  # 9 周窗口
        hh = w_h[lo:i + 1].max()
        ll = w_l[lo:i + 1].min()
        span = hh - ll
        rsv = 50.0 if span <= 0 else (w_c[i] - ll) / span * 100.0
        if i == 0:
            cur_k, cur_d = rsv, rsv
        else:
            cur_k = alpha * rsv + (1 - alpha) * prev_k
            cur_d = alpha * cur_k + (1 - alpha) * prev_d
        k[i], d[i], j[i] = cur_k, cur_d, 3 * cur_k - 2 * cur_d
        prev_k, prev_d = cur_k, cur_d
    return k, d, j


def _apply_quote_to_week(weekly: dict, quote_date: np.datetime64,
                         tx_high: float, tx_low: float, price: float) -> dict:
    """把当日实时价并入当前周线（未走完的一周），返回新的周线数组，不修改入参。"""
    if len(weekly["start"]) == 0:
        return {"start": np.array([quote_date], dtype="datetime64[D]"),
                "o": np.array([price]), "h": np.array([tx_high]),
                "l": np.array([tx_low]), "c": np.array([price])}
    week = _to_week_start(np.array([quote_date], dtype="datetime64[D]"))[0]
    last_week = weekly["start"][-1]
    o = weekly["o"].copy()
    h = weekly["h"].copy()
    l = weekly["l"].copy()
    c = weekly["c"].copy()
    if week == last_week:
        h[-1] = max(h[-1], tx_high, price)
        l[-1] = min(l[-1], tx_low, price)
        c[-1] = price
        start = weekly["start"]
    elif week > last_week:
        start = np.append(weekly["start"], week)
        o = np.append(o, price if price > 0 else float(c[-1]))
        h = np.append(h, max(tx_high, price))
        l = np.append(l, min(tx_low, price) if tx_low > 0 else price)
        c = np.append(c, price)
    else:  # 行情日期早于缓存（异常数据），不并入
        start = weekly["start"]
    return {"start": start, "o": o, "h": h, "l": l, "c": c}


def evaluate(daily: dict, weekly: dict, quote: dict) -> dict:
    """计算单只 ETF 的全部指标与信号。

    daily  : {"dates","o","h","l","c","v"}，历史日线，不含当日
    weekly : weekly_ohlc 的结果，基于同一份历史日线
    quote  : {"price","pct","amount","high","low","open","prev","qdate","name","code"}
    """
    c = daily["c"]
    n = len(c)
    price = float(quote.get("price") or 0)
    qdate = np.datetime64(str(quote.get("qdate") or "1970-01-01")[:10], "D")

    base = {
        "code": quote.get("code", ""),
        "name": quote.get("name", ""),
        "price": price,
        "pct": quote.get("pct", 0.0),
        "amount": quote.get("amount", 0.0),
        "turnover": quote.get("turnover", 0.0),
        "liangbi": quote.get("liangbi", 0.0),
        "main_inflow": quote.get("main_inflow", 0.0),
        "premium": quote.get("premium"),  # 溢价率%，来自东财折价率取负
        "asset": quote.get("asset", "其他"),
        "bars": n,
        "spark": [],
    }
    if n + 1 < MIN_BARS or price <= 0:  # 传入的是不含当日的历史，n+1 为含当日的新旧判断口径
        base.update({
            "signal": SIGNAL_WAIT, "signal_text": "观望", "near_buy": False,
            "reason": f"数据不足({n}<{MIN_BARS}日)" if price > 0 else "无有效报价",
            "cond": {"j_trough": False, "j_up": False, "above_ma17": False, "ma5_above_ma17": False},
            "met": 0, "score": 0.0, "risk": None,
        })
        base["spark"] = [round(float(x), 3) for x in c[-30:]]
        return base

    close_eff = np.append(c, price)
    ma5 = float(close_eff[-5:].mean())
    ma17 = float(close_eff[-17:].mean())
    ma60 = float(close_eff[-60:].mean()) if n >= 59 else float("nan")

    # 当日并入周线后重算周线 KDJ（只影响最后一周）
    w = _apply_quote_to_week(weekly, qdate,
                             float(quote.get("high") or price),
                             float(quote.get("low") or price), price)
    k_arr, d_arr, j_arr = kdj_weekly(w["h"], w["l"], w["c"])
    j_last = float(j_arr[-1])
    j_prev = float(j_arr[-2]) if len(j_arr) >= 2 else j_last
    j_win = j_arr[-6:]
    j_min6 = float(np.nanmin(j_win)) if len(j_win) else j_last

    # 三项条件（第 4 项为多头结构确认）
    c_trough = j_min6 <= J_TROUGH
    c_jup = j_last > j_prev
    c_above17 = price > ma17
    c_ma5 = ma5 > ma17
    met = int(c_trough) + int(c_jup) + int(c_above17) + int(c_ma5)
    buy_ready = c_trough and c_jup and c_above17 and c_ma5

    # 多头 / 修复期 / 空头
    if not c_above17:
        state = "空头"
    elif not c_ma5:
        state = "修复期"
    else:
        state = "多头"

    # 四档信号（与 daily_signal.classify 一致）
    if state == "空头":
        signal = SIGNAL_EXIT
        reason = "收盘价低于17日线，生命线失守，不参与"
    elif state == "修复期":
        signal = SIGNAL_WAIT
        reason = "站上17日线但5日线未金叉，等MA5上穿确认"
    elif j_last >= J_OVERBOUGHT:
        signal = SIGNAL_TRIM
        reason = "多头中周线J超买，按规则卖一半"
    elif buy_ready:
        signal = SIGNAL_BUY
        reason = "周线J凹底回勾 + 站上17日线 + 5日线在上，波段第一买点"
    elif price < ma5:
        signal = SIGNAL_TRIM
        reason = "多头中收盘跌破5日线，降仓位等回踩"
    else:
        signal = SIGNAL_ADD
        reason = "多头趋势中回踩不破、站稳5日线，加仓点"

    # 接近买点：只差一项条件，且方向可等
    near_buy = False
    near_reason = ""
    if not buy_ready:
        if c_trough and c_jup and c_above17 and not c_ma5 and ma5 >= ma17 * 0.99:
            near_buy, near_reason = True, "J已凹底回勾且站上17日线，只差5日线上穿"
        elif c_trough and c_jup and c_ma5 and not c_above17 and price >= ma17 * (1 - NEAR_MA17_PCT / 100):
            near_buy, near_reason = True, f"J已凹底回勾，距17日线 {abs(price / ma17 - 1) * 100:.1f}% 待收复"
        elif c_above17 and c_ma5 and c_trough and not c_jup:
            near_buy, near_reason = True, "趋势已多头，等周线J回勾"

    dist17 = (price / ma17 - 1) * 100
    dist5 = (price / ma5 - 1) * 100
    hit = int((daily["l"][-20:] <= ma17).sum()) if n >= 20 else 0

    # 综合分：贴近17日线（好入场）50 + 成交额 30 + J回勾 20，与 kdj17_scan 计分一致
    pos_score = max(0.0, min(1.0, (8 - abs(dist17)) / 8)) if abs(dist17) <= 8 else 0.0
    amount_score = 0.0  # 由上层按全市场成交额统一归一化
    score = pos_score * 50 + (20 if c_jup else 0) + amount_score

    base.update({
        "signal": signal,
        "signal_text": SIGNAL_TEXT[signal],
        "state": state,
        "near_buy": near_buy,
        "near_reason": near_reason,
        "reason": reason,
        "cond": {"j_trough": bool(c_trough), "j_up": bool(c_jup),
                 "above_ma17": bool(c_above17), "ma5_above_ma17": bool(c_ma5)},
        "met": met,
        "buy_ready": buy_ready,
        "ma5": round(ma5, 4),
        "ma17": round(ma17, 4),
        "ma60": round(ma60, 4) if ma60 == ma60 else None,
        "dist_ma17": round(dist17, 2),
        "dist_ma5": round(dist5, 2),
        "j_last": round(j_last, 1),
        "j_prev": round(j_prev, 1),
        "j_min6": round(j_min6, 1),
        "j_overbought": bool(j_last >= J_OVERBOUGHT),
        "pullback_hit": f"{hit}/20",
        "pos_score": round(pos_score, 4),
        "score": round(score, 1),
        "stop": round(ma17, 4),          # 止损位=17日线
        "spark": [round(float(x), 3) for x in close_eff[-30:]],
    })
    return base


def apply_amount_score(rows: list) -> None:
    """按成交额把综合分补满 30 分（全市场统一归一化）。"""
    amounts = [r.get("amount") or 0 for r in rows]
    top = max(amounts) if amounts else 0
    if top <= 0:
        return
    for r in rows:
        r["score"] = round((r.get("score") or 0) + (r.get("amount") or 0) / top * 30, 1)
