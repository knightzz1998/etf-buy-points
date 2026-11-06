#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资金流过滤条件对比：3 日流入 vs 60 日流入 vs 短期反转

限制：东财资金流历史只有约 120 个交易日，样本窗口短，结论仅看方向。
入场：博主买点 + 进攻档；比较不同资金条件下的 5/20 日收益。
用法：python3 workbench/backtest_flow.py
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_spec import build_pool, live_universe, read_hist, fetch_index, kdj_weekly_aligned  # noqa: E402
from backtest_v2 import weekly_series  # noqa: E402

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
CACHE = Path(__file__).resolve().parent.parent / "data" / "flow_hist_cache"


def secid(code: str) -> str:
    return ("1." if code.startswith(("5", "6", "9")) else "0.") + code


def fetch_flow(code: str) -> pd.DataFrame | None:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{code}.csv"
    if f.exists():
        try:
            return pd.read_csv(f)
        except Exception:
            pass
    params = {"lmt": 0, "klt": 101, "secid": secid(code), "fields1": "f1", "fields2": "f51,f52"}
    data = requests.get("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
                        params=params, headers=HEADERS, timeout=20).json().get("data") or {}
    rows = []
    for line in data.get("klines") or []:
        p = line.split(",")
        rows.append([p[0], float(p[1])])
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "main_net"])
    df.to_csv(f, index=False)
    return df


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
    print(f"池 {len(pool)} 只，抓取资金流历史…")

    with ThreadPoolExecutor(max_workers=8) as ex:
        flows = dict(zip([p["code"] for p in pool], ex.map(fetch_flow, [p["code"] for p in pool])))

    rows = {"base": [], "f3": [], "f3_f60": [], "f60": [], "f3_only_reversal": []}
    for p in pool:
        fl = flows.get(p["code"])
        df = read_hist(p["code"])
        if fl is None or df is None or len(df) < 300:
            continue
        d = df["date"].astype(str)
        c = df["close"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        m5 = pd.Series(c).rolling(5).mean().to_numpy()
        m17 = pd.Series(c).rolling(17).mean().to_numpy()
        wk_j, w_idx = weekly_series(h, l, c, d)
        jw = kdj_weekly_aligned(h, l, c, df["date"])
        jw_prev = np.array([wk_j[w - 1] if w > 0 else wk_j[0] for w in w_idx])
        jmin6 = np.zeros(len(c))
        for w in range(len(wk_j)):
            lo = max(0, w - 5)
            jmin6[w_idx == w] = wk_j[lo:w + 1].min()
        sig = (jmin6 <= 20) & (jw > jw_prev) & (c > m17) & (m5 > m17) & (jw < 80)

        fl = fl.sort_values("date").reset_index(drop=True)
        fl["s3"] = fl["main_net"].rolling(3).sum()
        fl["s20"] = fl["main_net"].rolling(20).sum()
        fl["s60"] = fl["main_net"].rolling(60).sum()
        fmap = {r.date: r for r in fl.itertuples()}

        for t in np.flatnonzero(sig[: len(c) - 22]):
            date = d.iloc[t]
            if tier_map.get(date) != "进攻":
                continue
            f = fmap.get(date)
            if f is None or pd.isna(f.s60):
                continue
            entry = c[t + 1]
            if entry <= 0:
                continue
            ret5 = (c[t + 1 + 5] / entry - 1) * 100
            ret20 = (c[t + 1 + 20] / entry - 1) * 100
            row = {"r5": ret5, "r20": ret20}
            rows["base"].append(row)
            if f.s3 > 0:
                rows["f3"].append(row)
                if f.s60 > 0:
                    rows["f3_f60"].append(row)
                else:
                    rows["f3_only_reversal"].append(row)
            if f.s60 > 0:
                rows["f60"].append(row)

    out = []
    for key, label in (("base", "不过滤资金"),
                       ("f3", "3 日流入"),
                       ("f60", "60 日流入"),
                       ("f3_f60", "3 日流入 + 60 日流入"),
                       ("f3_only_reversal", "3 日流入但 60 日流出（反转）")):
        d = pd.DataFrame(rows[key])
        if not len(d):
            continue
        out.append({
            "资金条件": label, "样本": len(d),
            "5 日均%": round(d.r5.mean(), 2),
            "5 日胜率%": round((d.r5 > 0).mean() * 100, 1),
            "20 日均%": round(d.r20.mean(), 2),
            "20 日胜率%": round((d.r20 > 0).mean() * 100, 1),
        })
    print(pd.DataFrame(out).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
