#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入场时点回测：盘中触及 MA17 买 vs 尾盘确认买 vs 次日开盘买

事件：收盘价上穿 MA17（前一日收盘在下方，当日收在上方）
  ① 盘中到线买：仅当当日最低价触及 MA17 时有效，成交价 = MA17
  ② 尾盘买入：当日收盘成交
  ③ 次日开盘买：次日开盘价成交
  ④ 站稳两日再买：次日收盘仍在 MA17 上方才买，成交价 = 次日收盘

指标：5/10/20 日收益、胜率、5 日内最大不利偏移（MAE）、假突破率
用法：python3 workbench/backtest_entry.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_spec import build_pool, live_universe, read_hist, fetch_index  # noqa: E402

HORIZONS = (5, 10, 20)


def main() -> int:
    base = json.loads((Path(__file__).resolve().parent / "dist" / "data" / "base.json").read_text())
    pool = build_pool(base, live_universe())
    print(f"池 {len(pool)} 只")

    sh = fetch_index("000001", "1")
    cy = fetch_index("399006", "0")
    idx = sh[["date", "close"]].merge(cy[["date", "close"]], on="date", suffixes=("_sh", "_cy"))
    idx["m_sh"] = idx["close_sh"].rolling(20).mean()
    idx["m_cy"] = idx["close_cy"].rolling(20).mean()
    idx["tier"] = np.where((idx["close_sh"] >= idx["m_sh"]) & (idx["close_cy"] >= idx["m_cy"]), "进攻",
                          np.where((idx["close_sh"] >= idx["m_sh"]) | (idx["close_cy"] >= idx["m_cy"]), "中性", "防守"))
    tier_map = dict(zip(idx["date"].astype(str), idx["tier"]))

    rows = {k: [] for k in ("touch", "close", "nextopen", "confirm2")}
    false_break = 0
    breakout = 0
    touch_total = 0
    touch_success = 0

    for p in pool:
        df = read_hist(p["code"])
        if df is None or len(df) < 260:
            continue
        c = df["close"].to_numpy(float)
        o = df["open"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        ma17 = pd.Series(c).rolling(17).mean().to_numpy()
        n = len(c)
        for t in range(20, n - max(HORIZONS) - 2):
            prev_below = c[t - 1] <= ma17[t - 1]
            touched = l[t] <= ma17[t]
            closed_above = c[t] > ma17[t]
            if not (prev_below and touched):
                continue
            touch_total += 1
            if closed_above:
                touch_success += 1

            # ① 盘中到线买：触及 MA17 即成交（含当天最终没站上的失败单）
            entry = ma17[t]
            row = {"code": p["code"], "date": df["date"].iloc[t], "entry": entry,
                   "tier": tier_map.get(str(df["date"].iloc[t]), "防守"),
                   "mae5": (l[t:t + 6].min() / entry - 1) * 100}
            for hz in HORIZONS:
                row[f"r{hz}"] = (c[t + hz] / entry - 1) * 100
            rows["touch"].append(row)

            if not closed_above:
                continue

            # 以下都要求当日收盘站上 MA17
            breakout += 1
            if c[t + 1] < ma17[t + 1]:
                false_break += 1

            # ② 尾盘买
            entry = c[t]
            row = {"code": p["code"], "date": df["date"].iloc[t], "entry": entry,
                   "tier": tier_map.get(str(df["date"].iloc[t]), "防守"),
                   "mae5": (l[t:t + 6].min() / entry - 1) * 100}
            for hz in HORIZONS:
                row[f"r{hz}"] = (c[t + hz] / entry - 1) * 100
            rows["close"].append(row)

            # ③ 次日开盘买
            entry = o[t + 1]
            if entry > 0:
                row = {"code": p["code"], "date": df["date"].iloc[t], "entry": entry,
                       "tier": tier_map.get(str(df["date"].iloc[t]), "防守"),
                       "mae5": (l[t + 1:t + 7].min() / entry - 1) * 100}
                for hz in HORIZONS:
                    row[f"r{hz}"] = (c[t + 1 + hz] / entry - 1) * 100
                rows["nextopen"].append(row)

            # ④ 站稳两日：次日收盘仍在 MA17 上方
            if t + 1 < n and c[t + 1] > ma17[t + 1] and t + 1 + max(HORIZONS) < n:
                entry = c[t + 1]
                row = {"code": p["code"], "date": df["date"].iloc[t], "entry": entry,
                       "tier": tier_map.get(str(df["date"].iloc[t]), "防守"),
                       "mae5": (l[t + 1:t + 7].min() / entry - 1) * 100}
                for hz in HORIZONS:
                    row[f"r{hz}"] = (c[t + 1 + hz] / entry - 1) * 100
                rows["confirm2"].append(row)

    print(f"从下方触及 MA17 共 {touch_total} 次，其中当日收盘站上 {touch_success} 次"
          f"（{touch_success / max(1, touch_total) * 100:.1f}%）；站上后次日又跌破 {false_break} 次"
          f"（占站上 {false_break / max(1, breakout) * 100:.1f}%）")

    def table(dataset, title):
        print(f"\n=== {title} ===")
        out = []
        for key, label in (("touch", "① 盘中到线买（MA17 价）"),
                       ("close", "② 尾盘收盘买"),
                       ("nextopen", "③ 次日开盘买"),
                           ("confirm2", "④ 站稳两日再买")):
            d = pd.DataFrame(dataset[key])
            if not len(d):
                continue
            out.append({
                "方式": label,
                "样本": len(d),
                "5日均%": round(d.r5.mean(), 2),
                "5日胜率%": round((d.r5 > 0).mean() * 100, 1),
                "10日均%": round(d.r10.mean(), 2),
                "20日均%": round(d.r20.mean(), 2),
                "20日胜率%": round((d.r20 > 0).mean() * 100, 1),
                "5日MAE%": round(d.mae5.mean(), 2),
            })
        print(pd.DataFrame(out).to_string(index=False))

    table(rows, "全部档位")
    attack = {k: [r for r in v if r.get("tier") == "进攻"] for k, v in rows.items()}
    table(attack, "仅进攻档")
    return 0


if __name__ == "__main__":
    sys.exit(main())
