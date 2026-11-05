# ETF 买点工作台

全市场 A 股 ETF 的买点看板。构建期把日线指标烘焙成静态数据，页面打开后用东方财富实时快照补当日价，
盘中按 3 分钟自动重算，显示「哪些 ETF 符合买点、哪些差一项」。

线上站点为纯静态，无后端常驻进程。

## 策略 v2（回测定稿）

**只在进攻档开新仓**：

- 上证与创业板均在 MA20 上方 → 进攻档（可开新仓）
- 仅其一 → 中性档（不新开）
- 都否 → 防守档（只做持仓管理）

**买点**（博主波段法）：周线 KDJ(9,3,3) 近 6 周 J 谷底 ≤ 20 且本周 J 回勾 ｜ 现价 > MA17 ｜ MA5 > MA17 ｜ 周线 J < 80。

**排序**：距 MA20 由远到近（趋势强度），每日最多 2 只。

**离场**：破 5 日线减仓；破 17 日线次日离场；周线 J ≥ 80 卖一半；满 45 个交易日时间止损。

回测（326 只策略池 × 约 3 年，t 日信号 / t+1 收盘入场）：博主买点在进攻档 62 笔、胜率 43.5%、
均 +1.46%/笔、盈亏比 1.82；不筛档位为负期望。详见 [docs/ETF策略v2-设计.md](docs/ETF策略v2-设计.md)。

## 快速开始

```bash
python3 -m pip install -r requirements.txt

# 1. 抓全市场快照与日线，生成 workbench/dist/data/base.json
#    首次会补抓约 1500 只 ETF 的日线，1–2 分钟；之后按行情日期增量更新
python3 workbench/build_data.py

# 2. 组装前端到 workbench/dist
python3 workbench/publish.py

# 3. 本地预览（Node ≥ 18）
cd workbench && PORT=8123 node static-server.mjs dist
# 打开 http://127.0.0.1:8123
```

## 目录

```
workbench/
├── build_data.py       # 抓快照 + 日线，产出 dist/data/base.json
├── signal_engine.py    # Python 信号引擎（口径基准，用于校验）
├── publish.py          # 组装 dist/，可选组装部署 stage
├── simulate.py         # 模拟盘记录器（每日收盘后跑，写 data/sim_daily.jsonl）
├── backtest_spec.py    # 规格书口径回测
├── backtest_v2.py      # 策略 v2 交易级回测
├── static/             # 前端源码（index.html / app.js / engine.js / style.css）
│   └── engine.js       # 前端信号引擎，与 signal_engine.py 逐值对齐
└── static-server.mjs   # 静态服务
docs/                   # 策略差异与可行性、口径回测结论、v2 设计
```

## 数据来源

| 用途 | 接口 |
|---|---|
| 全市场 ETF 实时快照 | 东方财富 `push2.eastmoney.com/api/qt/clist/get` |
| 日线历史 | 东方财富 `push2his.eastmoney.com/api/qt/stock/kline/get` |
| 指数实时（档位） | 东方财富 `push2 .../ulist.np/get`（上证 000001、创业板 399006） |

浏览器端直接请求东财接口（返回 `Access-Control-Allow-Origin: *`），不经过中间服务器。
行情只在页面打开时拉取，不做后台轮询。

## 模拟盘

```bash
python3 workbench/simulate.py            # 记录当日（收盘后跑）
python3 workbench/simulate.py --status   # 查看持仓与待入场
```

记录器只做纸面成交，不连接券商、不提交委托；数据写入本地 `data/`（已被 .gitignore 忽略）。

## 限制

- 指标依赖构建产物；跨多日不重建会让均线失真（页面会顶栏提示）。
- 日线不足 130 根的新 ETF 不纳入。
- 信号为规则化提示，不构成投资建议；盘中破位等尾盘确认，收盘破 17 日线次日才执行离场。
