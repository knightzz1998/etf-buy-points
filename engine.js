/**
 * 博主波段法信号引擎（浏览器端）
 *
 * 与仓库 Python 口径保持一致：
 *   - 周线 KDJ(9,3,3)：周高/周低/周收盘
 *   - 5/17/60 日线：简单移动平均
 *   - 第一买点：近 6 周 J 谷底 <= 20 且本周 J 回勾 + 现价 > MA17 + MA5 > MA17
 *   - 四档：空头离场 / 修复期观望 / 多头内按 J>=80、三条件、破 5 日线区分
 *
 * 入参是构建期烘焙的静态记录（含最近 60 日收盘与最近 45 周高低收），
 * 实时快照只补当日价，因此盘中得到的是动态均线和动态周线 J。
 */

export const J_TROUGH = 20;
export const J_OVERBOUGHT = 80;
export const MIN_WEEKS = 12;
export const NEAR_MA17_PCT = 3;

export const SIGNAL_ORDER = { buy: 0, add: 1, trim: 2, wait: 3, exit: 4 };
export const SIGNAL_TEXT = {
  buy: "可以买入", add: "可以加仓", trim: "可以减仓", exit: "该离场", wait: "观望",
};
export const SIGNAL_ICON = { buy: "🟢", add: "🔵", trim: "🟡", exit: "🔴", wait: "⚪" };

/** 交易日所属周的周一（YYYY-MM-DD） */
export function weekStart(dateStr) {
  const d = new Date(dateStr + "T00:00:00Z");
  if (Number.isNaN(d.getTime())) return "";
  const wd = (d.getUTCDay() + 6) % 7; // Monday=0
  d.setUTCDate(d.getUTCDate() - wd);
  return d.toISOString().slice(0, 10);
}

/** 周线 KDJ(9,3,3)：h/l/c 为等长周线数组，返回 {k,d,j} */
export function kdjWeekly(h, l, c) {
  const n = c.length;
  const k = new Array(n), d = new Array(n), j = new Array(n);
  const alpha = 1 / 3;
  let prevK = 50, prevD = 50;
  for (let i = 0; i < n; i++) {
    const lo = Math.max(0, i - 8);
    let hh = -Infinity, ll = Infinity;
    for (let t = lo; t <= i; t++) {
      if (h[t] > hh) hh = h[t];
      if (l[t] < ll) ll = l[t];
    }
    const span = hh - ll;
    const rsv = span <= 0 ? 50 : ((c[i] - ll) / span) * 100;
    let curK, curD;
    if (i === 0) {
      curK = rsv;
      curD = rsv;
    } else {
      curK = alpha * rsv + (1 - alpha) * prevK;
      curD = alpha * curK + (1 - alpha) * prevD;
    }
    k[i] = curK;
    d[i] = curD;
    j[i] = 3 * curK - 2 * curD;
    prevK = curK;
    prevD = curD;
  }
  return { k, d, j };
}

function mean(arr) {
  let s = 0;
  for (const x of arr) s += x;
  return s / arr.length;
}

/**
 * 计算单只 ETF 的指标与信号。
 * rec: 构建期静态记录 {c,n,a,p,d,w,ds,ws,...}
 * q:   实时快照 {price,pct,high,low,amount,turnover,liangbi,mf,premium,date}
 */
export function evaluate(rec, q) {
  const price = Number(q.price) || 0;
  const qdate = q.date || rec.ds;
  const out = {
    code: rec.c, name: rec.n, asset: rec.a, group: rec.g || rec.n, peers: 1,
    price,
    pct: Number(q.pct) || 0,
    amount: Number(q.amount) || 0,
    turnover: Number(q.turnover) || 0,
    liangbi: Number(q.liangbi) || 0,
    mainInflow: Number(q.mainInflow) || 0,
    premium: q.premium == null ? null : Number(q.premium),
    scale: Number(q.scale) || 0,
    qdate,
    spark: rec.d.slice(-30),
  };

  if (price <= 0 || rec.w.length < MIN_WEEKS || rec.d.length < 20) {
    Object.assign(out, {
      signal: "wait", signalText: "观望", state: "无数据", nearBuy: false,
      reason: price > 0 ? "历史数据不足" : "无有效报价",
      cond: { jTrough: false, jUp: false, aboveMa17: false, ma5AboveMa17: false },
      met: 0, score: 0, distMa17: null, distMa20: null, strength: 0,
      ma5: null, ma17: null, ma20: null, ma60: null,
      jLast: null, jPrev: null, jMin6: null, jOverbought: false,
      pullbackHit: "-", buyReady: false, stop: null,
    });
    return out;
  }

  // 当日若已在烘焙数据里，先剔除，再补实时价，避免重复计算当日
  const closes = rec.d.slice();
  if (rec.ds === qdate) closes.pop();
  const eff = closes.concat([price]);

  const ma5 = mean(eff.slice(-5));
  const ma17 = mean(eff.slice(-17));
  const ma20 = mean(eff.slice(-20));
  const ma60 = eff.length >= 59 ? mean(eff.slice(-60)) : null;

  // 把当日价并入最后一周
  const w = rec.w.map((x) => x.slice());
  const qw = weekStart(qdate);
  const txHigh = Number(q.high) || price;
  const txLow = Number(q.low) || price;
  if (w.length && qw === rec.ws) {
    const last = w[w.length - 1];
    last[0] = Math.max(last[0], txHigh, price);
    last[1] = Math.min(last[1], txLow, price);
    last[2] = price;
  } else if (!w.length || qw > rec.ws) {
    w.push([Math.max(txHigh, price), Math.min(txLow, price), price]);
  }

  const wh = w.map((x) => x[0]);
  const wl = w.map((x) => x[1]);
  const wc = w.map((x) => x[2]);
  const { j } = kdjWeekly(wh, wl, wc);
  const jLast = j[j.length - 1];
  const jPrev = j.length >= 2 ? j[j.length - 2] : jLast;
  const jMin6 = Math.min(...j.slice(-6));

  const jTrough = jMin6 <= J_TROUGH;
  const jUp = jLast > jPrev;
  const aboveMa17 = price > ma17;
  const ma5AboveMa17 = ma5 > ma17;
  const met = Number(jTrough) + Number(jUp) + Number(aboveMa17) + Number(ma5AboveMa17);
  const buyReady = jTrough && jUp && aboveMa17 && ma5AboveMa17;

  let state;
  if (!aboveMa17) state = "空头";
  else if (!ma5AboveMa17) state = "修复期";
  else state = "多头";

  let signal, reason;
  if (state === "空头") {
    signal = "exit";
    reason = "收盘价低于17日线，生命线失守，不参与";
  } else if (state === "修复期") {
    signal = "wait";
    reason = "站上17日线但5日线未金叉，等MA5上穿确认";
  } else if (jLast >= J_OVERBOUGHT) {
    signal = "trim";
    reason = "多头中周线J超买，按规则卖一半";
  } else if (buyReady) {
    signal = "buy";
    reason = "周线J凹底回勾 + 站上17日线 + 5日线在上，波段第一买点";
  } else if (price < ma5) {
    signal = "trim";
    reason = "多头中收盘跌破5日线，降仓位等回踩";
  } else {
    signal = "add";
    reason = "多头趋势中回踩不破、站稳5日线，加仓点";
  }

  let nearBuy = false, nearReason = "";
  if (!buyReady) {
    if (jTrough && jUp && aboveMa17 && !ma5AboveMa17 && ma5 >= ma17 * 0.99) {
      nearBuy = true; nearReason = "J已凹底回勾且站上17日线，只差5日线上穿";
    } else if (jTrough && jUp && ma5AboveMa17 && !aboveMa17 && price >= ma17 * (1 - NEAR_MA17_PCT / 100)) {
      nearBuy = true;
      nearReason = `J已凹底回勾，距17日线 ${(Math.abs(price / ma17 - 1) * 100).toFixed(1)}% 待收复`;
    }
  }

  const distMa17 = (price / ma17 - 1) * 100;
  const distMa20 = (price / ma20 - 1) * 100;
  const distMa5 = (price / ma5 - 1) * 100;
  // 回踩17日线参考触发：近 20 个交易日最低价触及 MA17 的次数
  const lows = rec.dl && rec.dl.length ? rec.dl : rec.d.slice(-20);
  const hit = lows.filter((x) => x <= ma17).length;
  const posScore = Math.abs(distMa17) <= 8 ? Math.max(0, Math.min(1, (8 - Math.abs(distMa17)) / 8)) : 0;

  Object.assign(out, {
    signal, signalText: SIGNAL_TEXT[signal], state,
    nearBuy, nearReason: nearReason || "",
    reason,
    cond: { jTrough, jUp, aboveMa17, ma5AboveMa17 },
    met, buyReady, score: posScore * 50 + (jUp ? 20 : 0),
    ma5, ma17, ma20, ma60,
    distMa17, distMa20, distMa5,
    // 策略 v2 排序：距 MA20 越远（趋势越强）越优先
    strength: Math.round(distMa20 * 100) / 100,
    jLast, jPrev, jMin6, jOverbought: jLast >= J_OVERBOUGHT,
    pullbackHit: `${hit}/20`,
    posScore,
    stop: ma17,
  });
  return out;
}

/** 成交额归一化补满 30 分（与 Python 端一致） */
export function applyAmountScore(rows) {
  let top = 0;
  for (const r of rows) if (r.amount > top) top = r.amount;
  if (top <= 0) return;
  for (const r of rows) r.score = Math.round((r.score + (r.amount / top) * 30) * 10) / 10;
}
