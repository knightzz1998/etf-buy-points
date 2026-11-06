#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同一批买点下，「破 MA17 止损」与「不止损死拿」的结果对比

入场：博主第一买点且处于进攻档（与 backtest_v2 一致），t 日信号、t+1 收盘入场
出场：
  A 破 MA17 次日收盘离场，最长 45 日
  B 不止损，固定持有 45 日
  C 不止损，固定持有 20 日
  D 不止损，只在浮盈 ≥20% 时卖，最长 90 日
用法：python3 workbench/backtest_stop.py
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

    trades = {"A_stop": [], "B_hold45": [], "C_hold20": [], "D_tp_only": []}
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
        wk_j, w_idx = weekly_series(h, l, c, dates)
        jw = kdj_weekly_aligned(h, l, c, df["date"])
        jw_prev = np.array([wk_j[w - 1] if w > 0 else wk_j[0] for w in w_idx])
        jw_min6 = np.zeros(n)
        for w in range(len(wk_j)):
            lo = max(0, w - 5)
            jw_min6[w_idx == w] = wk_j[lo:w + 1].min()
        sig = (jw_min6 <= 20) & (jw > jw_prev) & (c > ma17) & (ma5 > ma17) & (jw < 80)
        for t in np.flatnonzero(sig[: n - 100]):
            if tier_map.get(dates[t]) != "进攻":
                continue
            entry = c[t + 1]
            if entry <= 0:
                continue
            # A 止损
            ret, days = None, 0
            for k in range(t + 1, min(t + 46, n)):
                days = k - (t + 1)
                if c[k] < ma17[k]:
                    ret = c[k + 1] / entry - 1 if k + 1 < n else c[k] / entry - 1
                    break
            if ret is None:
                idx_end = min(t + 1 + 45, n - 1)
                ret = c[idx_end] / entry - 1
                days = idx_end - (t + 1)
            trades["A_stop"].append(ret * 100 - 0.05)

            end45 = min(t + 1 + 45, n - 1)
            trades["B_hold45"].append((c[end45] / entry - 1) * 100 - 0.05)
            end20 = min(t + 1 + 20, n - 1)
            trades["C_hold20"].append((c[end20] / entry - 1) * 100 - 0.05)

            ret_d, hit = None, False
            for k in range(t + 1, min(t + 91, n)):
                if c[k] / entry - 1 >= 0.20:
                    ret_d, hit = 20.0 - 0.05, True
                    break
            if ret_d is None:
                end90 = min(t + 1 + 90, n - 1)
                ret_d = (c[end90] / entry - 1) * 100 - 0.05
            trades["D_tp_only"].append(ret_d)

    rows = []
    for key, label in (("A_stop", "A 破 MA17 止损（现规则）"),
                       ("B_hold45", "B 不止损，拿满 45 日"),
                       ("C_hold20", "C 不止损，拿满 20 日"),
                       ("D_tp_only", "D 不止损，只等 +20%（最长 90 日）")):
        r = np.array(trades[key])
        if not len(r):
            continue
        win = r[r > 0].sum()
        loss = abs(r[r <= 0].sum())
        rows.append({
            "方式": label, "笔数": len(r),
            "胜率%": round((r > 0).mean() * 100, 1),
            "均收益%": round(r.mean(), 2),
            "中位%": round(float(np.median(r)), 2),
            "盈亏比": round(win / loss, 2) if loss else None,
            "最差%": round(r.min(), 1),
            "亏损>10%笔数": int((r < -10).sum()),
            "累计%": round(r.sum(), 1),
        })
    print(pd.DataFrame(rows).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
