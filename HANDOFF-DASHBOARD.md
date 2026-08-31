# HANDOFF — Agentic Trading Dashboard 改造

> 交接文档。接手者请从头读完再动手；**第 2 节的三个「已证伪方案」尤其重要**，避免重走弯路。
> 所有行号基于 commit `ba9e4f5`（分支 `mechanics-2026-08-30`）。
>
> **动手前第一件事**：跑 `python check_handoff_anchors.py`。
> 本文引用了 53 处 `文件:行号`，行号会随代码变动漂移。该脚本把每处引用锚定到一行具体代码
> （基线存在 `handoff-anchors.json`），告诉你它现在漂到了第几行：
>
> - **OK** —— 引用仍然准确，照着读就行
> - **MOVED** —— 代码还在，只是行号变了。跑 `--fix` 自动改正本文，然后 `--snapshot` 重设基线
> - **MISSING** —— **锚定的那行代码已被改写或删除**。这说明本文相应段落的结论可能已经失效，
>   需要人来重新判断，renumber 解决不了。这是唯一会让脚本非零退出的情况。

---

## 0. TL;DR

仓库 `C:\Users\helow\Documents\Trading`（P1）+ `C:\Users\helow\Documents\Trading-P2`（P2），一套 agentic paper-trading 系统。

**Dashboard 已经存在**（`src/agentic_trading/dashboard/`，FastAPI + React/Vite，7 页面，双盘）。任务不是从零建，是三件事：

1. **补全四块数据盲区**（意图 / 拦截 / 信号验证 / 心跳）
2. **加实时「实况」视图 + 全屏「大屏」模式**（用户要视觉冲击力强的那种）
3. **常驻化 + 打通日报**

Phase 0 的设计稿**已完成并验证**，见第 5 节。

### 0.1 已由用户拍板、不必重新讨论的决策

这些是对话里明确问过、用户明确选过的，不是设计者的默认假设——改动前请留意：

- **访问范围：只在本机看。** 绑 `127.0.0.1`（现有 dashboard 已如此），宽屏定宽、信息密度优先。
  **明确不做手机/内网可访问、不做响应式布局。** 不要为了"手机也能看"改绑 `0.0.0.0` 或加媒体查询断点。
- **实时到底要什么：三件事都要，不是三选一。**
  a) 运算台·当前信号（watchlist 逐标的量化分实时算，见 §3.1）
  b) 周期跟踪·决策流（cycle 跑的时候看它逐步在做什么，见 §2.1 已证伪 + §6 Phase 2a）
  c) 持仓盯市·P&L 跳动（开市期间轮询 Alpaca 持仓，见 `broker_read.py` 现有能力）
  用户原话是想要"那种可视化的、图形化的实时"，类比 X 上常见的那类交易/agent 监控画面——即 §6 Phase 3 的 8 步管线逐段点亮 + 大屏模式。
- **架构分水岭：允许 dashboard 跑只读计算。** 用户明确选择放宽旁观者契约到"可以调用 `signals/` 的纯函数"（见 §7 约束 1 的例外条款），而不是严守"只展示已落库的东西"。这是 §3.1 影子计算方案成立的前提，没有这个选择 Phase 2c 就做不了。
- **Phase 2a 改 `run.py` 吐事件：用户已确认接受改交易引擎**，代价是要过 P2 同步（§7 约束 2）。备选的"只 tail 日志、零引擎风险"方案已被明确放弃——不是没考虑，是权衡过后选了改引擎这条。

---

## 1. 系统现状（接手必读）

### 1.1 它是什么

Python 3.12，`src/agentic_trading/`，双盘共用一套引擎（P1 固定 14 只成长池 / P2 252 候选机械 RS 轮动）。**不是常驻进程**——Windows 计划任务拉起短命进程，跑完就退。

已注册的计划任务（`schtasks /query` 实测）：

| 任务 | 命令 | 触发 |
|---|---|---|
| `AgenticTrading` | `pythonw.exe -m agentic_trading.run` | Mon–Fri 09:45 & 16:15 ET，限时 30min |
| `AgenticTradingFastScan` | `... run --fast` | Mon–Fri 09:35 起，每 20min，持续 6h30m，限时 10min |
| `AgenticTradingP2` / `P2FastScan` | 同上，P2 盘 | 09:50 / 16:20 ET |
| `AgenticTradingDailyReport` | `daily_report` 双盘 | 16:25 |
| `AgenticTradingHeartbeatCheck` | `heartbeat --check` | 11:00 起每小时 ×4 |
| `AgenticTradingWeeklyReview` / `P2` | Kimi CLI 一次性实例 | Sat 08:30 / 09:30 |

`src/agentic_trading/scheduler.py` 是个真守护进程，但**没有被注册**，不是活的那条路（它自己的 docstring 都说 Task Scheduler 更稳）。**没有 dashboard 的自启任务。**

### 1.2 现有 dashboard

`src/agentic_trading/dashboard/`，1029 行 Python + 约 660 行 TSX，19 个测试，`dist/` 已构建。

```powershell
.\.venv\Scripts\python.exe -m agentic_trading.dashboard.api   # http://127.0.0.1:8600
```

| 文件 | 行数 | 职责 |
|---|---|---|
| `api.py` | 275 | FastAPI，9 条 GET 路由 + SPA fallback，uvicorn 127.0.0.1:8600 |
| `views.py` | 167 | journal.db 只读 SQL 视图（`mode=ro`） |
| `broker_read.py` | 188 | GET-only Alpaca 读取器（stdlib urllib，60s 缓存，按 book 读各自 `.env`） |
| `registry.py` | 73 | 读 `config/dashboard.yaml` → `Book` 对象，所有路径由 `root` 推导 |
| `trades.py` | 100 | 平均成本法的回合（round-trip）核算 |
| `compare.py` | 129 | 双盘重叠度 + 60 日滚动收益相关 |
| `rosters.py` | 90 | 解析 P2 的 `research/p2/rosters/*.md` |
| `frontend/src/` | ~660 | React 18 + react-router 6 + recharts，暗色主题，中文标签 |

现有端点：`/api/books`、`/api/books/{id}/{equity,cycles,decisions,signal-outcomes,logic,positions,trades,roster}`、`/api/compare`、`/api/docs`。

页面：总览 / 持仓 / 交易记录 / 决策链 / 策略逻辑 /（P2 才有）轮动名单 / 双盘对比。

**技术栈实测已装**：fastapi 0.141.1、starlette 1.6.0、uvicorn 0.52.4、**websockets 17.0.1**（`uvicorn[standard]` 带的）、pandas 3.0.5、pyarrow 25.0.1、yfinance 1.6.0、alpaca-py 0.44.0。前端 react 18.3.1 / react-router-dom 6.26.2 / recharts 2.12.7 / vite 5.4.6 / TS 5.5.4。

### 1.3 数据层（journal.db，10 表）

Schema 权威定义在 `journal/logger.py:13-152`。当前实测行数：

| 表 | 行数 | dashboard 是否使用 |
|---|---|---|
| `cycles` | 143 | ✅ |
| `decisions` | 703 | ✅ |
| `signal_outcomes` | 703 | ⚠️ 端点已上线但**零页面调用** |
| `position_state` / `position_peaks` | 6 / 6 | ✅ |
| `llm_escalations` | 47 | ❌ |
| `trade_intents` | **2**（ANET / ARGX，活的） | ❌ |
| `intent_events` | **0**（新表，08-30 起落库） | ❌ |
| `intraday_confirmations` | 0 | ❌ |
| `regime_dwell` | 1 | ❌ |

数据区间 2026-08-18 → 2026-08-28，143 个周期。**系统 08-28 之后没再跑过**（08-29 周六 / 08-30 周日）。

账面：cycle #143，净值 $10,011.69，现金 $1,692.88，regime neutral (+0.018)，6 个持仓（MSFT / AMZN / JPM / NVDA / IQV / ACET）。

**关键设计**：`decisions.fill_price` **永远为 NULL**（`logger.py:37-39` 刻意如此），`order_qty` 对真实订单恒为 `0.0`。**真实成交只在券商侧**，靠 `broker_read.fills()` 取。journal 是审计日志，不是持仓真相源。

---

## 2. ⚠️ 已证伪的方案 —— 不要重走

接手时最容易想到、但实测行不通的三条路：

### 2.1 ❌ tail 日志做实时决策流 —— 死路

逐行量过真实日志。一个完整深周期（`logs/2026-08-28.log` 第 1–45 行）的形状是：

```
09:45:02–09:45:04   11 行 Obsidian 扫描噪音
09:45:11            Connected to Alpaca
09:45:12            Account: equity=... open_positions=6 market_open=True
09:45:26            Market regime: NEUTRAL (0.04), VIX 14.5  +6 行 regime 分量
      <<<< 2 分 25 秒 绝对静默 >>>>
09:47:51.751        ANET: BUY queued as TradeIntent
09:47:51.754        MU: AVOID (combined=+0.36) — ...
09:47:51.756        SNDK / AMD / NVDA / AMZN / ANET / CRDO ...
09:47:51.758        （20 条决策全部在 16 毫秒内喷完）
09:47:52.145        Cycle complete: 18 symbols evaluated, 0 orders placed.
```

原因：决策日志在**第二个循环**里打印，位于整个逐标的处理**之后**——

```python
# run.py:1446-1451
for item in work:
    decision = item.decision
    if decision is None:
        continue
    log.info("%s: %s (combined=%+.2f) — %s", item.symbol, decision.action.value.upper(),
             decision.combined_score, decision.reasoning)
```

工作循环本体（`run.py:1026-1136`）**不打印任何逐标的进度**：没有"正在算 NVDA"，没有"quant 分 = +0.54"，没有"LLM 调用开始/返回"。

16:15 那个周期形状相同：`16:15:20` 最后一个止损 → `16:18:16` 第一条决策 = **2 分 56 秒静默**。

快扫更薄：15 行，14 个 watchlist 标的**一行都不产生**。

→ **结论：要实时决策流，只能改 `run.py` 主动吐结构化事件。这不是偏好，是唯一解。**

### 2.2 ❌ 用 `data/cycle.lock` 检测"正在跑" —— 危险且无效

`cycle_lock.py:24` 定义路径，`:96-98` 用的是 **OS 字节范围锁**（`msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)`），不是"文件存在即上锁"。

实测：`data/cycle.lock` **0 字节，mtime 停在 2026-08-23**，而周期一直跑到 08-28。因为 `open("a+")` 不写入就不动 mtime。**存在性和 mtime 都给不出任何信息。**

唯一检测手段是去抢同一把非阻塞锁。但**抢到了你就持有了真正的互斥锁**——深周期会干等 `DEEP_LOCK_WAIT_SECONDS = 180`（`cycle_lock.py:29`），快扫直接跳过。真要这么做，open/lock/unlock/close 必须瞬时且包在 `finally` 里。**不推荐。**

### 2.3 ❌ dashboard 调 `fetch_price_history()` 取实时价 —— 会污染交易引擎

`data/market_data.py:149-171`，`CACHE_TTL_SECONDS = 15 * 60`（`:23`）。缓存过期或未命中时它会打 yfinance **并写入** `data/cache/<SYM>.parquet`（`:166-167`）——**那是交易进程要读的共享文件**。

当前磁盘缓存停在 2026-08-28 16:15，早已过 TTL，所以**每一次调用都会重新拉取并覆写**。

→ **正确做法**：直接 `pd.read_parquet(cache_path(sym))`。`cache_path()`（`market_data.py:39-43`）是纯路径helper，复用安全。实时价另走 `data/alpaca_feed.py`（见 3.2）。

---

## 3. ✅ 已验证可行的路径

### 3.1 影子计算：便宜、纯净、零写入

`signals/technical.py:78-141` 的 `compute_signal(symbol, df, min_bars=55, weights=None)` 是**纯函数**——只碰 `df["Close"] / ["High"] / ["Low"]`，无 I/O、无全局、无时钟。需要 ≥55 根 bar。

**本机实测**（直接读 `data/cache/*.parquet`）：

| 标的 | 行数 | parquet 读取 | compute_signal 热均值 | 结果 |
|---|---|---|---|---|
| NVDA | 126 | 2.1 ms | **1.80 ms** | +0.276，RSI 52.3 |
| MSFT | 126 | 2.1 ms | **1.80 ms** | +0.600，RSI 73.5 |
| ANET | 126 | 2.1 ms | **1.79 ms** | +0.390，RSI 54.6 |

**整个 14 只 watchlist 从热缓存 ≈ 55 ms CPU。**

`signals/macro.py:90-139` 的 `assess_regime(period="1y", *, feed=None, asof=None)`——**传了 `feed` 就完全不碰 `market_data`**（`macro.py:94-97`），彻底沙箱化。需要 11 个 ticker（RSP/SPY/HYG/LQD/IWM/TLT/XLY/XLP + ^VIX），**全部已在 `data/cache/` 里**（`^` → `idx_` 映射见 `market_data.py:39-43`）。实测 **23.8 ms**，重算结果与 2026-08-28 16:15 的日志行**逐位吻合**。

`decision/engine.py:43-52` 的 `decide()` **零 I/O**。`verdict=None` 是**官方支持路径**（`engine.py:56-58`，quant-only），快扫本身就走这条（`run.py:1070-1072`）。四个阈值从 `config/risk.yaml` 读：`min_quant_score_to_consider: 0.15`、`buy_threshold: 0.35`、`sell_threshold: -0.25`、`risk_off_score_penalty: 0.15`。

→ **dashboard 能在 <100ms CPU 内复现整个 watchlist 的 quant-only 决策，零写入。** 唯一复现不了的是 LLM 那一半（每标的一次 `codex exec` ≈ 10 秒）。

### 3.2 实时价：用 AlpacaFeed，不要用 yfinance

`data/alpaca_feed.py` 已存在，快扫层在用。`AlpacaFeed(drop_forming=False).prefetch(symbols)` 对**整个 universe 一次批量请求约 1 秒**（`run.py:914-922`、`alpaca_feed.py:83-113`）。已有凭证，纯 GET。

注意 `^VIX` 会回退到 yfinance（Alpaca 对指数符号返回 400，见 `alpaca_feed.py:41-50`）。

### 3.3 中途进度：`llm_escalations` 是唯一干净的信号

`journal/logger.py:299-307` 的 `record_escalation`，在 `run.py:1127` 被调用——**每个标的的 LLM 调用返回后立刻 commit**。WAL 模式下 `mode=ro` 读者即时可见（`views.py:11-15` 的 `connect_ro`）。

表是 `symbol PRIMARY KEY` + `last_escalated_at` upsert。**数有多少行带着当前 cycle 的时间戳 → 真实的「已分析 n / 14」进度条，纯只读。**

⚠️ 注意：这些表存的时间戳都是 `cycle_timestamp`（周期**开始**时刻，`run.py:883`），不是"当下"。所以你观察的是**行的变动**，不是进度时钟。

### 3.4 SSE 而非 WebSocket

`grep` 全仓库 `asyncio|websocket|StreamingResponse|async def` → **零命中**。`dashboard/api.py` 的 9 条路由**全是同步 `def`**，Starlette 丢线程池跑，所以旁边加一条 `async def` SSE 路由完全安全。

**选 SSE 的理由**：`text/event-stream` 本身就是一个 **GET** 请求 → **不破坏「每条路由都是 GET」的旁观者契约**。且 `StreamingResponse` 零新依赖。

uvicorn 单 worker（`api.py:270`），所以进程内共享状态可行。

前端目前**完全不轮询**（`App.tsx:24-26` 是挂载时 fetch 一次）。

### 3.5 市场开闭市：需要新加，但很简单

dashboard 目前**没有**开市判断。权威源是 `execution/broker.py:128-129` 的 `broker.is_market_open()` → Alpaca `/v2/clock`。

往 `broker_read.py` 加一个 5 行的 GET，照抄现有 `_cached("clock", produce)` 模式（`broker_read.py:76-88`），只读安全。

⚠️ **全仓库没有节假日日历**。工作日判断是三处朴素的 `weekday() < 5`（`scheduler.py:32`、`heartbeat.py:179`、`dashboard/compare.py:40`）。

---

## 4. 运行时真实数字（做动效必须知道）

**深周期**（14 watchlist + 4 表外持仓 = 18 个标的）：

| 日期时间 | 总时长 | 其中静默（LLM 循环） |
|---|---|---|
| 08-28 09:44:52 | **179.4 s** | 144.9 s |
| 08-28 16:15:07 | **189.8 s** | 180.3 s |
| 08-27 09:45:02 | 166.1 s | 162.9 s |
| 08-24 09:45:11 | 404.5 s | 393.9 s |
| 08-20 09:47:38 | 441.1 s | 435.1 s |

范围 **154–441 秒，近期典型 150–190 秒**（Codex `gpt-5.6-terra`；400s+ 那几天是 Kimi/Claude）。

**LLM 占比的干净测量**：2026-08-24 16:15 那次 CLI 对所有 14 个标的瞬间 exit 1，逐标的循环在"没有可用 LLM"下跑完，错误时间戳给出精确节奏 **~1.9 秒/标的**（yfinance news + fundamentals 的 HTTP，`run.py:1121-1122`），14 个 = 24.4 秒，整周期 30.4 秒。两次 `--dry-run --skip-llm` 深周期均为 **3.4 秒**（18 标的，含 regime、全部 compute_signal、decide、journal 写入、止损对账）。

→ **150–190 秒的深周期里，LLM 子进程占 80–87% 墙钟，约 8.5–11.7 秒/次 `codex exec`。循环严格串行**（`run.py:1026` → `:1123`），全仓库除 `backtest/audit.py:808` 外无任何线程池。

**快扫**：中位 **5–7 秒**，近期尾部 48 秒；闭市直接跳过 0.9–1.5 秒。每交易日触发 20 次（09:35–16:05 每 20min），08-28 实际 18 次（临近深周期的会让路，`run.py:1535-1538`）。

⚠️ **快扫的 LLM 升级路径最后一次触发是 2026-08-20**（18 次），08-21 至 08-28 **零次**。且日志里 `Fast-tier scan complete: 4/14 symbols escalated` 是**误标**——`len(rows)` 把 4 个表外持仓也算进去了（`run.py:1506`）。

**动效设计含义**：深周期的"活"窗口是 2.5–3 分钟且几乎全在等 LLM；快扫是 5–7 秒。**开市期间系统平均每 1200 秒里只有约 6 秒是可观测的。** 大屏模式必须在"什么都没跑"的绝大多数时间里依然好看——靠影子计算（3.1）持续刷新，而不是靠等 cycle。

---

## 5. Phase 0 已完成 ✅

设计稿已产出并验证：

- `research/dashboard-phase0-demo.py` —— 可重跑的生成器，**已 track**（`research/` 不 gitignore）
- 跑它会在 `tmp/dashboard-demo.html` 产出自包含 HTML（`tmp/` 已 gitignore，重新生成即可，不必保留）

**每个数字都是生成时从 `data/journal.db`（`mode=ro`）和 `data/heartbeat.json` 现读的，无手写无编造。** 不改 `src/`，不起 dashboard 服务。

四块面板 + 实测结论：

**① 心跳** —— deep 静默 46.7h，超过 `DEEP_MAX_AGE_HOURS=26`（`heartbeat.py:53-54`）。但新写的 `missed_sessions()` 算出漏掉的**交易日 = 0**（08-29 周六、08-30 周日），判为「休市中」而非「停摆」。**这个判据比现有 `heartbeat --check` 更准**——按小时数报警每个周末都会误报，建议 Phase 1 采纳。

**② 待执行意图** —— ANET / ARGX，窗口 08-29 14:00Z 已开，TTL 剩 25.0 小时。单独写了 `fmt_countdown()`（复用 `fmt_age()` 会渲染成"1.0 天"，倒计时看不出今晚死还是明天下午死）。

**③ 意图拦截·两层** —— 决策层 SQL 谓词**逐字复用** `daily_report.py:135-149` 的 `VETO_CATEGORIES`（风控否决 1/93、新单上限 0/13、fail-closed 2/2、intent 排队 16/16）；执行层 `intent_events` 五类全 0，**如实显示空态并说明原因**，不编占位数据。

**④ 信号验证** —— per-horizon 样本数分别标注，这是核心设计点：

| 桶 | 总 n | 评了 +1d | 评了 +5d | 评了 +20d |
|---|---|---|---|---|
| avoid | 326 | 293 | 183 | **0** |
| hold | 192 | **43** | **23** | 0 |
| buy | 168 | 159 | 145 | 0 |
| wait | 17 | 11 | **5** | 0 |

hold 桶 192 条只评了 43 条 +1d，avoid 是 293/326 —— **把两行均值并排比较是错的**。现有 `outcomes_summary()` 只返回总 n，正是会诱发这个误读。`wait` 桶 +5d 显示 **+4.78% 但 n 只有 5**，是噪声不是发现。n<30 标琥珀；`small_sample` 阈值与代码里 `outcomes_summary(min_n=5)` 对齐，未自行发明更严规则。

> 生成 + 查看：
> ```powershell
> .\.venv\Scripts\python.exe research\dashboard-phase0-demo.py
> cd tmp; python -m http.server 8899 --bind 127.0.0.1
> ```
> → `http://127.0.0.1:8899/dashboard-demo.html`
> （Chrome 扩展的 `file://` 被拦，所以要走 HTTP；直接双击文件用普通浏览器打开也行）

---

## 6. 待做：Phase 1–4

### Phase 1 — 补全四块盲区

**后端** `dashboard/views.py` + `api.py`，三个新 GET：

- `/api/books/{id}/health` —— 读 `Book.root/data/heartbeat.json`，附 `stops_covered`/`positions`（可能缺字段，按可选处理）。**阈值引用 `heartbeat.py:53-54` 常量，不硬编码**。采纳 demo 的 `missed_sessions()` 判据。
- `/api/books/{id}/intents` —— `trade_intents` + 派生状态（`pending` / `window_open` / `expired`），TTL 用 `run.py:729` 的 `TRADE_INTENT_TTL`。
- `/api/books/{id}/vetoes?days=` —— `intent_events` 聚合（复用 `daily_report.py:319-331` 的 `_count_intent_events`）+ `VETO_CATEGORIES` 计数。

`/signal-outcomes` 端点已存在，只需给 `views.py:109` 的 `outcomes_summary()` 补 per-horizon 的 `n_1d`/`n_5d`/`n_20d`。

**前端**：`api.ts` 加类型 + 三个 `get()`（复用现成 `usd()`/`pct()`/`shortTime()`）；`Overview.tsx` 加心跳条 + 意图卡；新页 `Intents.tsx`、`Outcomes.tsx`；`App.tsx` 的 `PAGES` 加两项。

### Phase 2 — 实况引擎（新增，本次改造的核心）

**2a. `run.py` 吐结构化事件** —— 用户已确认接受改交易引擎。

在关键节点 append 一行 JSONL 到 `data/live_events.jsonl`：

```json
{"t":"2026-08-31T13:45:12.481+00:00","cycle":144,"stage":"quant","symbol":"NVDA","score":0.41}
{"t":"...","cycle":144,"stage":"llm_call_start","symbol":"NVDA"}
{"t":"...","cycle":144,"stage":"llm_call_done","symbol":"NVDA","ms":8200,"stance":"bullish"}
{"t":"...","cycle":144,"stage":"decide","symbol":"NVDA","action":"hold","combined":0.54}
```

建议埋点：cycle 开始 / regime 完成 / 每标的进入循环 / compute_signal 完成 / LLM 调用起止 / decide 完成 / sizing 结果 / 订单或否决 / cycle 结束。

⚠️ **写入必须走 `run.py:248-259` 的 `_journal_safe` 那种吃错误的包装**——事件写失败绝不能中断交易。时间戳用 `datetime.now(timezone.utc).isoformat()`（**不要**复用 cycle 起始时间戳，那样就没有进度含义了）。文件要轮转或截断，别让它无限增长。

⚠️ **`run.py` 是双盘同步文件**：改完必须拷贝到 `C:\Users\helow\Documents\Trading-P2`，`python check_p2_sync.py` 必须 exit 0。

**2b. SSE 端点** —— `GET /api/books/{id}/live/stream`，`async def` + `StreamingResponse`，tail `live_events.jsonl` 并推送。同时推：`llm_escalations` 的「已分析 n/14」进度（3.3）、下一次计划任务倒计时（`run.py:319` 的 `RUN_TIMES_ET` + 20 分钟快扫网格）、Alpaca `/v2/clock` 开闭市（3.5）。

**2c. 影子计算端点** —— `GET /api/books/{id}/live/signals`，按 3.1 重算整个 watchlist 的 quant 分解（trend .30 / cross .20 / momentum .20 / macd .20 / rsi .10，见 `technical.py:59-61`）+ regime 五分量 + `decide(verdict=None)`，返回距 `buy_threshold: 0.35` 还差多少。

⚠️ **必须直接 `pd.read_parquet(cache_path(sym))`，绝不调 `fetch_price_history()`**（见 2.3）。实时价走 `AlpacaFeed`（3.2）。若要自建缓存，**用独立目录**，不要写 `data/cache/`。

### Phase 3 — 视觉：实况页 + 大屏模式

用户明确要「两者都要」：`/p1/live` 实况页（1200px 定宽，密度优先，融入现有 7 页）+ `/p1/live?wall` 大屏模式（全屏、无导航、字号放大、动效强，适合副屏常开和截图发 X）。**共用同一套 SSE 端点，只是渲染层不同。**

**核心视觉：8 步管线逐段点亮。** `views.py:152-161` 里**已经写好了** 8 步的中文描述（0 市场体制 → 1 量化信号 → 2 风控优先 → 3 LLM 分析 → 4 决策合成 → 5 风险 sizing → 6 执行窗口 → 7 保护性止损），直接拿来当流程图节点。

**技法（纯 SVG + CSS，零新依赖）**：
- 连线用 `stroke-dasharray` + `stroke-dashoffset` 关键帧做流动
- 数据包用 SVG `animateMotion` 沿路径跑
- 每个阶段一个 identity 色
- 暗底 + 辉光描边 + 细网格 + 平滑推进 —— 这三样是让画面"读起来像流动的数据"的关键，**暗底 `#0d1117` 我们已经有了**

**不要引入 React Flow**：8 个固定节点，手写 SVG 比它更轻更可控。前端依赖保持 4 个。

**设计 token 全部取自 `frontend/src/index.css`**：`--bg #0d1117 / --panel #161b22 / --border #30363d / --text #e6edf3 / --muted #8b949e / --accent #58a6ff / --green #3fb950 / --red #f85149 / --amber #d29922`，Segoe UI + 微软雅黑，14px，`tabular-nums`，8px 圆角，无阴影无渐变。每个面板底部带 `.note` 数据来源脚注（仓库既有习惯）。

⚠️ **大屏模式必须在"什么都没跑"时依然好看**——见第 4 节，开市期间平均每 1200 秒只有约 6 秒可观测。空闲态靠影子计算持续刷新，不能靠等 cycle。

### Phase 4 — 常驻化 + 打通日报

**4a.** 新增计划任务，参照 `config/scheduled-tasks/*.xml`，登录时用 `pythonw.exe` 拉起 dashboard（无控制台闪窗，交易任务已是此惯例）。若加 `.cmd` 包装器**必须保持 CRLF**（`.gitattributes:4-5`，cmd.exe 会误解析 LF-only 批处理）。

**4b. round-trip 接进日报 —— 本计划风险最高的一步。**

`AGENTS.md:186-189` 记着这个缺口：回合盈亏核算"只活在手动启动、且只有 P1 有的 dashboard 里，日报/周报够不着"。

障碍：`check_p2_sync.py:42` 规定 **`dashboard/` 是 P1 专属**，P2 没这个包；而 `daily_report.py` 是**双盘同步文件**。直接 `from .dashboard.trades import round_trips` 会让 P2 日报 ImportError 崩掉。

正确做法是上提为共享模块：
- `dashboard/trades.py`（纯计算）→ `src/agentic_trading/round_trips.py`
- `dashboard/broker_read.py`（Alpaca 只读，日报也需要成交流水）→ `src/agentic_trading/broker_read.py`
- `dashboard/` 改为从新位置导入（`api.py` 不变）
- 两个新文件**拷贝到 Trading-P2**，`check_p2_sync.py` exit 0

**顺带**：`daily_report` 至今每份都写着"matplotlib 不可用，已跳过绘图"——matplotlib 没装。装上，或改为指向 dashboard。可选。

---

## 7. 硬约束（违反会出事）

1. **旁观者契约**（`api.py:5-7`、`views.py:3-4`、`broker_read.py:8-18`）：全 GET；journal 一律 `mode=ro`；broker reader 内**不存在任何下单/撤单代码**，且硬拒非 paper endpoint（`broker_read.py:42-45`）。
   **本次已放宽的唯一一点**：允许 dashboard 调用 `signals/` 的纯函数做只读影子计算。仍然不下单、不写 journal、不改任何状态。
2. **双盘同步**：改任何 `src/agentic_trading/**` 里**非 `dashboard/`** 的文件 → 拷贝到 P2 → `python check_p2_sync.py` exit 0。
   Phase 1、3 只碰 `dashboard/`，**豁免**；**Phase 2a（改 `run.py`）和 Phase 4b 不豁免**。
3. **冻结区**（`AGENTS.md:22-24` 铁律 #1）：`config/risk.yaml`、`config/watchlist.yaml`、`signals/`、`decision/` **只读不改**。影子计算是调用它们，不是修改。
4. **`equity_series` 必须带 `WHERE mode='paper'`**（`views.py:33-36`）。`dry_run` 报告固定 $100,000 假净值且写同一个 journal，漏掉这个过滤会在曲线中间打出 10x 尖峰。
5. **路径**用 `Path(__file__).resolve().parents[N]` 锚定，不依赖 cwd。`dashboard/*` 是 `parents[3]`，`run.py`/`config.py`/`heartbeat.py` 是 `parents[2]`。
6. **编码**：所有文件 IO 带 `encoding="utf-8"`；入口点要 `sys.stdout.reconfigure(encoding="utf-8")`（`run.py:104-118`），否则 Windows 控制台会把中文和破折号打成乱码。
7. **日志**用 `%s` 惰性格式化，不用 f-string。`logging.getLogger(__name__)` 模块级。
8. **SQLite**：写入端 `logger.py:179-198` 先设 `busy_timeout=10000` **再**切 WAL（顺序有意义，切 WAL 本身要拿写锁）。读取端一律 `mode=ro` URI。
9. **生成物不进 git**：`.gitignore` 已排除 `dist/`、`node_modules/`、`tmp/`、`logs/daily/`、`data/*.db`。**约定是：派生产物 gitignore，值得留存的文字分析放 `research/` 并 track。**
10. **`.gitattributes:4-5`**：`*.cmd` / `*.bat` 必须保持 CRLF。

---

## 8. 验证

**测试基线**：P1 **424 passed**，P2 **405 passed**。29 个测试文件，402 个测试函数，pytest 8，`testpaths = ["tests"]`。无 CI。

```powershell
.\.venv\Scripts\python.exe -m pytest              # P1，期望 424 passed
python check_p2_sync.py                            # Phase 2a / 4b 后必须 exit 0
cd ..\Trading-P2; .\.venv\Scripts\python.exe -m pytest   # 期望 405 passed
```

**新增后端测试直接套 `tests/test_dashboard.py:40-95` 的现成模式**：`tmp_path` 下造两个假 book root，用 `logger.connect()` + `record_cycle()` 播种真 journal.db，写临时 `dashboard.yaml`，`monkeypatch.setattr(api_mod, "load_books", ...)`，用 `fastapi.testclient.TestClient` 驱动。不碰真 journal，不需要 Alpaca 凭证。

⚠️ **`tests/conftest.py:19-26` 有个 autouse fixture 重定向 heartbeat 文件**——动 heartbeat 相关代码前先读它的 docstring：跑测试曾经覆写了活的 `data/heartbeat.json`，让看门狗以为深周期刚跑完（`positions: 0`），会压制真实告警长达 26 小时。

**前端**：
```powershell
cd src\agentic_trading\dashboard\frontend
npm run build          # = tsc --noEmit && vite build，必须零报错
```

**端到端**：
```powershell
.\.venv\Scripts\python.exe -m agentic_trading.dashboard.api
# → http://127.0.0.1:8600
```
逐页验证，**重点验空态**：`intent_events` 现在 0 行、`ret_20d` 全空、无 Alpaca 凭证时的 `degraded` 横幅。

**引擎冒烟**（不碰账户）：
```powershell
.\.venv\Scripts\python.exe -m agentic_trading.run --dry-run --skip-llm    # 约 3.4 秒
```
Phase 2a 改完 `run.py` 后必跑这个，并检查 `data/live_events.jsonl` 是否产出预期事件。

---

## 9. 环境备忘

- **DAIC hook**（cc-sessions）：仓库处于 discussion 模式时**所有写入被拦**（Write/Edit 和 write-like 的 Bash）。触发词 `yert` 进实现模式、`SILENCE` 回讨论模式，配置在 `sessions/sessions-config.json`。**只有用户能激活。**
- Git Bash 的 heredoc 在这个环境里容易被引号搞挂，写文件优先用 Write 工具。
- Bash 里带 `2>&1` / `2>/dev/null` 这类重定向会被 DAIC 判为 write-like 而拦截，读命令避开重定向。
- 当前分支 `mechanics-2026-08-30`，工作树干净（除未跟踪的 `sessions/`）。
- 项目约定文档是 `AGENTS.md`（17KB，中文），不是 CLAUDE.md。`.claude/` 是空目录。
- LLM 是**子进程**调用（`analyst_cli_path` → `codex.exe` / `claude.cmd` / `kimi.exe`），不是 harness。

## 10. 已评估并否决的外部参考

**HKUDS/AI-Trader**（21.9k stars）：**不借鉴。** 技术栈已撞车（双方都是 FastAPI + React 18 + react-router 6 + recharts + Vite 5 + TS）；它多出来的是社区/挑战赛/copy-trading/`ethers` 钱包，对单人双盘只读审计台是负资产；仓库树内**没有 LICENSE 文件**（只有 README 的 MIT 徽章），抄代码有法律模糊。

**Freqtrade / FreqUI**（25k stars）：**结构可参考，控制面不可抄。** 有价值的是它把「实时状态端点」和「历史端点」分开，实时那部分靠 WebSocket 推 typed message（`analyzed_df` 推的就是当前算出的指标 dataframe）。但它是**常驻守护进程**（`/pair_candles` 文档原话 "while the bot is running"），我们不是——这是根本差异。且 FreqUI 能 start/stop bot、强制开平仓，**我们是严格只读，那部分绝不能抄**。

---

## 11. 分阶段执行建议：模型与 effort

这是给协调者（人或另一个 Claude 会话）分派各阶段时的参考，不是强制流程。

| 阶段 | 建议模型 | Effort | 理由 |
|---|---|---|---|
| Phase 1 补盲区 | Sonnet | medium | 高度模式化：`views.py` 有 6 个现成 view 函数可仿，`api.py` 有 9 条现成路由，`api.ts` 有成套类型，`tests/test_dashboard.py:40-95` 是现成测试模板。歧义低，代码量大，单价该省。 |
| Phase 2a 改 `run.py` 吐事件 | Opus | high | 唯一能弄坏在跑的系统的一段——`run.py` 是双盘同步文件，P2 每天有真实计划任务在跑。写入必须走 `_journal_safe` 那种吃错误的包装，改错会中断真实交易决策。 |
| Phase 2b/2c SSE + 影子计算端点 | Sonnet | medium | 照 §3.1/§3.4 给的实测方案（纯函数、路径、耗时都已给出）实现，路径清楚。 |
| Phase 3 视觉（8 步管线动效 + 大屏） | Opus | medium | 这是需要判断力的部分：demo 已确认的设计要推广到全部页面，且要保证"什么都没跑的绝大多数时间也好看"（见 §4 末尾），这类取舍不是照抄代码能出的。 |
| Phase 4 常驻化 + round_trips 上提 | Opus | high | 第二处有真实风险的改动：把 P1 专属的 `dashboard/trades.py`/`broker_read.py` 上提为共享模块，要过 `check_p2_sync.py`，P2 侧也要跑通。 |

**不建议全程用 max**：这些任务的约束已经在本文档里写清楚了，需要的是照约束执行、量力选路，不是从零推导——max 只会在已有答案的地方空转。真正需要深推理的部分（"哪些方案证伪、为什么、影子计算是否可行"）已经在 Phase 0 做完，见第 2、3 节。

**顺序建议**：1 → 2a → 2b/2c → 3 → 4。Phase 2a 建议在 2b/2c 和 3 之前先独立验证（跑一次 `--dry-run --skip-llm` 冒烟 + 检查 `data/live_events.jsonl` 产出），因为后两个阶段都依赖它的事件格式。Phase 4 放最后，因为它是唯一牵涉两个仓库真实同步的一步。

**上下文管理**：每个阶段开新会话，把本文档作为起点交接，不要在一个长会话里连续做完全部阶段——Phase 0 探索阶段就已经把一次会话喂到接近 100% 上下文，实现阶段的代码量更大。
