#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略 v2 回测：博主法 × 档位 × 排序，对比规格书原版

交易口径：
  - t 日收盘出信号，t+1 日收盘入场
  - 出场：收盘跌破 MA17 → 次日收盘卖；持有满 45 个交易日；浮盈 ≥20%
  - 单边成本 0.025%（ETF 佣金+冲击近似），双边 0.05%

用法：python3 workbench/backtest_v2.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_spec import (  # noqa: E402
    build_pool, fetch_index, read_hist, live_universe, macd,
    kdj_daily, kdj_weekly_aligned, week_index, J_DAILY_CAP,
    DIST_UP, DIST_DEF,
)

MAX_HOLD = 45
TAKE_PROFIT = 20.0
COST = 0.05  # 双边 %


def weekly_series(h, l, c, dates):
    """返回 (日对齐周J, 各周J, 日→周映射)"""
    w_idx = week_index(dates)
    n_weeks = int(w_idx[-1]) + 1
    wk_h = np.zeros(n_weeks)
    wk_l = np.zeros(n_weeks)
    wk_c = np.zeros(n_weeks)
    for i in range(len(c)):
        w = w_idx[i]
        if i == 0 or w_idx[i] != w_idx[i - 1]:
            wk_h[w], wk_l[w], wk_c[w] = h[i], l[i], c[i]
        else:
            wk_h[w] = max(wk_h[w], h[i])
            wk_l[w] = min(wk_l[w], l[i])
            wk_c[w] = c[i]
    wk_j = np.zeros(n_weeks)
    pk = pd_ = 50.0
    for w in range(n_weeks):
        lo = max(0, w - 8)
        hh = wk_h[lo:w + 1].max()
        ll = wk_l[lo:w + 1].min()
        span = hh - ll
        rsv = 50.0 if span <= 0 else (wk_c[w] - ll) / span * 100
        ck = rsv if w == 0 else rsv / 3 + pk * 2 / 3
        cd = ck if w == 0 else ck / 3 + pd_ * 2 / 3
        wk_j[w] = 3 * ck - 2 * cd
        pk, pd_ = ck, cd
    return wk_j, w_idx


def simulate(signals, close_map, ma17_map, dates_map, calendar, max_pos=2, tp=None):
    """t 日收盘出信号 → t+1 收盘入场；收盘破 MA17 / 满 45 日 / 达止盈 → 次日收盘卖出。"""
    trades = []
    held = {}
    pending = set()
    by_date = {d: g.sort_values("score", ascending=False) for d, g in signals.groupby("date")}
    for date in calendar:
        # 1) 昨日触发、今日收盘执行卖出
        for code in list(pending):
            pos = held.pop(code, None)
            pending.discard(code)
            if not pos:
                continue
            i = dates_map[code].get(date)
            if i is None:
                held[code] = pos
                continue
            px = close_map[code][i]
            trades.append({"code": code, "entry_date": pos["entry_date"],
                           "days": i - pos["entry_idx"],
                           "ret": (px / pos["entry"] - 1) * 100 - COST,
                           "reason": pos["reason"]})
        # 2) 今日收盘判断是否触发卖出
        for code, pos in held.items():
            i = dates_map[code].get(date)
            if i is None:
                continue
            px = close_map[code][i]
            ret = px / pos["entry"] - 1
            if px < ma17_map[code][i]:
                pos["reason"] = "破MA17"; pending.add(code)
            elif i - pos["entry_idx"] >= MAX_HOLD:
                pos["reason"] = "到期"; pending.add(code)
            elif tp is not None and ret * 100 >= tp:
                pos["reason"] = "止盈"; pending.add(code)
        # 3) 今日收盘入场
        day_sig = by_date.get(date)
        if day_sig is None:
            continue
        for _, row in day_sig.iterrows():
            if len(held) + len(pending) >= max_pos:
                break
            code = row["code"]
            if code in held:
                continue
            i = dates_map[code].get(date)
            if i is None or i + 1 >= len(close_map[code]):
                continue
            held[code] = {"entry": close_map[code][i + 1], "entry_idx": i + 1,
                          "entry_date": date, "reason": ""}
    return pd.DataFrame(trades)


def stats(trades: pd.DataFrame, label: str) -> dict:
    if not len(trades):
        return {"策略": label, "笔数": 0}
    win = trades[trades.ret > 0]
    loss = trades[trades.ret <= 0]
    pf = (win.ret.sum() / abs(loss.ret.sum())) if len(loss) and loss.ret.sum() != 0 else np.nan
    return {
        "策略": label, "笔数": len(trades),
        "胜率%": round((trades.ret > 0).mean() * 100, 1),
        "均收益%": round(trades.ret.mean(), 2),
        "中位%": round(trades.ret.median(), 2),
        "盈亏比": round(pf, 2) if pf == pf else None,
        "平均持有日": round(trades.days.mean(), 1),
        "最差%": round(trades.ret.min(), 2),
        "总收益%": round(trades.ret.sum(), 1),
    }


def main() -> int:
    import json
    base_dict = json.loads((Path(__file__).resolve().parent / "dist" / "data" / "base.json").read_text())
    pool = build_pool(base_dict, live_universe())
    print(f"池 {len(pool)} 只")

    sh = fetch_index("000001", "1")
    cy = fetch_index("399006", "0")
    idx = sh[["date", "close"]].merge(cy[["date", "close"]], on="date", suffixes=("_sh", "_cy"))
    idx["ma20_sh"] = idx["close_sh"].rolling(20).mean()
    idx["ma20_cy"] = idx["close_cy"].rolling(20).mean()
    idx["tier"] = np.where(
        (idx["close_sh"] >= idx["ma20_sh"]) & (idx["close_cy"] >= idx["ma20_cy"]), "进攻",
        np.where((idx["close_sh"] >= idx["ma20_sh"]) | (idx["close_cy"] >= idx["ma20_cy"]), "中性", "防守"))
    tier_map = dict(zip(idx["date"], idx["tier"]))
    calendar = list(idx["date"])

    close_map, ma17_map, dates_map = {}, {}, {}
    rows = {k: [] for k in ("blogger_all", "blogger_gate", "blogger_atk",
                            "blogger_def", "spec_old", "spec_fixed")}

    for p in pool:
        df = read_hist(p["code"])
        if df is None or len(df) < 300:
            continue
        dates = df["date"].astype(str)
        c = df["close"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        n = len(c)
        ma5 = pd.Series(c).rolling(5).mean().to_numpy()
        ma17 = pd.Series(c).rolling(17).mean().to_numpy()
        ma20 = pd.Series(c).rolling(20).mean().to_numpy()
        ma20_up = np.zeros(n, bool)
        for i in range(3, n):
            ma20_up[i] = ma20[i] > ma20[i - 1] > ma20[i - 2] > ma20[i - 3]
        dif, dea = macd(c)
        jd = kdj_daily(h, l, c)
        wk_j, w_idx = weekly_series(h, l, c, dates)
        jw = kdj_weekly_aligned(h, l, c, df["date"])
        tier = np.array([tier_map.get(d, "防守") for d in dates])
        dist20 = (c / ma20 - 1) * 100

        jw_prev = np.array([wk_j[w - 1] if w > 0 else wk_j[0] for w in w_idx])
        jw_min6 = np.zeros(n)
        for w in range(len(wk_j)):
            lo = max(0, w - 5)
            jw_min6[w_idx == w] = wk_j[lo:w + 1].min()
        blogger = (jw_min6 <= 20) & (jw > jw_prev) & (c > ma17) & (ma5 > ma17) & (jw < 80)

        spec_five = ((c > ma17) & (ma5 > ma17) & ma20_up & (dif > dea) & (jd <= J_DAILY_CAP))
        spec_strict = ((c > ma17) & (ma5 > ma17) & ma20_up & (dif > dea) & (jd <= 90))
        weak = (p["group"] == "弱趋势")
        defensive = (p["group"] == "防御")

        # 规格书原版：进攻=三条件；中性=五条件+8%；防守=五条件+6%+防御组；弱趋势=严格
        spec_ok = np.where(
            tier == "进攻", (c > ma17) & (ma5 > ma17) & (dif > dea),
            np.where(tier == "中性", spec_five & (dist20 <= DIST_UP),
                     spec_strict & (dist20 <= DIST_DEF) & defensive))
        if weak:
            spec_ok = spec_strict & (dist20 <= DIST_DEF)
        spec_ok &= ~(jd > 100)

        def push(key, mask, score):
            for t in np.flatnonzero(mask[: n - 3]):
                rows[key].append({"date": dates[t], "code": p["code"], "score": float(score[t])})

        push("blogger_all", blogger, dist20)
        push("blogger_gate", blogger & (tier != "中性"), dist20)
        push("blogger_atk", blogger & (tier == "进攻"), dist20)
        push("blogger_def", blogger & (tier == "防守") & defensive & (dist20 <= DIST_DEF), dist20)
        push("spec_old", spec_ok, -dist20)
        push("spec_fixed", spec_ok & (tier == "进攻"), dist20)

        close_map[p["code"]] = c
        ma17_map[p["code"]] = ma17
        dates_map[p["code"]] = {d: i for i, d in enumerate(dates)}

    out = []
    for key, label in (("blogger_all", "博主法·全信号（距MA20降序，前2）"),
                       ("blogger_gate", "博主法+跳过中性档（前2）"),
                       ("blogger_atk", "博主法+仅进攻档（前2）"),
                       ("blogger_def", "博主法+防守档防御组低吸（前2）"),
                       ("spec_old", "规格书原版（距MA20升序，前2）"),
                       ("spec_fixed", "规格书修正（仅进攻档+降序，前2）")):
        sig = pd.DataFrame(rows[key])
        if not len(sig):
            out.append({"策略": label, "笔数": 0})
            continue
        tr = simulate(sig, close_map, ma17_map, dates_map, calendar)
        out.append(stats(tr, label + "｜无止盈"))
        tr2 = simulate(sig, close_map, ma17_map, dates_map, calendar, tp=20.0)
        out.append(stats(tr2, label + "｜+20%止盈"))

    print(pd.DataFrame(out).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
