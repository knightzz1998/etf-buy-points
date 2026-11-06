import { evaluate, applyAmountScore, SIGNAL_ORDER, SIGNAL_ICON } from "./engine.js";

const SPOT_URL = "https://push2.eastmoney.com/api/qt/clist/get";
const IDX_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get";
const IDX_SECIDS = "1.000001,0.399006";
const FS = "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827";
const FIELDS = "f12,f14,f2,f3,f6,f8,f10,f15,f16,f17,f18,f21,f62,f402,f124,f297";
const PAGE_SIZE = 100;
const CONCURRENCY = 4;

const state = {
  base: null,
  quotes: new Map(),
  rows: [],
  filter: "all",
  asset: "all",
  minAmount: 10000000,
  search: "",
  sortKey: "signal",
  sortDir: 1,
  tier: null,
  indexQuotes: new Map(),
  view: "board",
  positions: [],
  watch: new Set(),
  interval: 180,
  openCode: null,
  dedup: true,
  poolOnly: true,
  quoteAt: null,
  quoteDate: "",
  refreshing: false,
  countdown: 180,
};

const $ = (id) => document.getElementById(id);
const el = {
  tbody: $("tbody"), empty: $("empty"), banner: $("banner"),
  quoteTime: $("quote-time"), baseDate: $("base-date"), footDate: $("foot-date"),
  footTime: $("foot-time"), session: $("session"), counter: $("counter"),
  tier: $("tier"), tierNote: $("tier-note"),
  resultCount: $("result-count"), refreshBtn: $("refresh-btn"),
  refreshLabel: $("refresh-label"), drawer: $("drawer"), mask: $("drawer-mask"),
};

const num = (v) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

const fmt = (v, digits = 3) => (v == null || !Number.isFinite(v) ? "—" : v.toFixed(digits));
const fmtPct = (v, digits = 2) => (v == null || !Number.isFinite(v) ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`);
const fmtInflow = (v) => {
  if (!v) return "—";
  const abs = Math.abs(v);
  const body = abs >= 1e8 ? `${(abs / 1e8).toFixed(2)} 亿` : `${(abs / 1e4).toFixed(0)} 万`;
  return (v > 0 ? "+" : "-") + body;
};

const fmtAmount = (v) => {
  if (!v) return "—";
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)} 亿`;
  return `${(v / 1e4).toFixed(0)} 万`;
};

function shrTime(ts) {
  if (!ts) return "--:--:--";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", hour12: false,
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(new Date(ts * 1000));
}

function shanghaiNow() {
  const text = new Date().toLocaleString("sv-SE", { timeZone: "Asia/Shanghai" });
  return new Date(text.replace(" ", "T"));
}

function marketSession() {
  const d = shanghaiNow();
  const day = d.getDay();
  const mins = d.getHours() * 60 + d.getMinutes();
  if (day === 0 || day === 6) return { label: "休市", cls: "closed" };
  if (mins >= 570 && mins <= 690) return { label: "盘中", cls: "live" };
  if (mins > 690 && mins < 780) return { label: "午间休市", cls: "closed" };
  if (mins >= 780 && mins <= 900) return { label: "盘中", cls: "live" };
  if (mins < 570) return { label: "未开盘", cls: "closed" };
  return { label: "已收盘", cls: "closed" };
}

/* ---------------- 行情 ---------------- */

async function fetchPage(pn) {
  const params = new URLSearchParams({
    pn: String(pn), pz: String(PAGE_SIZE), po: "1", np: "1",
    fltt: "2", invt: "2", fid: "f12", fs: FS, fields: FIELDS, _: String(Date.now()),
  });
  const res = await fetch(`${SPOT_URL}?${params}`, { credentials: "omit", cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return data?.data?.diff ?? [];
}

async function fetchAllPages() {
  const first = await fetchPage(1);
  const pages = [];
  pages.push(first);
  // 首包不足一页说明只有这些
  if (first.length < PAGE_SIZE) return pages.flat();
  let next = 2;
  let stop = false;
  const worker = async () => {
    while (!stop) {
      const pn = next++;
      let part;
      try {
        part = await fetchPage(pn);
      } catch (err) {
        stop = true;
        throw err;
      }
      if (!part.length) { stop = true; return; }
      pages.push(part);
      if (part.length < PAGE_SIZE) stop = true;
    }
  };
  await Promise.all(Array.from({ length: CONCURRENCY }, worker));
  return pages.flat();
}

async function fetchIndexQuotes() {
  const params = new URLSearchParams({
    secids: IDX_SECIDS, fltt: "2", invt: "2", fields: "f2,f3,f12,f14", _: String(Date.now()),
  });
  const res = await fetch(`${IDX_URL}?${params}`, { credentials: "omit", cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  const map = new Map();
  for (const r of data?.data?.diff ?? []) map.set(String(r.f12), { price: num(r.f2), pct: num(r.f3), name: r.f14 });
  return map;
}

function liveMa20(idx, price, qdate) {
  if (!idx || !idx.d || !idx.d.length || !price) return null;
  const closes = idx.d.slice();
  if (idx.ds === qdate) closes.pop();
  const arr = closes.slice(-19).concat([price]);
  if (arr.length < 20) return null;
  return arr.reduce((a, b) => a + b, 0) / arr.length;
}

function computeTier(qdate) {
  const meta = state.base?.meta?.index;
  const sh = state.indexQuotes.get("000001");
  const cy = state.indexQuotes.get("399006");
  if (!meta || !sh || !cy) return null;
  const shMa = liveMa20(meta.sh, sh.price, qdate);
  const cyMa = liveMa20(meta.cy, cy.price, qdate);
  if (!shMa || !cyMa) return null;
  const shUp = sh.price >= shMa;
  const cyUp = cy.price >= cyMa;
  return {
    name: shUp && cyUp ? "进攻" : shUp || cyUp ? "中性" : "防守",
    sh: { price: sh.price, ma20: shMa, above: shUp },
    cy: { price: cy.price, ma20: cyMa, above: cyUp },
  };
}

function renderTier() {
  const t = state.tier;
  el.tier.dataset.t = t ? t.name : "";
  el.tier.innerHTML = `<span>档位</span><b>${t ? t.name : "--"}</b>`;
  if (t) {
    el.tier.title = `上证 ${t.sh.price.toFixed(2)} vs MA20 ${t.sh.ma20.toFixed(2)}（${t.sh.above ? "上" : "下"}）；`
      + `创业板 ${t.cy.price.toFixed(2)} vs MA20 ${t.cy.ma20.toFixed(2)}（${t.cy.above ? "上" : "下"}）`;
  }
  if (t && t.name !== "进攻") {
    el.tierNote.hidden = false;
    el.tierNote.innerHTML = `<b>当前${t.name}档，不新开仓。</b>
      上证 ${t.sh.price.toFixed(2)} 在 MA20 ${t.sh.above ? "上" : "下"}方，创业板 ${t.cy.price.toFixed(2)} 在 MA20 ${t.cy.above ? "上" : "下"}方。
      回测：博主买点只在进攻档有效（62 笔 / 胜率 43.5% / 均 +1.46%），非进攻档开仓为负期望；当前只做持仓管理——破 17 日线离场、破 5 日线减仓。`;
  } else {
    el.tierNote.hidden = true;
  }
}

function normalizeQuote(r) {
  const raw = String(r.f297 ?? "");
  const date = raw.length === 8 ? `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6)}` : "";
  const hasPremium = r.f402 !== "-" && r.f402 != null;
  return {
    name: String(r.f14 || ""),
    price: num(r.f2), pct: num(r.f3), amount: num(r.f6), turnover: num(r.f8), liangbi: num(r.f10),
    scale: num(r.f21),
    high: num(r.f15), low: num(r.f16), open: num(r.f17), prev: num(r.f18),
    mainInflow: num(r.f62),
    premium: hasPremium ? -num(r.f402) : null,
    date, ts: num(r.f124),
  };
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  el.refreshBtn.classList.add("loading");
  el.refreshBtn.disabled = true;
  el.refreshLabel.textContent = "刷新中";
  try {
    const raw = await fetchAllPages();
    try {
      state.indexQuotes = await fetchIndexQuotes();
    } catch (err) {
      state.indexQuotes = new Map();
    }
    const map = new Map();
    let maxTs = 0;
    let date = "";
    for (const r of raw) {
      const code = String(r.f12 ?? "");
      if (!code) continue;
      const q = normalizeQuote(r);
      map.set(code, q);
      if (q.ts > maxTs) maxTs = q.ts;
      if (!date && q.date) date = q.date;
    }
    if (map.size < 500) throw new Error(`仅返回 ${map.size} 只行情`);
    const now = new Date();
    state.quotes = map;
    state.quoteAt = now;
    state.quoteDate = date;
    document.body.dataset.quotes = String(map.size);
    document.body.dataset.quoteDate = date;
    state.tier = computeTier(date);
    // 每次拿到新报价都重算指标，否则看板会停在首屏的烘焙价
    state.rows = computeRows();
    hideBanner();
    checkStale();
    updateClock(maxTs);
    renderTier();
    render();
    renderPositions();
    updatePosBadge();
  } catch (err) {
    const shown = state.quoteAt
      ? `当前显示 ${state.quoteAt.toLocaleTimeString("zh-CN", { hour12: false })} 取的快照`
      : "当前显示构建期烘焙数据";
    showBanner(`行情刷新失败：${err.message}。${shown}，指标仍按最近一次价格计算。`);
  } finally {
    state.refreshing = false;
    el.refreshBtn.classList.remove("loading");
    el.refreshBtn.disabled = false;
    el.refreshLabel.textContent = "刷新";
    state.countdown = state.interval || 180;
  }
}

function checkStale() {
  const baseDate = state.base?.meta?.data_date;
  if (!baseDate || !state.quoteDate) return;
  const gap = (new Date(state.quoteDate) - new Date(baseDate)) / 86400000;
  if (gap > 3) {
    showBanner(`指标基准日 ${baseDate} 已落后于行情日 ${state.quoteDate}。实时价只补当日，缺失的交易日会让均线与周线 J 失真，请重新构建数据后再看。`);
  }
}

function showBanner(text) {
  el.banner.textContent = text;
  el.banner.hidden = false;
}
function hideBanner() { el.banner.hidden = true; }

/* ---------------- 计算与渲染 ---------------- */

function computeRows() {
  const meta = state.base.meta;
  const rows = state.base.codes.map((rec) => {
    const q = state.quotes.get(rec.c);
    const quote = q
      ? { ...q, date: q.date || state.quoteDate || rec.ds }
      : {
        price: rec.p, pct: rec.pct, amount: rec.amt, turnover: rec.to,
        liangbi: rec.lb, mainInflow: rec.mf, premium: rec.pm, scale: 0,
        high: rec.p, low: rec.p, date: meta.data_date,
      };
    if (!quote.amount) quote.amount = rec.amt;
    return evaluate(rec, quote);
  });
  applyAmountScore(rows);
  return rows;
}

function dedupByGroup(list) {
  const map = new Map();
  for (const r of list) {
    const key = r.group || r.name;
    const cur = map.get(key);
    if (!cur) {
      r.peers = 1;
      map.set(key, r);
    } else {
      cur.peers += 1;
      if (r.amount > cur.amount) {
        r.peers = cur.peers;
        map.set(key, r);
      }
    }
  }
  return [...map.values()];
}

function searching() {
  return state.search.trim().length > 0;
}

function matchBase(r) {
  const s = state.search.trim().toLowerCase();
  // 搜索直达：命中名称或代码就显示，不受类型/成交额/信号筛选影响
  if (s) return r.name.toLowerCase().includes(s) || r.code.includes(s);
  if (state.asset !== "all" && r.asset !== state.asset) return false;
  if ((r.amountRef ?? r.amount) < state.minAmount) return false;
  if (state.poolOnly) {
    if (!["宽基", "行业主题"].includes(r.asset)) return false;
    if (r.premium != null && r.premium > 3) return false;
    if (r.scale > 0 && r.scale < 1e9) return false;
  }
  return true;
}

function matchSignal(r) {
  if (searching()) return true;
  switch (state.filter) {
    case "buy": return r.signal === "buy";
    case "add": return r.signal === "add";
    case "trim": return r.signal === "trim";
    case "exit": return r.signal === "exit";
    case "near": return r.nearBuy;
    case "watch": return state.watch.has(r.code);
    case "myrule": return myRule(r).pass;
    case "opportunity": return r.signal === "buy" || r.nearBuy;
    default: return true;
  }
}

function filteredRows() {
  return state.rows.filter((r) => matchBase(r) && matchSignal(r));
}

const SORT_DEFAULT_DIR = {
  signal: 1, name: 1, price: -1, pct: -1, dist17: -1, dist20: -1,
  met: -1, j: 1, amount: -1, inflow: -1, flow3: -1, flowsig: -1,
  ma5: -1, ma10: -1, ma60: -1, order: -1, macd: -1, myrule: -1, premium: -1, r30: -1,
};

function sortValue(r, key) {
  switch (key) {
    case "signal": return SIGNAL_ORDER[r.signal] ?? 9;
    case "name": return r.name;
    case "price": return r.price;
    case "pct": return r.pct;
    case "dist17": return r.distMa17 ?? -999;
    case "dist20": return r.distMa20 ?? -999;
    case "met": return r.met;
    case "j": return r.jLast ?? 999;
    case "amount": return r.amount;
    case "inflow": return r.mainInflow ?? 0;
    case "flow3": return r.flow3 ?? 0;
    case "flowsig": return flowSignal(r).rank;
    case "ma5": return r.distMa5 ?? -999;
    case "ma10": return r.distMa10 ?? -999;
    case "ma60": return r.distMa60 ?? -999;
    case "order": return r.order === "多头" ? 2 : r.order === "空头" ? 0 : 1;
    case "macd": return r.macdRank ?? -1;
    case "myrule": return myRule(r).rank;
    case "premium": return r.premium ?? -999;
    case "r30": return r.r30 ?? -999;
    default: return 0;
  }
}

function applySort(list) {
  const key = state.sortKey;
  const dir = state.sortDir;
  list.sort((a, b) => {
    const va = sortValue(a, key);
    const vb = sortValue(b, key);
    let c = typeof va === "string" ? va.localeCompare(vb, "zh-CN") : (va - vb);
    if (c === 0) c = (b.strength ?? -99) - (a.strength ?? -99);
    return c * dir;
  });
}

function renderSortIndicators() {
  document.querySelectorAll(".grid thead th[data-key]").forEach((th) => {
    const on = th.dataset.key === state.sortKey;
    th.classList.toggle("sorted", on);
    th.dataset.dir = on ? (state.sortDir > 0 ? "asc" : "desc") : "";
  });
}

function sparkline(values, { width = 88, height = 26, color } = {}) {
  if (!values || values.length < 2) return "";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) => [
    (i / (values.length - 1)) * width,
    height - 3 - ((v - min) / span) * (height - 6),
  ]);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  return `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" aria-hidden="true">
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="1.8" fill="${color}"/>
  </svg>`;
}

function sparkColor(r) {
  const first = r.spark[0];
  return r.price >= first ? "var(--up)" : "var(--down)";
}

function flowLines(r) {
  const items = [["3日", r.flow3], ["5日", r.flow5], ["10日", r.flow10]];
  if (items.every(([, v]) => v == null)) return '<span class="dash">—</span>';
  return items.map(([label, v]) => {
    const c = v > 0 ? "pos" : v < 0 ? "neg" : "dash";
    const arrow = v > 0 ? "↑ " : v < 0 ? "↓ " : "";
    return `<span class="${c}">${label} ${arrow}${fmtInflow(v)}</span>`;
  }).join("<br>");
}

// 资金方向：3/5/10 日同号判定趋势，再看日均强度给出加速/放缓
function flowSignal(r) {
  const a = r.flow3, b = r.flow5, c = r.flow10;
  if (a == null || b == null || c == null) return { text: "—", mark: "", cls: "", rank: -1 };
  let text, cls, rank;
  if (a > 0 && b > 0 && c > 0) { text = "持续流入"; cls = "in"; rank = 4; }
  else if (a < 0 && b < 0 && c < 0) { text = "持续流出"; cls = "out"; rank = 0; }
  else if (a > 0 && b <= 0) { text = "转为流入"; cls = "in"; rank = 3; }
  else if (a < 0 && b >= 0) { text = "转为流出"; cls = "out"; rank = 1; }
  else { text = "资金分歧"; cls = "mix"; rank = 2; }
  let pace = "";
  const avg3 = a / 3;
  const avg5 = b / 5;
  if (cls !== "mix" && Math.abs(avg5) > 0) {
    const ratio = Math.abs(avg3) / Math.abs(avg5);
    pace = ratio >= 1.2 ? "加速" : ratio <= 0.8 ? "放缓" : "平稳";
  }
  const mark = cls === "in" ? "↑" : cls === "out" ? "↓" : "~";
  return { text, mark, cls, pace, rank };
}

// 用户自己的买入标准：站稳17日线 + 资金流入(3日) + 当前为进攻档
function myRule(r) {
  const c17 = !!r.stable17;
  const cf = r.flow3 != null && r.flow3 > 0;
  const ct = !!(state.tier && state.tier.name === "进攻");
  const missing = [];
  if (!c17) missing.push("17日线");
  if (!cf) missing.push("资金");
  if (!ct) missing.push("档位");
  return { pass: c17 && cf && ct, c17, cf, ct, missing, rank: Number(c17) + Number(cf) + Number(ct) };
}

function myRuleBadge(r) {
  const m = myRule(r);
  if (m.pass) return '<span class="ma-badge up">✓ 可买</span>';
  return `<span class="ma-badge flat">缺 ${m.missing.join(" / ")}</span>`;
}

function orderBadge(r) {
  if (!r.order || r.order === "—") return '<span class="ma-badge flat">—</span>';
  const cls = r.order === "多头" ? "up" : r.order === "空头" ? "down" : "flat";
  const mark = r.order === "多头" ? "↗" : r.order === "空头" ? "↘" : "~";
  return `<span class="ma-badge ${cls}">${mark} ${r.order}</span>`;
}

function macdBadge(r) {
  if (!r.macdState) return '<span class="ma-badge flat">—</span>';
  const cls = r.macdState === "金叉" || r.macdState === "多头" ? "up" : "down";
  const dot = r.macdState === "金叉" || r.macdState === "死叉" ? "● " : "";
  return `<span class="ma-badge ${cls}">${dot}${r.macdState}</span>`;
}

function flowBadge(r) {
  const f = flowSignal(r);
  if (!f.text || f.text === "—") return '<span class="flow-badge mix">—</span>';
  return `<span class="flow-badge ${f.cls}">${f.mark} ${f.text}</span>${f.pace ? `<span class="flow-pace">${f.pace}</span>` : ""}`;
}

function condTags(r) {
  const c = r.cond;
  const item = (on, text) => `<i class="${on ? "on" : ""}">${text}</i>`;
  return `<span class="cond">
    ${item(c.jTrough, "凹底")}
    ${item(c.jUp, "回勾")}
    ${item(c.aboveMa17, "站17")}
    ${item(c.ma5AboveMa17, "5上17")}
  </span>`;
}

function gated(r) {
  return state.tier && state.tier.name !== "进攻" && (r.signal === "buy" || r.signal === "add");
}

function rowHtml(r) {
  const pctCls = r.pct > 0 ? "up" : r.pct < 0 ? "down" : "flat";
  const distCls = (r.distMa17 ?? 0) > 0 ? "pos" : "neg";
  const jCls = r.jOverbought ? "hot" : (r.jLast != null && r.jLast <= 20 ? "cold" : "");
  const premium = r.premium;
  const premiumCell = premium == null
    ? `<td class="col-num col-premium premium zero">—</td>`
    : `<td class="col-num col-premium premium ${Math.abs(premium) >= 3 ? "warn" : ""}">${fmtPct(premium, 2)}</td>`;
  return `<tr data-code="${esc(r.code)}" class="${r.signal === "buy" ? "row-buy" : ""}">
    <td><span class="sig" data-s="${r.signal}">${SIGNAL_ICON[r.signal]} ${r.signalText}${r.nearBuy ? '<span class="near-tag">接近</span>' : ""}${gated(r) ? '<span class="gate-tag">不新开</span>' : ""}</span></td>
    <td><div class="name-cell"><b><button class="star ${state.watch.has(r.code) ? "on" : ""}" data-star="${r.code}" title="加入自选">★</button>${esc(r.name)}</b><span>${r.code} · ${esc(r.asset)}${r.peers > 1 ? ` · <b class="peer">同类 ${r.peers}</b>` : ""}</span></div></td>
    <td class="col-num">${fmt(r.price)}</td>
    <td class="col-num pct ${pctCls}">${fmtPct(r.pct)}</td>
    <td class="col-num dist ${distCls}">${fmtPct(r.distMa17, 1)}</td>
    <td class="col-num dist ${(r.distMa20 ?? 0) > 0 ? "pos" : "neg"}">${fmtPct(r.distMa20, 1)}</td>
    <td class="col-num"><span class="dstack"><span>${fmt(r.ma5)}</span><em>${fmtPct(r.distMa5, 1)}</em></span></td>
    <td class="col-num"><span class="dstack"><span>${fmt(r.ma10)}</span><em>${fmtPct(r.distMa10, 1)}</em></span></td>
    <td class="col-num"><span class="dstack"><span>${fmt(r.ma60)}</span><em>${r.distMa60 == null ? "—" : fmtPct(r.distMa60, 1)}</em></span></td>
    <td>${orderBadge(r)}</td>
    <td>${macdBadge(r)}</td>
    <td>${myRuleBadge(r)}</td>
    <td class="col-cond">${condTags(r)}</td>
    <td class="col-num"><span class="jcell ${jCls}"><b>${r.jLast == null ? "—" : r.jLast.toFixed(0)}</b><span>谷 ${r.jMin6 == null ? "—" : r.jMin6.toFixed(0)}</span></span></td>
    <td class="col-num amount">${fmtAmount(r.amount)}</td>
    <td class="col-num col-inflow inflow ${(r.mainInflow ?? 0) > 0 ? "pos" : (r.mainInflow ?? 0) < 0 ? "neg" : ""}">${fmtInflow(r.mainInflow)}</td>
    <td class="col-num col-flow flowcell">${flowLines(r)}</td>
    <td class="col-flow">${flowBadge(r)}</td>
    ${premiumCell}
    <td class="col-spark spark">${sparkline(r.spark, { color: sparkColor(r) })}</td>
  </tr>`;
}

function renderStats() {
  const pool = state.rows.filter(matchBase);
  const count = (fn) => pool.filter(fn).length;
  $("stat-total").textContent = pool.length;
  $("stat-buy").textContent = count((r) => r.signal === "buy");
  $("stat-add").textContent = count((r) => r.signal === "add");
  $("stat-trim").textContent = count((r) => r.signal === "trim");
  $("stat-exit").textContent = count((r) => r.signal === "exit");
  $("stat-near").textContent = count((r) => r.nearBuy);
  document.querySelectorAll(".tile").forEach((t) => {
    const f = t.dataset.filter;
    const map = { all: "all", buy: "buy", add: "add", trim: "trim", exit: "exit", near: "near" };
    t.classList.toggle("active", map[f] === state.filter);
  });
}

function render() {
  if (!state.base) return;
  renderStats();
  const raw = filteredRows();
  const list = state.dedup ? dedupByGroup(raw) : raw;
  applySort(list);
  renderSortIndicators();
  const poolSize = state.rows.filter(matchBase).length;
  el.resultCount.textContent = `${list.length} / ${poolSize} 只${state.poolOnly ? "（策略池）" : ""}${state.dedup ? "（同赛道去重）" : ""}`;
  if (!list.length) {
    el.tbody.innerHTML = "";
    el.empty.hidden = false;
    const hints = {
      opportunity: "当前没有买点或接近买点的标的。<br>可以切到 <b>全部</b> 看多头趋势中的加仓机会，或等周线 J 回勾。",
      buy: "当前没有同时满足四项条件的第一买点。<br>看 <b>接近买点</b> 或 <b>机会</b> 视图等信号。",
      near: "没有差一项条件的标的。",
    };
    el.empty.innerHTML = hints[state.filter] || "当前筛选条件下没有标的，试试放宽条件。";
  } else {
    el.empty.hidden = true;
    el.tbody.innerHTML = list.map(rowHtml).join("");
    if (state.openCode) {
      const tr = el.tbody.querySelector(`tr[data-code="${state.openCode}"]`);
      if (tr) tr.classList.add("open");
    }
  }
  if (state.openCode) renderDrawer();
}

function updateClock(ts) {
  const text = ts
    ? shrTime(ts)
    : (state.quoteAt ? shrTime(state.quoteAt.getTime() / 1000) : "--:--:--");
  el.quoteTime.textContent = text;
  el.footTime.textContent = text;
  const s = marketSession();
  if (el.session.dataset.state !== s.label) {
    el.session.dataset.state = s.label;
    el.session.className = `session ${s.cls}`;
    el.session.innerHTML = `<i class="dot"></i><span>${s.label}</span>`;
  }
}

/* ---------------- 详情抽屉 ---------------- */

function chartSvg(r) {
  const values = r.spark;
  if (!values || values.length < 2) return "";
  const w = 340, h = 130, pad = 8;
  const min = Math.min(...values, r.ma17 ?? Infinity);
  const max = Math.max(...values, r.ma17 ?? -Infinity);
  const span = max - min || 1;
  const x = (i) => pad + (i / (values.length - 1)) * (w - pad * 2);
  const y = (v) => h - pad - ((v - min) / span) * (h - pad * 2);
  const pts = values.map((v, i) => [x(i), y(v)]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const area = `${line} L${x(values.length - 1).toFixed(1)} ${h - pad} L${x(0).toFixed(1)} ${h - pad} Z`;
  const up = r.price >= values[0];
  const color = up ? "var(--up)" : "var(--down)";
  const maLine = r.ma17 != null && r.ma17 >= min && r.ma17 <= max
    ? `<line x1="${pad}" x2="${w - pad}" y1="${y(r.ma17).toFixed(1)}" y2="${y(r.ma17).toFixed(1)}" stroke="var(--add)" stroke-width="1" stroke-dasharray="4 3" opacity=".7"/>
       <text x="${w - pad}" y="${(y(r.ma17) - 5).toFixed(1)}" text-anchor="end" class="axis" fill="var(--add)">MA17 ${fmt(r.ma17)}</text>`
    : "";
  const last = pts[pts.length - 1];
  return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="近30日走势">
    <defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${up ? "#ff5b5b" : "#22c58b"}" stop-opacity=".22"/>
      <stop offset="100%" stop-color="${up ? "#ff5b5b" : "#22c58b"}" stop-opacity="0"/>
    </linearGradient></defs>
    <path d="${area}" fill="url(#fill)"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round"/>
    ${maLine}
    <circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="2.6" fill="${color}"/>
    <text x="${pad}" y="12" class="axis">${max.toFixed(3)}</text>
    <text x="${pad}" y="${h - 2}" class="axis">${min.toFixed(3)}</text>
  </svg>`;
}

function condItem(pass, label, value) {
  return `<div class="cond-item ${pass ? "pass" : "fail"}">
    <span><span class="mark">${pass ? "✓" : "✗"}</span> ${label}</span>
    <span class="v">${value}</span>
  </div>`;
}

function renderDrawer() {
  const r = state.rows.find((x) => x.code === state.openCode);
  if (!r) { closeDrawer(); return; }
  const pctCls = r.pct > 0 ? "up" : r.pct < 0 ? "down" : "";
  const premiumWarn = r.premium != null && r.premium >= 3;
  const reasonCls = premiumWarn || r.signal === "exit" ? "reason warn" : "reason";
  const trendCls = r.trendLabel === "强势" || r.trendLabel === "偏强" ? "up"
    : r.trendLabel === "偏弱" || r.trendLabel === "走弱" ? "down" : "flat";
  const tierName = state.tier ? state.tier.name : null;
  const openSignal = r.signal === "buy" || r.signal === "add";
  const tierAction = !tierName
    ? "<b>档位：</b>未取到指数行情，无法判定档位。"
    : tierName === "进攻"
      ? (openSignal ? "<b>档位：</b>进攻档，可按信号开新仓（单只不超过 20%，同时最多 2 只）。"
                    : "<b>档位：</b>进攻档，但该标的当前不是开仓信号。")
      : `<b>档位：</b>当前${tierName}档，不新开仓；${openSignal ? "该标的有买点条件，但只在进攻档执行。" : "只做持仓管理。"}`;
  const premiumNote = premiumWarn
    ? `<div class="reason warn">溢价 ${fmtPct(r.premium)} ≥ 3%，按纪律禁止买入；技术信号建立在溢价虚高的价格上，需等溢价回落。</div>`
    : "";
  el.drawer.innerHTML = `
    <div class="dw-head">
      <div class="dw-title">
        <span class="sig" data-s="${r.signal}">${SIGNAL_ICON[r.signal]} ${r.signalText}${r.nearBuy ? '<span class="near-tag">接近</span>' : ""}</span>
        <h2>${esc(r.name)}</h2>
        <p>${r.code} · ${esc(r.asset)} · 行情 ${r.qdate || "—"}</p>
      </div>
      <button class="dw-close" type="button" aria-label="关闭">×</button>
    </div>
    <div class="dw-price">
      <strong>${fmt(r.price)}</strong>
      <span class="pct ${pctCls}">${fmtPct(r.pct)}</span>
    </div>
    <div class="dw-sub">${r.state} · ${esc(r.reason)}</div>

    <div class="dw-sec">
      <h3>四项条件</h3>
      <div class="cond-list">
        ${condItem(r.cond.jTrough, "周线 J 近 6 周谷底 ≤ 20", r.jMin6 == null ? "—" : r.jMin6.toFixed(1))}
        ${condItem(r.cond.jUp, "本周 J 回勾（高于上周）", r.jLast == null ? "—" : `${r.jPrev.toFixed(1)} → ${r.jLast.toFixed(1)}`)}
        ${condItem(r.cond.aboveMa17, "现价站上 17 日线", `MA17 ${fmt(r.ma17)}`)}
        ${condItem(r.cond.ma5AboveMa17, "5 日线在 17 日线上方", `MA5 ${fmt(r.ma5)}`)}
      </div>
    </div>

    <div class="dw-sec">
      <h3>近 30 个交易日</h3>
      <div class="dw-chart">${chartSvg(r)}</div>
    </div>

    <div class="dw-sec">
      <h3>我的买入标准</h3>
      <div class="cond-list">
        ${condItem(!!r.stable17, "站稳 17 日线（今日与昨日收盘均在上方）", "MA17 " + fmt(r.ma17))}
        ${condItem(r.flow3 != null && r.flow3 > 0, "资金流入（3 日主力净流入 > 0）", fmtInflow(r.flow3))}
        ${condItem(!!(state.tier && state.tier.name === "进攻"), "当前档位为进攻档", state.tier ? state.tier.name : "未知")}
      </div>
    </div>

    <div class="dw-sec">
      <h3>趋势分析</h3>
      <div class="trend-head">
        <span class="trend-badge ${trendCls}">${r.trendLabel || "—"}</span>
        <b>${r.trendScore == null ? "—" : r.trendScore}</b>
        <span class="dw-sub">/ 100</span>
      </div>
      <div class="trend-bar"><i class="${trendCls}" style="width:${r.trendScore || 0}%"></i></div>
      <div class="trend-lines">
        <div><span>均线排列</span><b>${r.order === "—" || !r.order ? "数据不足" : `${r.order}（MA5/17/20/60）`}</b></div>
        <div><span>MACD（12,26,9）</span><b>${r.macdState || "—"}${r.dif == null ? "" : ` ｜ DIF ${r.dif.toFixed(3)} / DEA ${r.dea.toFixed(3)}`}</b></div>
        <div><span>20 日线方向</span><b>${r.ma20Slope == null ? "—" : `${r.ma20Slope > 0 ? "向上" : r.ma20Slope < 0 ? "向下" : "走平"} ${fmtPct(r.ma20Slope, 2)}（5日）`}</b></div>
        <div><span>60 日位置</span><b>${r.pos60 == null ? "—" : `${r.pos60.toFixed(0)}% ｜ 距高 ${fmtPct(r.distHigh, 1)}`}</b></div>
        <div><span>资金 3/5/10 日</span><b>${fmtInflow(r.flow3)} / ${fmtInflow(r.flow5)} / ${fmtInflow(r.flow10)}</b></div>
      </div>
    </div>

    <div class="dw-sec">
      <h3>关键价位与量能</h3>
      <div class="kv">
        <div><span>17 日线（止损线）</span><b>${fmt(r.ma17)}</b></div>
        <div><span>距 17 日线</span><b class="${(r.distMa17 ?? 0) > 0 ? "up" : "down"}">${fmtPct(r.distMa17, 1)}</b></div>
        <div><span>5 日线（持股线）</span><b>${fmt(r.ma5)}</b></div>
        <div><span>10 日线</span><b>${fmt(r.ma10)}</b></div>
        <div><span>20 日线（趋势线）</span><b>${fmt(r.ma20)}</b></div>
        <div><span>距 20 日线（排序依据）</span><b class="${(r.distMa20 ?? 0) > 0 ? "up" : "down"}">${fmtPct(r.distMa20, 1)}</b></div>
        <div><span>60 日线</span><b>${fmt(r.ma60)}</b></div>
        <div><span>周线 J / 前值</span><b>${r.jLast == null ? "—" : `${r.jLast.toFixed(1)} / ${r.jPrev.toFixed(1)}`}</b></div>
        <div><span>回踩 17 日线触发</span><b>${r.pullbackHit}</b></div>
        <div><span>成交额</span><b>${fmtAmount(r.amount)}</b></div>
        <div><span>换手率</span><b>${r.turnover ? `${r.turnover.toFixed(2)}%` : "—"}</b></div>
        <div><span>溢价率</span><b class="${premiumWarn ? "up" : ""}">${r.premium == null ? "—" : fmtPct(r.premium)}</b></div>
        <div><span>主力净流入</span><b>${r.mainInflow ? `${(r.mainInflow / 1e4).toFixed(0)} 万` : "—"}</b></div>
      </div>
    </div>

    <div class="${reasonCls}">
      ${tierAction}<br>
      ${r.nearBuy ? `<b>接近买点：</b>${esc(r.nearReason)}<br>` : ""}
      <b>判定：</b>${esc(r.reason)}<br>
      <b>纪律：</b>收盘破 17 日线次日无条件离场；不破 5 日线持股；周线 J ≥ 80 卖一半；单只不超过 20% 仓位。
    </div>
    ${premiumNote}
    <div class="dw-note">
      盘中均线与周线 J 用实时价计算，属动态值，收盘后才作最终确认。
      数据来源：东方财富 push2 快照 / push2his 日线。本页仅作信号提示，不构成投资建议。
    </div>`;
  el.drawer.querySelector(".dw-close").addEventListener("click", closeDrawer);
  el.drawer.hidden = false;
  el.mask.hidden = false;
}

function closeDrawer() {
  state.openCode = null;
  el.drawer.hidden = true;
  el.mask.hidden = true;
  document.querySelectorAll(".grid tbody tr.open").forEach((tr) => tr.classList.remove("open"));
}

/* ---------------- 事件 ---------------- */

function syncChips() {
  document.querySelectorAll("#signal-chips .chip").forEach((c) => {
    c.classList.toggle("active", c.dataset.filter === state.filter);
  });
  if (!["opportunity", "all", "buy", "add", "trim", "exit"].includes(state.filter)) {
    document.querySelectorAll("#signal-chips .chip").forEach((c) => c.classList.remove("active"));
  }
  document.querySelectorAll("#asset-chips .chip").forEach((c) => {
    c.classList.toggle("active", c.dataset.asset === state.asset);
  });
}

function bind() {
  document.querySelectorAll(".tile").forEach((t) => {
    t.addEventListener("click", () => {
      state.filter = state.filter === t.dataset.filter ? "opportunity" : t.dataset.filter;
      syncChips();
      render();
    });
  });
  document.querySelectorAll("#signal-chips .chip").forEach((c) => {
    c.addEventListener("click", () => {
      state.filter = c.dataset.filter;
      syncChips();
      render();
    });
  });
  document.querySelectorAll("#asset-chips .chip").forEach((c) => {
    c.addEventListener("click", () => {
      state.asset = c.dataset.asset;
      syncChips();
      render();
    });
  });
  $("search").addEventListener("input", (e) => {
    state.search = e.target.value;
    render();
  });
  $("min-amount").addEventListener("change", (e) => {
    state.minAmount = Number(e.target.value);
    render();
  });
  document.querySelector(".grid thead").addEventListener("click", (e) => {
    const th = e.target.closest("th[data-key]");
    if (!th) return;
    const key = th.dataset.key;
    if (state.sortKey === key) {
      state.sortDir = -state.sortDir;
    } else {
      state.sortKey = key;
      state.sortDir = SORT_DEFAULT_DIR[key] ?? -1;
    }
    render();
  });
  $("dedup").addEventListener("change", (e) => {
    state.dedup = e.target.checked;
    render();
  });
  $("pool-only").addEventListener("change", (e) => {
    state.poolOnly = e.target.checked;
    render();
  });
  $("interval").addEventListener("change", (e) => {
    state.interval = Number(e.target.value);
    state.countdown = state.interval || 180;
    updateCounter();
  });
  el.refreshBtn.addEventListener("click", () => refresh());
  document.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => setView(t.dataset.view));
  });
  $("pos-form").addEventListener("submit", addPosition);
  $("pos-clear").addEventListener("click", () => {
    if (state.positions.length && confirm("清空全部持仓？")) {
      state.positions = [];
      savePositions();
      renderPositions();
    }
  });
  $("pos-tbody").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-del]");
    if (btn) removePosition(btn.dataset.del);
  });
  $("pos-export").addEventListener("click", exportPositions);
  $("pos-import").addEventListener("click", () => $("pos-file").click());
  $("pos-file").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    if (file) importPositions(file);
    e.target.value = "";
  });
  el.mask.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawer();
  });
  el.tbody.addEventListener("click", (e) => {
    const star = e.target.closest("[data-star]");
    if (star) {
      e.stopPropagation();
      const code = star.dataset.star;
      if (state.watch.has(code)) state.watch.delete(code);
      else state.watch.add(code);
      saveWatch();
      render();
      return;
    }
    const tr = e.target.closest("tr[data-code]");
    if (!tr) return;
    const code = tr.dataset.code;
    if (state.openCode === code) { closeDrawer(); return; }
    state.openCode = code;
    document.querySelectorAll(".grid tbody tr.open").forEach((x) => x.classList.remove("open"));
    tr.classList.add("open");
    renderDrawer();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    const age = state.quoteAt ? (Date.now() - state.quoteAt.getTime()) / 1000 : Infinity;
    if (age >= (state.interval || 180)) {
      state.countdown = state.interval || 180;
      refresh();
    } else {
      state.countdown = Math.max(1, Math.round((state.interval || 180) - age));
      updateCounter();
    }
  });
}

function updateCounter() {
  if (!state.interval) {
    el.counter.textContent = "自动刷新已暂停";
    return;
  }
  const s = Math.max(0, state.countdown);
  const label = s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : `${s}s`;
  el.counter.textContent = `下次刷新 ${label}`;
}

function tick() {
  const s = marketSession();
  if (el.session.dataset.cls !== s.cls) el.session.dataset.cls = s.cls;
  updateClock(null);
  if (!state.interval) { updateCounter(); return; }
  state.countdown -= 1;
  if (state.countdown <= 0) {
    state.countdown = state.interval;
    if (!document.hidden) refresh();
  }
  updateCounter();
}

async function boot() {
  bind();
  try {
    const res = await fetch("data/base.json", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.base = await res.json();
    el.baseDate.textContent = state.base.meta.data_date;
    el.footDate.textContent = state.base.meta.data_date;
  } catch (err) {
    showBanner(`基础数据加载失败：${err.message}。请确认 data/base.json 已生成。`);
    return;
  }
  state.rows = computeRows();
  state.positions = loadPositions();
  state.watch = loadWatch();
  renderTier();
  render();
  syncChips();
  updateClock(null);
  updateCounter();
  setInterval(tick, 1000);
  await refresh();
}

boot();


/* ---------------- 我的持仓 ---------------- */

const FLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get";
const flowCache = new Map();

function secidOf(code) {
  return (code.startsWith("5") || code.startsWith("6") || code.startsWith("9") ? "1." : "0.") + code;
}

async function loadFlows(codes) {
  const now = Date.now();
  const need = codes.filter((c) => !flowCache.has(c) || now - flowCache.get(c).at > 300000);
  if (!need.length) return 0;
  await Promise.all(need.map(async (code) => {
    try {
      const params = new URLSearchParams({
        lmt: "15", klt: "101", secid: secidOf(code),
        fields1: "f1,f2,f3,f7", fields2: "f51,f52,f53,f54,f55",
      });
      const res = await fetch(`${FLOW_URL}?${params}`, { credentials: "omit" });
      const json = await res.json();
      const klines = (json && json.data && json.data.klines) || [];
      const vals = klines.map((l) => Number(l.split(",")[1]) || 0);
      const sum = (n) => vals.slice(-n).reduce((a, b) => a + b, 0);
      flowCache.set(code, { s3: sum(3), s5: sum(5), s10: sum(10), days: vals.length, at: Date.now() });
    } catch (err) {
      flowCache.set(code, { error: true, at: Date.now() });
    }
  }));
  return need.length;
}

const POS_KEY = "etf_positions_v1";
const WATCH_KEY = "etf_watch_v1";
const fmtMoney = (v) => (Number.isFinite(v) ? v.toLocaleString("zh-CN", { maximumFractionDigits: 1 }) : "—");

function loadPositions() {
  try {
    const raw = JSON.parse(localStorage.getItem(POS_KEY) || "[]");
    return Array.isArray(raw) ? raw.filter((x) => x && x.code) : [];
  } catch (err) {
    return [];
  }
}

function savePositions() {
  try { localStorage.setItem(POS_KEY, JSON.stringify(state.positions)); } catch (err) { /* 隐私模式忽略 */ }
}

function loadWatch() {
  try {
    const raw = JSON.parse(localStorage.getItem(WATCH_KEY) || "[]");
    return new Set(Array.isArray(raw) ? raw : []);
  } catch (err) {
    return new Set();
  }
}

function saveWatch() {
  try { localStorage.setItem(WATCH_KEY, JSON.stringify([...state.watch])); } catch (err) { /* 忽略 */ }
}

function adviceFor(x) {
  if (!x.r || x.r.ma17 == null) return ["无指标", ""];
  const pnlPct = x.pnlPct;
  if (x.price < x.r.ma17) return ["破 17 日线 · 次日离场", "sell"];
  if (pnlPct >= 20) return ["浮盈 ≥20% · 清仓", "trim"];
  if (pnlPct >= 12) return ["浮盈 ≥12% · 卖一半", "trim"];
  if (x.r.jLast != null && x.r.jLast >= 80) return ["周线 J ≥80 · 卖一半", "trim"];
  if (x.r.ma5 != null && x.price < x.r.ma5) return ["破 5 日线 · 减仓", "trim"];
  return [state.tier && state.tier.name !== "进攻" ? "持有（非进攻档）" : "持有", "hold"];
}

function positionRows() {
  return state.positions.map((p) => {
    const r = state.rows.find((x) => x.code === p.code);
    const q = state.quotes.get(p.code);
    const price = (q && q.price) || (r && r.price) || 0;
    const prev = (q && q.prev) || 0;
    const value = price * p.shares;
    const pnl = (price - p.cost) * p.shares;
    return {
      p, r, q, price, value, pnl,
      pnlPct: p.cost > 0 ? (price / p.cost - 1) * 100 : 0,
      name: (r && r.name) || (q && q.name) || p.code,
      day: prev > 0 ? (price - prev) * p.shares : null,
      dayPct: q ? q.pct : null,
    };
  });
}

function triggerCount(rows) {
  return rows.filter((x) => {
    const c = adviceFor(x)[1];
    return c === "sell" || c === "trim";
  }).length;
}

function updatePosBadge() {
  const rows = positionRows();
  const n = triggerCount(rows);
  const b = $("pos-badge");
  if (b) { b.hidden = n === 0; b.textContent = n; }
  const t = $("pos-trigger");
  if (t) {
    t.textContent = rows.length ? `${n} 只` : "--";
    t.className = n > 0 ? "trigger" : "";
  }
}

function renderPositions() {
  if (state.view !== "positions") return;
  const rows = positionRows();
  const totalValue = rows.reduce((a, x) => a + x.value, 0);
  const totalPnl = rows.reduce((a, x) => a + x.pnl, 0);
  const totalDay = rows.reduce((a, x) => a + (x.day || 0), 0);
  const dayPct = totalValue - totalDay > 0 ? (totalDay / (totalValue - totalDay)) * 100 : null;
  const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");

  $("pos-value").textContent = rows.length ? fmtMoney(totalValue) : "--";
  $("pos-pnl").textContent = rows.length ? `${totalPnl >= 0 ? "+" : ""}${fmtMoney(totalPnl)}` : "--";
  $("pos-pnl").className = cls(totalPnl);
  $("pos-day").textContent = rows.length ? `${totalDay >= 0 ? "+" : ""}${fmtMoney(totalDay)}${dayPct == null ? "" : `  ${fmtPct(dayPct, 2)}`}` : "--";
  $("pos-day").className = cls(totalDay);
  $("pos-count").textContent = rows.length ? `${rows.length} 只` : "--";
  updatePosBadge();

  $("pos-empty").hidden = rows.length > 0;
  $("pos-tbody").innerHTML = rows.map((x) => {
    const [advice, advCls] = adviceFor(x);
    const stop = x.r ? x.r.ma17 : null;
    const trim = x.r ? x.r.ma5 : null;
    const stopDist = stop && x.price ? (stop / x.price - 1) * 100 : null;
    const trimDist = trim && x.price ? (trim / x.price - 1) * 100 : null;
    const weight = totalValue > 0 ? (x.value / totalValue) * 100 : 0;
    const tp1 = x.p.cost * 1.12;
    const tp2 = x.p.cost * 1.2;
    const tp1d = x.price ? (tp1 / x.price - 1) * 100 : null;
    const tp2d = x.price ? (tp2 / x.price - 1) * 100 : null;
    const tpText = (v, d) => (d == null ? "—" : d <= 0 ? "已达成" : `${v.toFixed(3)} (${fmtPct(d, 1)})`);
    const pc = cls(x.pnl);
    const dc = cls(x.day ?? 0);
    return `<tr data-code="${esc(x.p.code)}">
      <td><div class="name-cell"><b>${esc(x.name)}</b><span>${x.p.code}${x.p.date ? ` · ${esc(x.p.date)}` : ""}</span></div></td>
      <td class="col-num">${fmtMoney(x.value)}</td>
      <td class="col-num ${pc}"><span class="dstack"><span>${x.pnl >= 0 ? "+" : ""}${x.pnl.toFixed(1)}</span><em>${fmtPct(x.pnlPct, 2)}</em></span></td>
      <td class="col-num ${dc}"><span class="dstack"><span>${x.day == null ? "—" : `${x.day >= 0 ? "+" : ""}${x.day.toFixed(1)}`}</span><em>${fmtPct(x.dayPct, 2)}</em></span></td>
      <td class="col-num"><span class="dstack"><span>${x.p.cost.toFixed(3)}</span><em>${fmt(x.price)}</em></span></td>
      <td class="col-num">${x.p.shares.toLocaleString("zh-CN")}</td>
      <td class="col-num">${weight.toFixed(1)}%</td>
      <td class="col-num flowcell">${flowCell(x.p.code)}</td>
      <td class="col-flow">${posFlowBadge(x.p.code)}</td>
      <td class="col-num"><span class="dstack"><span>${fmt(stop)}</span><em>${stopDist == null ? "—" : fmtPct(stopDist, 1)}</em></span></td>
      <td class="col-num"><span class="dstack"><span>${fmt(trim)}</span><em>${trimDist == null ? "—" : fmtPct(trimDist, 1)}</em></span></td>
      <td class="col-num"><span class="dstack"><span>${tpText(tp1, tp1d)}</span><em>${tpText(tp2, tp2d)}</em></span></td>
      <td class="col-num">${x.r && x.r.jLast != null ? x.r.jLast.toFixed(0) : "—"}</td>
      <td><span class="pos-advice ${advCls}">${advice}</span></td>
      <td><button class="pos-del" data-del="${esc(x.p.code)}" title="删除">×</button></td>
    </tr>`;
  }).join("");

  // 资金流：补齐缺失后重绘一次
  loadFlows(rows.map((x) => x.p.code)).then((n) => {
    if (n && state.view === "positions") renderPositions();
  });
}

function posFlowBadge(code) {
  const f = flowCache.get(code);
  if (!f) return '<span class="flow-badge mix">读取中</span>';
  if (f.error) return '<span class="flow-badge mix">—</span>';
  return flowBadge({ flow3: f.s3, flow5: f.s5, flow10: f.s10 });
}

function flowCell(code) {
  const f = flowCache.get(code);
  if (!f) return '<span class="dash">读取中…</span>';
  if (f.error) return '<span class="dash">—</span>';
  const line = (label, v) => {
    const c = v > 0 ? "pos" : v < 0 ? "neg" : "dash";
    return `<span class="${c}">${label} ${fmtInflow(v)}</span>`;
  };
  return [line("3日", f.s3), line("5日", f.s5), line("10日", f.s10)].join("<br>");
}

function exportPositions() {
  if (!state.positions.length) { $("pos-msg").textContent = "没有可导出的持仓"; return; }
  const blob = new Blob([JSON.stringify(state.positions, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `etf-positions-${new Date().toISOString().slice(0, 10)}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
  $("pos-msg").textContent = `已导出 ${state.positions.length} 条持仓`;
}

function importPositions(file) {
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const list = JSON.parse(String(reader.result));
      if (!Array.isArray(list)) throw new Error("格式不对");
      let n = 0;
      for (const item of list) {
        const code = String(item.code || "").trim();
        const cost = Number(item.cost);
        const shares = Number(item.shares);
        if (!/^\d{6}$/.test(code) || !(cost > 0) || !(shares > 0)) continue;
        const exist = state.positions.find((p) => p.code === code);
        if (exist) { exist.cost = cost; exist.shares = shares; exist.date = item.date || exist.date; }
        else state.positions.push({ code, cost, shares, date: item.date || "" });
        n += 1;
      }
      savePositions();
      renderPositions();
      $("pos-msg").textContent = `已导入 ${n} 条持仓`;
    } catch (err) {
      $("pos-msg").textContent = `导入失败：${err.message}`;
    }
  };
  reader.readAsText(file);
}

function addPosition(e) {
  e.preventDefault();
  const code = $("pos-code").value.trim();
  const cost = Number($("pos-cost").value);
  const shares = Number($("pos-shares").value);
  const date = $("pos-date").value;
  const msg = $("pos-msg");
  if (!/^\d{6}$/.test(code)) { msg.textContent = "代码需为 6 位数字"; return; }
  if (!(cost > 0) || !(shares > 0)) { msg.textContent = "成本和股数需大于 0"; return; }
  const exist = state.positions.find((p) => p.code === code);
  if (exist) {
    exist.cost = cost;
    exist.shares = shares;
    if (date) exist.date = date;
  } else {
    state.positions.push({ code, cost, shares, date: date || "" });
  }
  savePositions();
  msg.textContent = `已${exist ? "更新" : "添加"} ${code}`;
  e.target.reset();
  renderPositions();
}

function removePosition(code) {
  state.positions = state.positions.filter((p) => p.code !== code);
  savePositions();
  renderPositions();
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  $("view-board").hidden = view !== "board";
  $("view-positions").hidden = view !== "positions";
  if (view === "positions") renderPositions();
}
