#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组合级回测：不同仓位/持仓数下的年化与最大回撤

策略：进攻档 + 博主买点，按距 MA20 降序取前 N；仓位 = 总资产 × p；
出场：收盘破 MA17 次日收盘、满 45 日、浮盈 ≥20%
用法：python3 workbench/backtest_portfolio.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_spec import build_pool, live_universe, read_hist, fetch_index, kdj_weekly_aligned  # noqa: E402
from backtest_v2 import weekly_series  # noqa: E402

COST = 0.00005  # 单边：万 0.5


def main() -> int:
    base = json.loads((Path(__file__).resolve().parent / "dist" / "data" / "base.json").read_text())
    pool = build_pool(base, live_universe())
    sh = fetch_index("000001", "1")
    cy = fetch_index("399006", "0")
    idx = sh[["date", "close"]].merge(cy[["date", "close"]], on="date", suffixes=("_sh", "_cy"))
    idx["m_sh"] = idx["close_sh"].rolling(20).mean()
    idx["m_cy"] = idx["close_cy"].rolling(20).mean()
    idx["tier"] = np.where((idx["close_sh"] >= idx["m_sh"]) & (idx["close_cy"] >= idx["m_cy"]), "进攻",
                          np.where((idx["close_sh"] >= idx["m_sh"]) | (idx["close_cy"] >= idx["m_cy"]), "中性", "防守"))
    tier_map = dict(zip(idx["date"].astype(str), idx["tier"]))
    calendar = list(idx["date"].astype(str))

    close, ma17, ma5, dates, sig = {}, {}, {}, {}, {}
    for p in pool:
        df = read_hist(p["code"])
        if df is None or len(df) < 300:
            continue
        d = df["date"].astype(str)
        c = df["close"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        m5 = pd.Series(c).rolling(5).mean().to_numpy()
        m17 = pd.Series(c).rolling(17).mean().to_numpy()
        m20 = pd.Series(c).rolling(20).mean().to_numpy()
        wk_j, w_idx = weekly_series(h, l, c, d)
        jw = kdj_weekly_aligned(h, l, c, df["date"])
        jw_prev = np.array([wk_j[w - 1] if w > 0 else wk_j[0] for w in w_idx])
        jmin6 = np.zeros(len(c))
        for w in range(len(wk_j)):
            lo = max(0, w - 5)
            jmin6[w_idx == w] = wk_j[lo:w + 1].min()
        s = (jmin6 <= 20) & (jw > jw_prev) & (c > m17) & (m5 > m17) & (jw < 80)
        close[p["code"]] = c
        ma17[p["code"]] = m17
        ma5[p["code"]] = m5
        dates[p["code"]] = {x: i for i, x in enumerate(d)}
        sig[p["code"]] = (s, (c - m20) / m20)

    def run(size_pct: float, slots: int):
        cash, pos = 1.0, {}
        equity_curve, exposure = [], []
        trades = 0
        for date in calendar:
            # 卖出
            for code in list(pos.keys()):
                i = dates[code].get(date)
                if i is None:
                    continue
                px = close[code][i]
                p = pos[code]
                trigger = p.get("exit_pending")
                if trigger:
                    cash += p["shares"] * px * (1 - COST)
                    del pos[code]
                    trades += 1
                    continue
                if px < ma17[code][i] or i - p["entry_idx"] >= 45 or px / p["entry"] - 1 >= 0.20:
                    p["exit_pending"] = True
            # 买入
            if tier_map.get(date) == "进攻":
                cands = []
                for code, (s, dist) in sig.items():
                    i = dates[code].get(date)
                    if i is None or i + 1 >= len(close[code]):
                        continue
                    if s[i] and code not in pos:
                        cands.append((dist[i], code))
                cands.sort(reverse=True)
                for dist, code in cands:
                    if len(pos) >= slots:
                        break
                    i = dates[code].get(date)
                    px = close[code][i + 1]
                    budget = size_pct
                    if cash < budget * 0.99 or px <= 0:
                        continue
                    shares = budget / px
                    cash -= budget * (1 + COST)
                    pos[code] = {"shares": shares, "entry": px, "entry_idx": i + 1}
            value = cash + sum(p["shares"] * close[c][dates[c][date]]
                               for c, p in pos.items() if date in dates[c])
            equity_curve.append((date, value))
            if value > 0:
                exposure.append((value - cash) / value)
        eq = pd.Series([v for _, v in equity_curve], index=[d for d, _ in equity_curve])
        years = max(0.5, (pd.Timestamp(eq.index[-1]) - pd.Timestamp(eq.index[0])).days / 365)
        cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
        dd = (eq / eq.cummax() - 1).min()
        return {
            "仓位/只": f"{int(size_pct*100)}%",
            "持仓数": slots,
            "总收益%": round((eq.iloc[-1] / eq.iloc[0] - 1) * 100, 1),
            "年化%": round(cagr * 100, 1),
            "最大回撤%": round(dd * 100, 1),
            "交易笔数": trades,
            "平均仓位%": round(float(np.mean(exposure)) * 100, 0) if exposure else 0,
        }

    rows = []
    for size, slots in ((0.167, 6), (0.125, 8), (0.10, 10), (0.25, 4), (0.50, 2)):
        rows.append(run(size, slots))
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
