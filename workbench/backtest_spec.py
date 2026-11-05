#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规格书口径回测：用日线数据裁决若干未定口径

对比项：
  1. C4 的 J 用日线还是周线
  2. 中性档 J 上限 90 还是 100
  3. 防守档是否允许煤炭
  4. 环境过滤（站上MA20占比/成交额/涨停）是否该用

方法：
  - 信号在 t 日收盘确认，t+1 日收盘入场，看 5/10/20 日后的收益
  - 档位按上证 000001、创业板 399006 与各自 MA20 判定（收盘口径）
  - 股票池用当前同时满足规模≥10亿、成交额≥1000万、溢价≤3% 的股票型 ETF
  - 前复权日线，样本区间为缓存覆盖期（约 2023-11 起）

用法：python3 workbench/backtest_spec.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
CACHE = PROJECT / "data" / "etf_hist_cache"
IDX_CACHE = PROJECT / "data" / "index_hist_cache"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

HORIZONS = (5, 10, 20)
J_DAILY_CAP = 90
DIST_UP = 8.0
DIST_DEF = 6.0


# ---------- 数据 ----------

def fetch_index(code: str, prefix: str) -> pd.DataFrame:
    IDX_CACHE.mkdir(parents=True, exist_ok=True)
    f = IDX_CACHE / f"{code}.csv"
    if f.exists():
        df = pd.read_csv(f)
        if len(df) > 400:
            return df
    params = {
        "secid": f"{prefix}.{code}", "klt": 101, "fqt": 1,
        "beg": "20220101", "end": "20500101",
        "fields1": "f1,f2", "fields2": "f51,f52,f53,f54,f55",
    }
    data = requests.get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                        params=params, headers=HEADERS, timeout=20).json()["data"]
    rows = []
    for line in data["klines"]:
        p = line.split(",")
        rows.append([p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4])])
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low"])
    df.to_csv(f, index=False)
    return df


def read_hist(code: str) -> pd.DataFrame | None:
    f = CACHE / f"{code}.csv"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f)
    except Exception:
        return None
    if len(df) < 200:
        return None
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["close"]).reset_index(drop=True)


def live_universe() -> dict:
    """取实时规模/成交额/溢价，返回 {code: row}"""
    from concurrent.futures import ThreadPoolExecutor
    FS = "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827"

    def page(pn):
        p = {"pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
             "fid": "f12", "fs": FS, "fields": "f12,f2,f6,f21,f402"}
        return requests.get("https://push2.eastmoney.com/api/qt/clist/get",
                            params=p, headers=HEADERS, timeout=20).json()["data"]["diff"]

    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = [r for part in ex.map(page, range(1, 18)) for r in part]
    return {r["f12"]: r for r in rows}


# ---------- 指标 ----------

def ema(arr: np.ndarray, span: int) -> np.ndarray:
    return pd.Series(arr).ewm(span=span, adjust=False).mean().to_numpy()


def macd(close: np.ndarray) -> tuple:
    dif = ema(close, 12) - ema(close, 26)
    dea = ema(dif, 9)
    return dif, dea


def kdj_daily(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    n = len(close)
    k = np.full(n, np.nan)
    d = np.full(n, np.nan)
    j = np.full(n, np.nan)
    prev_k = prev_d = 50.0
    for i in range(n):
        lo = max(0, i - 8)
        hh = high[lo:i + 1].max()
        ll = low[lo:i + 1].min()
        span = hh - ll
        rsv = 50.0 if span <= 0 else (close[i] - ll) / span * 100
        if i == 0:
            cur_k, cur_d = rsv, rsv
        else:
            cur_k = rsv / 3 + prev_k * 2 / 3
            cur_d = cur_k / 3 + prev_d * 2 / 3
        k[i], d[i], j[i] = cur_k, cur_d, 3 * cur_k - 2 * cur_d
        prev_k, prev_d = cur_k, cur_d
    return j


def week_index(dates: pd.Series) -> np.ndarray:
    days = dates.to_numpy().astype("datetime64[D]").astype(np.int64)
    wd = (days + 3) % 7
    monday = days - wd
    _, inverse = np.unique(monday, return_inverse=True)
    return inverse


def kdj_weekly_aligned(high, low, close, dates) -> np.ndarray:
    """每个交易日对应的周线 J（含当前未走完的一周）。"""
    w_idx = week_index(dates)
    n_weeks = w_idx[-1] + 1
    # 已走完周的 K/D
    wk_h = np.zeros(n_weeks)
    wk_l = np.zeros(n_weeks)
    wk_c = np.zeros(n_weeks)
    for i in range(len(close)):
        w = w_idx[i]
        if i == 0 or w_idx[i] != w_idx[i - 1]:
            wk_h[w], wk_l[w], wk_c[w] = high[i], low[i], close[i]
        else:
            wk_h[w] = max(wk_h[w], high[i])
            wk_l[w] = min(wk_l[w], low[i])
            wk_c[w] = close[i]
    wk_k = np.zeros(n_weeks)
    wk_d = np.zeros(n_weeks)
    prev_k = prev_d = 50.0
    for w in range(n_weeks):
        lo = max(0, w - 8)
        hh = wk_h[lo:w + 1].max()
        ll = wk_l[lo:w + 1].min()
        span = hh - ll
        rsv = 50.0 if span <= 0 else (wk_c[w] - ll) / span * 100
        cur_k = rsv if w == 0 else rsv / 3 + prev_k * 2 / 3
        cur_d = cur_k if w == 0 else cur_k / 3 + prev_d * 2 / 3
        wk_k[w], wk_d[w] = cur_k, cur_d
        prev_k, prev_d = cur_k, cur_d

    # 当日所在周的实时期（周内部分）
    out = np.full(len(close), np.nan)
    week_start = {}
    for i in range(len(close)):
        w = w_idx[i]
        if i == 0 or w_idx[i] != w_idx[i - 1]:
            week_start[w] = i
    for i in range(len(close)):
        w = w_idx[i]
        s = week_start[w]
        lo_w = max(0, w - 8)
        hh = wk_h[lo_w:w].max() if w > 0 else high[s:i + 1].max()
        ll = wk_l[lo_w:w].min() if w > 0 else low[s:i + 1].min()
        hh = max(hh, high[s:i + 1].max())
        ll = min(ll, low[s:i + 1].min())
        span = hh - ll
        rsv = 50.0 if span <= 0 else (close[i] - ll) / span * 100
        if w == 0:
            cur_k = cur_d = rsv
        else:
            cur_k = rsv / 3 + wk_k[w - 1] * 2 / 3
            cur_d = cur_k / 3 + wk_d[w - 1] * 2 / 3
        out[i] = 3 * cur_k - 2 * cur_d
    return out


# ---------- 板块属性 ----------

DEFENSIVE = r"银行|煤炭|红利|电力|公用|水务|高速|央企|国企"
NEUTRAL = r"沪深300|证券|券商|A50|A500|上证50|价值|质量"
OFFENSIVE = (r"半导体|芯片|科创|机器人|军工|国防|传媒|游戏|人工智能|AI|通信|计算机|软件|"
             r"电池|新能源|光伏|汽车|有色|稀土|钢铁|化工|机械|建材|地产|医药|生物")
WEAK = r"酒|消费|食品|饮料"


def board_group(name: str) -> str:
    if pd.Series([name]).str.contains(WEAK, regex=True).iloc[0]:
        return "弱趋势"
    if pd.Series([name]).str.contains(DEFENSIVE, regex=True).iloc[0]:
        return "防御"
    if pd.Series([name]).str.contains(OFFENSIVE, regex=True).iloc[0]:
        return "进攻"
    if pd.Series([name]).str.contains(NEUTRAL, regex=True).iloc[0]:
        return "中性"
    return "未分类"


# ---------- 主流程 ----------

def build_pool(base: dict, live: dict) -> list:
    pool = []
    for rec in base["codes"]:
        code = rec["c"]
        r = live.get(code)
        if not r or rec["a"] not in ("宽基", "行业主题"):
            continue
        if (r.get("f21") or 0) < 1e9 or (r.get("f6") or 0) < 1e7:
            continue
        pm = None if r.get("f402") in (None, "-") else -r["f402"]
        if pm is not None and pm > 3:
            continue
        g = board_group(rec["n"])
        pool.append({"code": code, "name": rec["n"], "asset": rec["a"], "group": g,
                     "is_coal": ("煤炭" in rec["n"])})
    return pool


def main() -> int:
    base = json.load(open(ROOT / "dist" / "data" / "base.json", encoding="utf-8"))
    pool = build_pool(base, live_universe())
    print(f"股票池 {len(pool)} 只；分组 {pd.Series([p['group'] for p in pool]).value_counts().to_dict()}")

    sh = fetch_index("000001", "1")
    cy = fetch_index("399006", "0")
    idx = sh[["date", "close"]].merge(cy[["date", "close"]], on="date", suffixes=("_sh", "_cy"))
    idx["ma20_sh"] = idx["close_sh"].rolling(20).mean()
    idx["ma20_cy"] = idx["close_cy"].rolling(20).mean()
    idx["tier"] = np.where(
        (idx["close_sh"] >= idx["ma20_sh"]) & (idx["close_cy"] >= idx["ma20_cy"]), "进攻",
        np.where((idx["close_sh"] >= idx["ma20_sh"]) | (idx["close_cy"] >= idx["ma20_cy"]), "中性", "防守"))
    tier_map = dict(zip(idx["date"], idx["tier"]))
    print("档位分布(交易日)", idx["tier"].value_counts().to_dict())

    records = {v: [] for v in ("A_daily90_禁煤", "B_weekly90_禁煤", "C_daily100_禁煤", "D_daily90_放煤")}
    market = []
    for p in pool:
        df = read_hist(p["code"])
        if df is None:
            continue
        dates = df["date"].astype(str)
        c = df["close"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        n = len(c)
        if n < 260:
            continue
        ma5 = pd.Series(c).rolling(5).mean().to_numpy()
        ma17 = pd.Series(c).rolling(17).mean().to_numpy()
        ma20 = pd.Series(c).rolling(20).mean().to_numpy()
        ma20_up = np.zeros(n, bool)
        for i in range(3, n):
            ma20_up[i] = ma20[i] > ma20[i - 1] > ma20[i - 2] > ma20[i - 3]
        dif, dea = macd(c)
        jd = kdj_daily(h, l, c)
        jw = kdj_weekly_aligned(h, l, c, df["date"])
        tier = np.array([tier_map.get(d, "防守") for d in dates])
        dist20 = (c / ma20 - 1) * 100

        c1 = c > ma17
        c2 = ma5 > ma17
        c3 = ma20_up
        c5 = dif > dea
        base_cond = c1 & c2 & c3 & c5

        # 各变体信号：t 日收盘确认，t+1 收盘入场
        # 按规格书 §6.2：进攻档只要 C1&C2&C5；中性/防守/弱趋势走五条件
        j_gt100 = jd > 100
        dist_cap_arr = np.where(tier == "中性", DIST_UP, DIST_DEF)

        def signals(j_arr, cap_mid, coal_ok):
            c4 = j_arr <= cap_mid
            five = c1 & c2 & c3 & c5 & c4
            strict = c1 & c2 & c3 & c5 & (j_arr <= 90)
            ok = np.where(
                tier == "进攻", c1 & c2 & c5,
                np.where(tier == "中性", five & (dist20 <= DIST_UP),
                         strict & (dist20 <= DIST_DEF) & np.isin(
                             [p["group"]] * n, ["防御"])))
            if p["group"] == "防御" and not coal_ok and p["is_coal"]:
                ok = ok & (tier != "防守")
            if p["group"] == "弱趋势":
                ok = strict & (dist20 <= DIST_DEF)
            return ok & ~j_gt100

        variants = {
            "A_daily90_禁煤": signals(jd, 90, False),
            "B_weekly90_禁煤": signals(jw, 90, False),
            "C_daily100_禁煤": signals(jd, 100, False),
            "D_daily90_放煤": signals(jd, 90, True),
        }
        # 同日全池基准（等权）
        for t in range(n - max(HORIZONS) - 2):
            entry = c[t + 1]
            if entry <= 0:
                continue
            mrow = {"date": dates[t]}
            for hz in HORIZONS:
                mrow[f"b{hz}"] = float(c[t + 1 + hz] / entry - 1) * 100
            market.append(mrow)

        for variant, ok in variants.items():
            idxs = np.flatnonzero(ok[: n - max(HORIZONS) - 2])
            for t in idxs:
                entry = c[t + 1]
                if entry <= 0:
                    continue
                row = {"code": p["code"], "group": p["group"], "tier": tier[t],
                       "is_coal": p["is_coal"], "amt": float(df["volume"].iloc[t] * c[t]),
                       "date": dates[t], "j": float(jd[t]), "dist20": float(dist20[t])}
                for hz in HORIZONS:
                    row[f"r{hz}"] = float(c[t + 1 + hz] / entry - 1) * 100
                records[variant].append(row)

    bench = pd.DataFrame(market).groupby("date")[["b5", "b10", "b20"]].mean()
    out = {}
    print("\n=== 各变体整体（超额 = 信号收益 - 同日全池均值） ===")
    print(f"{'变体':16s}{'笔数':>7s}{'5日':>7s}{'5超额':>7s}{'5胜率':>7s}{'20日':>7s}{'20超额':>8s}{'20胜率':>7s}")
    for v, rows in records.items():
        if not rows:
            continue
        df = pd.DataFrame(rows).join(bench, on="date")
        df["a5"] = df.r5 - df.b5
        df["a20"] = df.r20 - df.b20
        out[v] = {
            "n": len(df), "r5": df.r5.mean(), "a5": df.a5.mean(),
            "w5": (df.r5 > 0).mean() * 100, "r10": df.r10.mean(),
            "r20": df.r20.mean(), "a20": df.a20.mean(),
            "w20": (df.r20 > 0).mean() * 100,
        }
        print(f"{v:16s}{len(df):7d}{df.r5.mean():7.2f}{df.a5.mean():7.2f}"
              f"{(df.r5 > 0).mean() * 100:7.1f}{df.r20.mean():7.2f}{df.a20.mean():8.2f}"
              f"{(df.r20 > 0).mean() * 100:7.1f}")

    frames = {}
    for v, rows in records.items():
        if not rows:
            continue
        d = pd.DataFrame(rows).join(bench, on="date")
        d["a5"] = d.r5 - d.b5
        d["a20"] = d.r20 - d.b20
        frames[v] = d

    print("\n=== 变体 × 档位（5日均值 / 超额 / 笔数） ===")
    for v, df in frames.items():
        g = df.groupby("tier").agg(n=("r5", "size"), r5=("r5", "mean"), a5=("a5", "mean"), r20=("r20", "mean"), a20=("a20", "mean"))
        print(f"\n[{v}]")
        print(g.round(2).to_string())

    print("\n=== 中性档：日线J90 / 周线J90 / 日线J100 ===")
    rows = []
    for v, df in frames.items():
        d = df[df.tier == "中性"]
        if len(d):
            rows.append([v, len(d), d.r5.mean(), d.a5.mean(), d.r20.mean(), d.a20.mean()])
    print(pd.DataFrame(rows, columns=["变体", "n", "r5", "a5", "r20", "a20"]).round(2).to_string(index=False))

    print("\n=== 防守档：禁煤 vs 放煤 ===")
    rows = []
    for v in ("A_daily90_禁煤", "D_daily90_放煤"):
        df = frames.get(v)
        if df is None:
            continue
        d = df[df.tier == "防守"]
        coal = d[d.is_coal]
        rows.append([v, len(d), d.r5.mean(), d.a5.mean(), d.r20.mean(), len(coal),
                     coal.r5.mean() if len(coal) else float("nan")])
    print(pd.DataFrame(rows, columns=["变体", "n", "r5", "a5", "r20", "煤炭笔数", "煤炭r5"]).round(2).to_string(index=False))

    print("\n=== 每日按「距MA20最近」取前 N（贴近规格书 §6.3/§8.3） ===")
    rows = []
    for v, df in frames.items():
        for n in (2, 5, 10):
            sel = df.sort_values(["date", "dist20"]).groupby("date").head(n)
            rows.append([v, n, len(sel), sel.r5.mean(), sel.a5.mean(),
                         (sel.a5 > 0).mean() * 100, sel.r20.mean(), sel.a20.mean()])
    print(pd.DataFrame(rows, columns=["变体", "每日取", "n", "r5", "a5", "超额胜率", "r20", "a20"]).round(2).to_string(index=False))

    print("\n=== 进攻档每日取前 2 ===")
    rows = []
    for v, df in frames.items():
        d = df[df.tier == "进攻"]
        sel = d.sort_values(["date", "dist20"]).groupby("date").head(2)
        rows.append([v, len(sel), sel.r5.mean(), sel.a5.mean(), (sel.a5 > 0).mean() * 100,
                     sel.r20.mean(), sel.a20.mean()])
    print(pd.DataFrame(rows, columns=["变体", "n", "r5", "a5", "超额胜率", "r20", "a20"]).round(2).to_string(index=False))

    print("\n=== 排序方式对比（A 基准，每日取前 2 / 前 5） ===")
    df = frames["A_daily90_禁煤"]
    orders = {
        "距MA20最近(规格书)": (["date", "dist20"], [True, True]),
        "距MA20最远": (["date", "dist20"], [True, False]),
        "成交额最大": (["date", "amt"], [True, False]),
        "成交额最大+距离次之": (["date", "amt", "dist20"], [True, False, True]),
        "距MA20最近+成交额次之": (["date", "dist20", "amt"], [True, True, False]),
    }
    rows = []
    for label, (cols, asc) in orders.items():
        for n in (2, 5):
            sel = df.sort_values(cols, ascending=asc).groupby("date").head(n)
            rows.append([label, n, len(sel), sel.r5.mean(), sel.a5.mean(),
                         (sel.a5 > 0).mean() * 100, sel.r20.mean(), sel.a20.mean()])
    print(pd.DataFrame(rows, columns=["排序", "每日取", "n", "r5", "a5", "超额胜率", "r20", "a20"]).round(2).to_string(index=False))

    print("\n=== 排序方式对比（仅进攻档，每日取前 2） ===")
    atk = df[df.tier == "进攻"]
    rows = []
    for label, (cols, asc) in orders.items():
        sel = atk.sort_values(cols, ascending=asc).groupby("date").head(2)
        rows.append([label, len(sel), sel.r5.mean(), sel.a5.mean(), (sel.a5 > 0).mean() * 100,
                     sel.r20.mean(), sel.a20.mean()])
    print(pd.DataFrame(rows, columns=["排序", "n", "r5", "a5", "超额胜率", "r20", "a20"]).round(2).to_string(index=False))

    print("\n=== 距MA20 分档的超额（A 基准，含中位数） ===")
    df = frames["A_daily90_禁煤"].copy()
    bins = [-100, 0, 1, 2, 3, 4, 6, 8, 100]
    labels = ["<0", "0-1", "1-2", "2-3", "3-4", "4-6", "6-8", ">8"]
    df["bucket"] = pd.cut(df.dist20, bins=bins, labels=labels)
    g = df.groupby("bucket", observed=True).agg(
        n=("a5", "size"), a5=("a5", "mean"), a5_med=("a5", "median"),
        a20=("a20", "mean"), a20_med=("a20", "median"),
        win=("a5", lambda x: (x > 0).mean() * 100))
    print(g.round(2).to_string())

    print("\n=== 进攻档 距MA20 分档 ===")
    atk2 = df[df.tier == "进攻"]
    g2 = atk2.groupby("bucket", observed=True).agg(
        n=("a5", "size"), a5=("a5", "mean"), a5_med=("a5", "median"),
        a20=("a20", "mean"), a20_med=("a20", "median"),
        win=("a5", lambda x: (x > 0).mean() * 100))
    print(g2.round(2).to_string())

    print("\n=== 排序方式（含中位数，前2） ===")
    rows = []
    for label, (cols, asc) in orders.items():
        sel = df.sort_values(cols, ascending=asc).groupby("date").head(2)
        rows.append([label, len(sel), sel.a5.mean(), sel.a5.median(),
                     (sel.a5 > 0).mean() * 100, sel.a20.mean(), sel.a20.median()])
    print(pd.DataFrame(rows, columns=["排序", "n", "a5均", "a5中位", "超额胜率", "a20均", "a20中位"]).round(2).to_string(index=False))

    print("\n=== 基准：各档位同日全池均值 ===")
    print(bench.describe().round(3).to_string())

    Path(ROOT / "backtest_spec_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
