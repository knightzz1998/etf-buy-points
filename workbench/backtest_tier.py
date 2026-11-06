#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分档位买入的回测对比：在哪些档位开新仓，长期期望如何

同一套入场（博主买点）、同一套出场（破 MA17 次日 / 45 日 / +20%）、
同样 6 格 × 16.7% 的仓位，只改「允许开仓的档位」。
用法：python3 workbench/backtest_tier.py
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

COST = 0.00025
SIZE = 1 / 6
SLOTS = 6


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

    close, ma17, dates, sig = {}, {}, {}, {}
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
        dates[p["code"]] = {x: i for i, x in enumerate(d)}
        sig[p["code"]] = (s, (c - m20) / m20)

    def run(allowed: set, label: str):
        cash, pos = 1.0, {}
        curve, rets, dates_out = [], [], []
        for date in calendar:
            for code in list(pos.keys()):
                i = dates[code].get(date)
                if i is None:
                    continue
                px = close[code][i]
                p = pos[code]
                if p.get("exit_pending"):
                    cash += p["shares"] * px * (1 - COST)
                    rets.append((px / p["entry"] - 1) * 100 - 0.05)
                    del pos[code]
                    continue
                if px < ma17[code][i] or i - p["entry_idx"] >= 45 or px / p["entry"] - 1 >= 0.20:
                    p["exit_pending"] = True
            if tier_map.get(date) in allowed:
                cands = []
                for code, (s, dist) in sig.items():
                    i = dates[code].get(date)
                    if i is None or i + 1 >= len(close[code]) or code in pos:
                        continue
                    if s[i]:
                        cands.append((dist[i], code))
                cands.sort(reverse=True)
                for dist, code in cands:
                    if len(pos) >= SLOTS:
                        break
                    i = dates[code].get(date)
                    px = close[code][i + 1]
                    if cash < SIZE * 0.99 or px <= 0:
                        continue
                    cash -= SIZE * (1 + COST)
                    pos[code] = {"shares": SIZE / px, "entry": px, "entry_idx": i + 1}
            value = cash + sum(p["shares"] * close[c][dates[c][date]] for c, p in pos.items() if date in dates[c])
            curve.append((date, value))
        eq = pd.Series([v for _, v in curve], index=[d for d, _ in curve])
        years = max(0.5, (pd.Timestamp(eq.index[-1]) - pd.Timestamp(eq.index[0])).days / 365)
        r = np.array(rets)
        win = r[r > 0].sum(); loss = abs(r[r <= 0].sum())
        return {
            "开仓档位": label, "笔数": len(r),
            "胜率%": round((r > 0).mean() * 100, 1) if len(r) else 0,
            "均收益%": round(r.mean(), 2) if len(r) else 0,
            "盈亏比": round(win / loss, 2) if loss else None,
            "年化%": round(((eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1) * 100, 1),
            "最大回撤%": round((eq / eq.cummax() - 1).min() * 100, 1),
            "_eq": eq,
        }

    rows = [
        run({"进攻"}, "只做进攻档"),
        run({"进攻", "中性"}, "进攻 + 中性"),
        run({"进攻", "中性", "防守"}, "三档都做"),
        run({"防守"}, "只做防守档"),
        run({"中性"}, "只做中性档"),
    ]
    print(pd.DataFrame([{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]).to_string(index=False))
    eq = rows[0]["_eq"]
    yearly = eq.groupby(eq.index.str[:4]).agg(["first", "last"])
    yearly["年收益%"] = ((yearly["last"] / yearly["first"] - 1) * 100).round(1)
    print("\n只做进攻档 · 分年度：")
    print(yearly[["年收益%"]].to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
