#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略 v2 模拟盘记录器（本地，不接券商）

每个交易日收盘后跑一次：
  - 判定档位（上证/创业板 vs 动态 MA20）
  - 在策略池内算博主第一买点，按距 MA20 降序取前 2 作为「明日待入场」
  - 执行昨日待入场（今日收盘价成交，需仍为进攻档且未破 MA17）
  - 持仓检查：收盘破 MA17 → 次日离场（本脚本在收盘后跑，记为次日执行，价格取次日收盘）
  - 把当日档位、信号、动作、持仓写入 data/sim_daily.jsonl，状态存 data/sim_state.json

用法：
    python3 workbench/simulate.py            # 记录当日
    python3 workbench/simulate.py --status   # 只看当前状态
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import requests

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
CACHE = PROJECT / "data" / "etf_hist_cache"
STATE_FILE = PROJECT / "data" / "sim_state.json"
LOG_FILE = PROJECT / "data" / "sim_daily.jsonl"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
MAX_POS = 2
MAX_HOLD = 45

sys.path.insert(0, str(ROOT))
import signal_engine as se  # noqa: E402
from build_data import asset_type, read_cache  # noqa: E402


def fetch_spot() -> dict:
    FS = "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827"
    from concurrent.futures import ThreadPoolExecutor

    def page(pn):
        p = {"pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2, "fid": "f12",
             "fs": FS, "fields": "f12,f14,f2,f3,f6,f21,f402,f297"}
        return requests.get("https://push2.eastmoney.com/api/qt/clist/get",
                            params=p, headers=HEADERS, timeout=20).json()["data"]["diff"]

    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = [r for part in ex.map(page, range(1, 18)) for r in part]
    return {r["f12"]: r for r in rows if r.get("f12")}


def fetch_index() -> dict:
    p = {"secids": "1.000001,0.399006", "fltt": 2, "invt": 2, "fields": "f2,f12"}
    diff = requests.get("https://push2.eastmoney.com/api/qt/ulist.np/get",
                        params=p, headers=HEADERS, timeout=15).json()["data"]["diff"]
    return {r["f12"]: float(r["f2"]) for r in diff}


def index_closes(code: str, prefix: str, qdate: str) -> tuple[list[float], str]:
    """读取指数日线；缓存最后日期早于行情日则先补抓。"""
    d = PROJECT / "data" / "index_hist_cache"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{code}.csv"
    need = True
    if f.exists():
        lines = f.read_text().strip().split("\n")[1:]
        if lines and lines[-1].split(",")[0] >= qdate:
            need = False
    if need:
        params = {"secid": f"{prefix}.{code}", "klt": 101, "fqt": 1,
                  "beg": "20250101", "end": "20500101",
                  "fields1": "f1,f2", "fields2": "f51,f52,f53,f54,f55"}
        data = requests.get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                            params=params, headers=HEADERS, timeout=20).json()["data"]
        rows = []
        for line in data.get("klines") or []:
            p = line.split(",")
            rows.append([p[0], p[1], p[2], p[3], p[4]])
        with f.open("w", encoding="utf-8") as fh:
            fh.write("date,open,close,high,low\n")
            for r in rows:
                fh.write(",".join(r) + "\n")
    lines = f.read_text().strip().split("\n")[1:]
    rows = [l.split(",") for l in lines if l]
    return [float(r[2]) for r in rows], (rows[-1][0] if rows else "")


def live_ma20(closes: list[float], last_date: str, price: float, qdate: str) -> float | None:
    if not closes:
        return None
    arr = closes[:-1] if last_date == qdate else closes[:]
    arr = arr[-19:] + [price]
    return sum(arr) / len(arr) if len(arr) >= 20 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {
        "positions": [], "pending": [], "last_run": "", "closed": []}

    if args.status:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    spot = fetch_spot()
    qdate = ""
    for r in spot.values():
        raw = str(r.get("f297") or "")
        if len(raw) == 8:
            qdate = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
            break

    # 档位
    idx = fetch_index()
    sh_closes, sh_last = index_closes("000001", "1", qdate)
    cy_closes, cy_last = index_closes("399006", "0", qdate)
    sh_ma = live_ma20(sh_closes, sh_last, idx.get("000001", 0), qdate)
    cy_ma = live_ma20(cy_closes, cy_last, idx.get("399006", 0), qdate)
    if not sh_ma or not cy_ma:
        print("指数数据不足，跳过", file=sys.stderr)
        return 1
    sh_up = idx["000001"] >= sh_ma
    cy_up = idx["399006"] >= cy_ma
    tier = "进攻" if sh_up and cy_up else ("中性" if sh_up or cy_up else "防守")

    # 策略池 + 信号
    pool, signals = [], []
    for code, r in spot.items():
        name = str(r.get("f14") or "")
        price = float(r.get("f2") or 0)
        amount = float(r.get("f6") or 0)
        scale = float(r.get("f21") or 0)
        pm = None if r.get("f402") in (None, "-", 0) else -float(r["f402"])
        if price <= 0 or asset_type(name) not in ("宽基", "行业主题"):
            continue
        if scale < 1e9 or amount < 1e7:
            continue
        if pm is not None and pm > 3:
            continue
        rows = read_cache(code)
        if not rows or len(rows) < 130:
            continue
        d = np.array([x[0] for x in rows], dtype="datetime64[D]")
        o = np.array([x[1] for x in rows], float)
        h = np.array([x[2] for x in rows], float)
        l = np.array([x[3] for x in rows], float)
        c = np.array([x[4] for x in rows], float)
        weekly = se.weekly_ohlc(d, o, h, l, c)
        if str(d[-1]) == qdate:
            d, o, h, l, c = d[:-1], o[:-1], h[:-1], l[:-1], c[:-1]
        q = {"code": code, "name": name, "price": price, "pct": float(r.get("f3") or 0),
             "amount": amount, "high": price, "low": price, "qdate": qdate, "asset": asset_type(name)}
        m = se.evaluate({"dates": d, "o": o, "h": h, "l": l, "c": c, "v": None}, weekly, q)
        if m.get("ma17") is None:
            continue
        close_eff = np.append(c, price)
        ma20 = float(close_eff[-20:].mean())
        dist20 = (price / ma20 - 1) * 100
        pool.append({"code": code, "name": name, "price": price, "amount": amount,
                     "scale": scale, "premium": pm, "ma17": m["ma17"], "ma20": ma20,
                     "j": m["j_last"], "dist20": dist20,
                     "buy_ready": bool(m.get("buy_ready")), "near": bool(m.get("near_buy"))})
        if m.get("buy_ready") and (m.get("j_last") or 0) < 80:
            signals.append(pool[-1])

    signals.sort(key=lambda x: x["dist20"], reverse=True)

    # 昨日待入场：需今日仍为进攻档且未破 MA17
    actions = []
    held_codes = {p["code"] for p in state["positions"]}
    for pend in state.get("pending", []):
        if pend["code"] in held_codes or len(state["positions"]) >= MAX_POS:
            continue
        today = next((p for p in pool if p["code"] == pend["code"]), None)
        if tier != "进攻":
            actions.append({"type": "skip", "code": pend["code"], "name": pend["name"],
                            "reason": f"档位转为{tier}"})
            continue
        if not today or today["price"] < today["ma17"]:
            actions.append({"type": "skip", "code": pend["code"], "name": pend["name"],
                            "reason": "已破 MA17"})
            continue
        state["positions"].append({
            "code": pend["code"], "name": pend["name"], "entry_date": qdate,
            "entry_price": today["price"], "entry_idx": qdate,
        })
        held_codes.add(pend["code"])
        actions.append({"type": "buy", "code": pend["code"], "name": pend["name"],
                        "price": today["price"]})

    # 持仓检查：昨日触发卖出条件 → 今日收盘执行；今日触发 → 标记待明日执行
    remain = []
    for pos in state["positions"]:
        today = next((p for p in pool if p["code"] == pos["code"]), None)
        if pos.get("exit_pending") and today:
            pnl = (today["price"] / pos["entry_price"] - 1) * 100
            state.setdefault("closed", []).append({
                "code": pos["code"], "name": pos["name"], "entry_date": pos["entry_date"],
                "entry_price": pos["entry_price"], "exit_date": qdate,
                "exit_price": today["price"], "pnl": round(pnl, 2),
                "reason": pos.get("exit_reason", "离场"),
            })
            actions.append({"type": "sell", "code": pos["code"], "name": pos["name"],
                            "price": today["price"], "pnl": round(pnl, 2),
                            "reason": pos.get("exit_reason", "离场")})
            continue
        if not today:
            remain.append(pos)
            continue
        try:
            held_days = (dt.date.fromisoformat(qdate) - dt.date.fromisoformat(pos["entry_date"])).days
        except ValueError:
            held_days = 0
        if today["price"] < today["ma17"]:
            pos["exit_pending"] = True
            pos["exit_reason"] = "破MA17"
            actions.append({"type": "exit_signal", "code": pos["code"], "name": pos["name"],
                            "reason": "破MA17，次日收盘离场"})
        elif held_days >= MAX_HOLD:
            pos["exit_pending"] = True
            pos["exit_reason"] = "到期"
            actions.append({"type": "exit_signal", "code": pos["code"], "name": pos["name"],
                            "reason": "满45个交易日，次日收盘离场"})
        remain.append(pos)
    state["positions"] = remain

    # 今日新信号 → 明日待入场（仅进攻档）
    state["pending"] = [] if tier != "进攻" else [
        {"code": s["code"], "name": s["name"], "signal_date": qdate} for s in signals[:MAX_POS]
    ]

    record = {
        "date": qdate, "tier": tier,
        "sh": {"price": round(idx["000001"], 2), "ma20": round(sh_ma, 2), "above": bool(sh_up)},
        "cy": {"price": round(idx["399006"], 2), "ma20": round(cy_ma, 2), "above": bool(cy_up)},
        "pool": len(pool),
        "signals": [{"code": s["code"], "name": s["name"], "dist20": round(s["dist20"], 2),
                     "j": round(s["j"], 1)} for s in signals[:10]],
        "actions": actions,
        "positions": state["positions"],
        "closed_count": len(state.get("closed", [])),
    }
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 同一天重复运行只保留最后一次记录
    old = []
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                if json.loads(line).get("date") != qdate:
                    old.append(line)
            except json.JSONDecodeError:
                continue
    old.append(json.dumps(record, ensure_ascii=False))
    LOG_FILE.write_text("\n".join(old) + "\n", encoding="utf-8")
    state["last_run"] = qdate
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{qdate} 档位 {tier}｜池 {len(pool)} 只｜买点 {len(signals)} 个｜待入场 "
          f"{len(state['pending'])}｜持仓 {len(state['positions'])}")
    if actions:
        for a in actions:
            print("  ", json.dumps(a, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
