#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF 买点工作台 · 数据构建

抓取全市场 ETF 快照与日线，产出一份静态数据文件供纯前端工作台使用：
  - 每只 ETF 最近 60 个交易日收盘价（算 MA5/MA17/MA60、画走势）
  - 每只 ETF 最近 45 周的高/低/收（算周线 KDJ）
  - 名称、资产类型、成交额等基础信息

日线缓存复用仓库的 data/etf_hist_cache/<code>.csv，缺哪只补哪只。
用法：
    python3 workbench/build_data.py              # 增量更新后重建 dist/data/base.json
    python3 workbench/build_data.py --force      # 忽略缓存全量重抓
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
CACHE_DIR = PROJECT / "data" / "etf_hist_cache"
DIST_DATA = ROOT / "dist" / "data"

SPOT_URL = "https://push2.eastmoney.com/api/qt/clist/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
FLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
FLOW_CACHE = PROJECT / "data" / "flow_cache.json"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
SPOT_FIELDS = "f12,f13,f14,f2,f3,f5,f6,f8,f10,f62,f402,f124,f297"
DAILY_KEEP = 60
WEEK_KEEP = 45

# 基金公司后缀，用于把 "<赛道>ETF<公司>" 归一成赛道名
COMPANY_SUFFIXES = (
    "华夏", "易方达", "南方", "广发", "嘉实", "华泰柏瑞", "天弘", "富国", "汇添富",
    "博时", "建信", "招商", "银华", "国泰", "工银", "华宝", "万家", "鹏华", "景顺",
    "摩根", "东财", "方正", "平安", "华安", "国联", "兴证", "大成", "融通", "海富通",
    "光大", "贝莱德", "中金", "申万", "浦银", "上银", "永赢", "汇丰", "国泰君安",
    "中银", "华富", "中欧", "泰康", "长城", "银河", "长盛", "金鹰", "诺安", "财通",
    "中信", "中邮", "兴全", "景顺长城", "华泰", "方正富邦", "兴银", "东方", "华商",
    "国投", "国海", "山西", "湘财", "华鑫", "中航", "红土", "前海", "兴证全球",
)

SECTOR_ALIASES = {
    "证券公司": "证券", "创新药产业": "创新药", "科创创新药": "科创创新药",
    "医疗器械服务": "医疗器械", "港股通创新药": "港股创新药", "恒生创新药": "港股创新药",
    "港股通医疗": "港股医药", "港股医疗": "港股医药", "港股通医药": "港股医药",
    "恒生生物科技": "恒生医药", "医疗创新": "医疗",
}


def sector_key(name: str) -> str:
    """从 ETF 名称剥离基金公司后缀与 "ETF/基金" 字样，得到赛道名。"""
    n = name
    for suffix in COMPANY_SUFFIXES:
        if n.endswith(suffix) and len(n) > len(suffix):
            n = n[:-len(suffix)]
            break
    n = n.replace("ETF", "").replace("基金", "").strip()
    return SECTOR_ALIASES.get(n, n) or name


# 资产类型判定，按顺序命中即返回
ASSET_RULES = [
    ("行业主题", r"黄金股|黄金产业|黄金主题|白银股"),
    ("宽基", r"沪深300|中证300|上证50|上证180|上证380|中证500|中证1000|中证2000|中证A500|中证A50|"
             r"科创50|科创100|科创200|科创综指|创业板|深证100|深证50|北证50|国证2000|MSCI|中证800|"
             r"双创|中小100|上证指数|深证成指|现金流|自由现金流|A500|A50|超大盘|大盘|中盘|小盘"),
    ("货币", r"货币|日利|添益|保证金|理财|快线|现金"),
    ("债券", r"债|短融|存单|政金|国开|城投|信用"),
    ("商品", r"黄金|白银|原油|豆粕|商品|能源化工|铜|螺纹|铁矿"),
    ("跨境", r"恒生|港股|中概|纳指|纳斯达克|标普|道琼斯|德国|日经|法国|东南亚|印度|越南|沙特|美国|全球|亚太|巴西|英国|海外|QDII|H股"),
]

SIGNAL_TEXT = {
    "buy": "可以买入", "add": "可以加仓", "trim": "可以减仓",
    "exit": "该离场", "wait": "观望",
}


def asset_type(name: str) -> str:
    for label, pattern in ASSET_RULES:
        if re.search(pattern, name):
            return label
    return "行业主题"


def _num(v, default=0.0):
    try:
        if v in ("-", "", None):
            return default
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def fetch_spot() -> tuple[list[dict], str]:
    """并发分页拉全市场 ETF 快照。返回 (行列表, 数据日期YYYY-MM-DD)。"""
    first = requests.get(SPOT_URL, params={
        "pn": 1, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f12",
        "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827", "fields": SPOT_FIELDS,
    }, headers=HEADERS, timeout=20).json()["data"]
    total = int(first["total"])
    pages = (total + 99) // 100

    def page(pn: int) -> list[dict]:
        r = requests.get(SPOT_URL, params={
            "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f12",
            "fs": "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827", "fields": SPOT_FIELDS,
        }, headers=HEADERS, timeout=20)
        return r.json().get("data", {}).get("diff") or []

    rows = list(first.get("diff") or [])
    with ThreadPoolExecutor(max_workers=8) as ex:
        for part in ex.map(page, range(2, pages + 1)):
            rows.extend(part)

    qdate = ""
    out = []
    for r in rows:
        code = str(r.get("f12") or "")
        name = str(r.get("f14") or "")
        price = _num(r.get("f2"))
        if not code or not name or price <= 0:
            continue
        raw_date = str(r.get("f297") or "")
        if len(raw_date) == 8 and not qdate:
            qdate = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
        out.append({
            "code": code, "name": name, "price": price,
            "pct": _num(r.get("f3")), "volume": _num(r.get("f5")),
            "amount": _num(r.get("f6")), "turnover": _num(r.get("f8")),
            "liangbi": _num(r.get("f10")), "main_inflow": _num(r.get("f62")),
            "premium": -_num(r.get("f402"), float("nan")) if r.get("f402") not in ("-", None) else None,
            "asset": asset_type(name),
        })
    return out, qdate or dt.date.today().isoformat()


def fetch_index_tail(secid_: str, keep: int = 20) -> dict:
    """指数最近 N 个交易日收盘，用于前端算动态 MA20。"""
    params = {
        "secid": secid_, "klt": 101, "fqt": 1, "beg": "20250101", "end": "20500101",
        "fields1": "f1,f2", "fields2": "f51,f53",
    }
    data = requests.get(KLINE_URL, params=params, headers=HEADERS, timeout=20).json()["data"]
    klines = data.get("klines") or []
    rows = [line.split(",") for line in klines[-keep:]]
    return {"d": [round(float(r[1]), 2) for r in rows], "ds": rows[-1][0] if rows else ""}


def secid(code: str) -> str:
    return ("1." if code.startswith(("5", "6", "9")) else "0.") + code


def fetch_kline(code: str) -> list[list]:
    beg = (dt.date.today() - dt.timedelta(days=1100)).strftime("%Y%m%d")
    params = {
        "secid": secid(code), "klt": 101, "fqt": 1, "beg": beg, "end": "20500101",
        "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }
    data = requests.get(KLINE_URL, params=params, headers=HEADERS, timeout=20).json().get("data")
    if not data or not data.get("klines"):
        return []
    rows = []
    for line in data["klines"]:
        # 源字段顺序：日期,开盘,收盘,最高,最低,成交量,成交额
        p = line.split(",")
        rows.append([p[0], float(p[1]), float(p[3]), float(p[4]), float(p[2]), float(p[5])])
    return rows


def secid(code: str) -> str:
    return ("1." if code.startswith(("5", "6", "9")) else "0.") + code


def fetch_flow(code: str) -> dict | None:
    params = {"lmt": 15, "klt": 101, "secid": secid(code),
              "fields1": "f1,f2,f3,f7", "fields2": "f51,f52"}
    data = requests.get(FLOW_URL, params=params, headers=HEADERS, timeout=15).json().get("data") or {}
    vals = [float(line.split(",")[1]) for line in (data.get("klines") or [])]
    if not vals:
        return None
    return {"s3": sum(vals[-3:]), "s5": sum(vals[-5:]), "s10": sum(vals[-10:])}


def load_flow(codes: list[str], qdate: str, force: bool) -> dict:
    """每日主力净流入 3/5/10 日累计，按行情日期缓存。"""
    cache = {}
    if FLOW_CACHE.exists() and not force:
        try:
            cache = json.loads(FLOW_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    todo = [c for c in codes if cache.get(c, {}).get("date") != qdate]
    if todo:
        print(f"[资金流] 需更新 {len(todo)} 只", flush=True)
        done, lock = 0, threading.Lock()

        def work(code):
            nonlocal done
            try:
                flow = fetch_flow(code)
            except (requests.RequestException, ValueError):
                flow = None
            with lock:
                done += 1
                if done % 200 == 0 or done == len(todo):
                    print(f"      {done}/{len(todo)}", flush=True)
            return code, flow

        with ThreadPoolExecutor(max_workers=12) as ex:
            for code, flow in ex.map(work, todo):
                if flow:
                    cache[code] = {"date": qdate, **flow}
    FLOW_CACHE.parent.mkdir(parents=True, exist_ok=True)
    FLOW_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return cache


def cache_path(code: str) -> Path:
    return CACHE_DIR / f"{code}.csv"


def read_cache(code: str) -> list[list] | None:
    f = cache_path(code)
    if not f.exists():
        return None
    try:
        rows = []
        with f.open(encoding="utf-8") as fh:
            next(fh, None)
            for line in fh:
                p = line.strip().split(",")
                if len(p) < 6 or not p[0]:
                    continue
                rows.append([p[0], float(p[1]), float(p[2]), float(p[3]),
                             float(p[4]), float(p[5])])
        return rows or None
    except (OSError, ValueError):
        return None


def write_cache(code: str, rows: list[list]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = cache_path(code).with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write("date,open,high,low,close,volume\n")
        for r in rows:
            fh.write(f"{r[0]},{r[1]},{r[2]},{r[3]},{r[4]},{r[5]}\n")
    tmp.replace(cache_path(code))


def is_stale(rows: list[list] | None, qdate: str) -> bool:
    if not rows:
        return True
    try:
        last = dt.date.fromisoformat(str(rows[-1][0])[:10])
        ref = dt.date.fromisoformat(qdate)
    except ValueError:
        return True
    return (ref - last).days > 4


def load_history(items: list[dict], qdate: str, force: bool) -> dict:
    """返回 {code: 日线行列表}，缺的并发补抓。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    hist, todo = {}, []
    for it in items:
        rows = None if force else read_cache(it["code"])
        if is_stale(rows, qdate):
            todo.append(it)
        else:
            hist[it["code"]] = rows
    if todo:
        print(f"[日线] 需更新 {len(todo)} 只，缓存命中 {len(hist)} 只", flush=True)
        done, lock = 0, threading.Lock()

        def work(it):
            nonlocal done
            rows = None
            for attempt in range(3):
                try:
                    rows = fetch_kline(it["code"])
                    break
                except (requests.RequestException, ValueError):
                    time.sleep(0.5 * (attempt + 1))
            if rows:
                write_cache(it["code"], rows)
            with lock:
                done += 1
                if done % 100 == 0 or done == len(todo):
                    print(f"      {done}/{len(todo)}", flush=True)
            return it["code"], rows

        with ThreadPoolExecutor(max_workers=12) as ex:
            for code, rows in ex.map(work, todo):
                if rows:
                    hist[code] = rows
    return hist


SBL3 = dict(mult_min=3.0, mult_max=6.0, mult_abort=8.0, min_chg=3.0,
            max_upper=0.10, min_body=0.50, flat_range=15.0, flat_amp=3.5,
            high_mult=1.25, pull_lo=0.30, pull_hi=0.60, pull_days=3)


def _sbl3_flags(rows: list[list], i: int) -> dict | None:
    """对第 i 根 K 线做三倍量判定（口径同 sbl3_scan.py）。"""
    if i < 70:
        return None
    bar = rows[i]
    prev = rows[i - 1]
    o, h, l, c, v = bar[1], bar[2], bar[3], bar[4], bar[5]
    vol10 = sum(r[5] for r in rows[i - 10:i]) / 10
    if vol10 <= 0:
        return None
    mult = v / vol10
    chg = (c / prev[4] - 1) * 100
    rng = h - l
    body = (c - o) / rng if rng else 0.0
    upper = (h - c) / rng if rng else 0.0
    closes = [r[4] for r in rows[i - 59:i + 1]]
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / len(closes)
    hi20 = max(r[2] for r in rows[i - 20:i])
    hi60 = max(r[2] for r in rows[i - 60:i])
    win = rows[i - 30:i]
    flat_range = (max(r[4] for r in win) / min(r[4] for r in win) - 1) * 100
    flat_amp = sum((r[2] / r[3] - 1) * 100 for r in win if r[3] > 0) / 30
    v10_prev = sum(r[5] for r in win[-10:]) / 10
    v60_prev = sum(r[5] for r in rows[i - 60:i]) / 60
    dry = v10_prev / v60_prev if v60_prev else 1.0
    f = {
        "mult_ok": SBL3["mult_min"] <= mult <= SBL3["mult_max"],
        "abort": mult > SBL3["mult_abort"],
        "chg_ok": chg >= SBL3["min_chg"],
        "body_ok": body >= SBL3["min_body"],
        "shadow_ok": upper <= SBL3["max_upper"],
        "above20": c >= ma20,
        "break": c >= hi20 or c >= hi60,
        "flat_ok": flat_range <= SBL3["flat_range"] and flat_amp <= SBL3["flat_amp"],
        "dry_ok": dry <= 0.8,
        "high_pos": c > ma60 * SBL3["high_mult"],
    }
    f["core"] = all([f["mult_ok"], f["chg_ok"], f["body_ok"], f["shadow_ok"], f["above20"], f["break"]])
    f["valid"] = f["core"] and not f["high_pos"] and not f["abort"]
    return {"mult": mult, "chg": chg, "body": body, "upper": upper,
            "flat": flat_range, "amp": flat_amp, "dry": dry,
            "low": l, "hi20": hi20, "hi60": hi60, "flags": f}


def sbl3_scan(rows: list[list]) -> dict | None:
    """当日爆量 + 最近一次爆量后的回调跟踪。"""
    n = len(rows)
    if n < 80:
        return None
    today = _sbl3_flags(rows, n - 1)
    if today is None:
        return None
    out = {
        "m": round(today["mult"], 2), "c": round(today["chg"], 2),
        "b": round(today["body"], 2), "u": round(today["upper"], 2),
        "f": round(today["flat"], 1), "a": round(today["amp"], 2),
        "d": round(today["dry"], 2), "life": round(today["low"], 3),
        "hi20": round(today["hi20"], 3),
        "ok": bool(today["flags"]["valid"]),
    }
    # 最近 5 根内的爆量日 → 回调状态
    for i in range(n - 1, max(70, n - 6) - 1, -1):
        sig = _sbl3_flags(rows, i)
        if sig and sig["flags"]["valid"]:
            days = n - 1 - i
            life = sig["low"]
            after = rows[i + 1:]
            held = min((r[3] for r in after), default=life) >= life
            vr = rows[-1][5] / rows[i][5] if rows[i][5] else 0
            last = rows[-1]
            yang = last[4] > last[1] and last[4] >= rows[-2][4]
            if not held:
                state = "失效"
            elif days == 0:
                state = "今日爆量"
            elif days <= SBL3["pull_days"] and yang and SBL3["pull_lo"] <= vr <= SBL3["pull_hi"]:
                state = "企稳可进"
            elif days <= SBL3["pull_days"]:
                state = "回调中"
            else:
                state = "超时"
            out["st"] = {"d": days, "vr": round(vr, 2), "held": bool(held),
                         "yang": bool(yang), "s": state, "life": round(life, 3)}
            break
    return out


def week_key(date_str: str) -> str:
    d = dt.date.fromisoformat(date_str[:10])
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def weekly_bars(rows: list[list]) -> list[list]:
    """把日线聚合成周线 [[high, low, close], ...]，按周一起点顺序。"""
    bars: list[list] = []
    cur_key = None
    for r in rows:
        k = week_key(r[0])
        high, low, close = r[2], r[3], r[4]
        if k != cur_key:
            bars.append([high, low, close])
            cur_key = k
        else:
            b = bars[-1]
            b[0] = max(b[0], high)
            b[1] = min(b[1], low)
            b[2] = close
    return bars


def build(force: bool = False) -> Path:
    t0 = time.time()
    print("[1/3] 拉取全市场 ETF 快照…", flush=True)
    items, qdate = fetch_spot()
    print(f"      {len(items)} 只，数据日期 {qdate}", flush=True)

    print("[2/3] 读取/更新日线缓存…", flush=True)
    hist = load_history(items, qdate, force)

    print("[3/3] 聚合周线与近 60 日收盘…", flush=True)
    flows = load_flow([it["code"] for it in items], qdate, force)
    codes = []
    skipped = 0
    for it in items:
        rows = hist.get(it["code"])
        if not rows or len(rows) < 130:
            skipped += 1
            continue
        weekly = weekly_bars(rows)[-WEEK_KEEP:]
        closes = [round(float(r[4]), 4) for r in rows[-DAILY_KEEP:]]
        codes.append({
            "c": it["code"], "n": it["name"], "a": it["asset"], "g": sector_key(it["name"]),
            "p": it["price"], "pct": it["pct"], "amt": it["amount"],
            "to": it["turnover"], "lb": it["liangbi"], "mf": it["main_inflow"],
            "pm": it["premium"],
            "d": closes,
            "dl": [round(float(r[3]), 4) for r in rows[-20:]],
            "w": [[round(b[0], 4), round(b[1], 4), round(b[2], 4)] for b in weekly],
            "ds": rows[-1][0][:10],
            "ws": week_key(rows[-1][0]),
            "f3": round(flows.get(it["code"], {}).get("s3", 0.0), 1),
            "f5": round(flows.get(it["code"], {}).get("s5", 0.0), 1),
            "f10": round(flows.get(it["code"], {}).get("s10", 0.0), 1),
            "sb": sbl3_scan(rows),
        })

    print("      拉取指数日线（上证 / 创业板）", flush=True)
    index_meta = {
        "sh": {"code": "000001", "name": "上证指数", **fetch_index_tail("1.000001")},
        "cy": {"code": "399006", "name": "创业板指", **fetch_index_tail("0.399006")},
    }

    payload = {
        "meta": {
            "built_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data_date": qdate,
            "source": "东方财富 push2 快照 + push2his 日线",
            "total": len(codes),
            "skipped": skipped,
            "rule": "策略v2：进攻档（上证与创业板均在MA20上）才开新仓；"
                    "买点=周线KDJ(9,3,3)凹底回勾 + 站上17日线 + MA5>MA17；"
                    "排序=距MA20由远到近（趋势强度）",
            "index": index_meta,
        },
        "codes": codes,
    }
    DIST_DATA.mkdir(parents=True, exist_ok=True)
    out = DIST_DATA / "base.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    size = out.stat().st_size / 1024 / 1024
    print(f"完成：{len(codes)} 只（跳过数据不足 {skipped} 只），{out} {size:.1f}MB，"
          f"耗时 {time.time() - t0:.0f}s", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="构建 ETF 买点工作台静态数据")
    ap.add_argument("--force", action="store_true", help="忽略缓存全量重抓日线")
    args = ap.parse_args()
    try:
        build(args.force)
    except requests.RequestException as exc:
        print(f"数据源请求失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
