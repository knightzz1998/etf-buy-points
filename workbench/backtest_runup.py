#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""买点前的涨幅是否透支：按「距近 20/60 日低点的涨幅」分组，看后续 5/20 日收益

入场：博主买点 + 进攻档；t 日信号、t+1 收盘入场。
用法：python3 workbench/backtest_runup.py
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

    rows = []
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
        sig = (jmin6 <= 20) & (jw > jw_prev) & (c > m17) & (m5 > m17) & (jw < 80)
        for t in np.flatnonzero(sig[: len(c) - 22]):
            if tier_map.get(d.iloc[t]) != "进攻":
                continue
            entry = c[t + 1]
            if entry <= 0:
                continue
            low20 = c[max(0, t - 19):t + 1].min()
            low60 = c[max(0, t - 59):t + 1].min()
            rows.append({
                "run20": (c[t] / low20 - 1) * 100,
                "run60": (c[t] / low60 - 1) * 100,
                "dist20": (c[t] / m20[t] - 1) * 100,
                "r5": (c[t + 6] / entry - 1) * 100,
                "r20": (c[t + 21] / entry - 1) * 100,
            })

    df = pd.DataFrame(rows)
    print(f"进攻档信号 {len(df)} 个\n")

    for col, label in (("run20", "买点前距 20 日低点涨幅"), ("run60", "买点前距 60 日低点涨幅")):
        bins = [-100, 5, 10, 15, 20, 30, 100]
        names = ["<5%", "5-10%", "10-15%", "15-20%", "20-30%", ">30%"]
        df["bucket"] = pd.cut(df[col], bins=bins, labels=names)
        g = df.groupby("bucket", observed=True).agg(
            n=("r5", "size"), r5=("r5", "mean"), w5=("r5", lambda x: (x > 0).mean() * 100),
            r20=("r20", "mean"), w20=("r20", lambda x: (x > 0).mean() * 100))
        print(f"=== {label} ===")
        print(g.round(2).to_string())
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
