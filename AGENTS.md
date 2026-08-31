# AGENTS.md — Agentic Trading 双盘系统

**给任何接手的 AI 会话：先读这个文件。** 这里沉淀跨会话的记忆、纪律和系统现状。
每次会话的重要交接内容追加到 §交接记录，不要新建散装 handoff 文件。
已完成/被取代的散装快照归档到 `research/archive/`（审计用，不再当任务书）；
仍被代码或待决事项引用的文件不移动（如 `HANDOFF-BACKTEST.md` 被
`backtest/__main__.py` 注释引用、`core-candidate-pool-2026-08-22.csv` 被
`backtest/factor_ic.py` 引用、`stage-b-data-spec.md` 是唯一待决、
`HANDOFF-DASHBOARD.md` 是**进行中的任务书**且自带锚点校验，见 §交接记录 2026-08-31）。

## 系统是什么

两个 Alpaca paper 交易书，共享同一套引擎（~11.7k 行 Python）：

- **一号盘（P1）** `C:\Users\helow\Documents\Trading` —— 固定成长池 14 只
- **二号盘（P2）** `C:\Users\helow\Documents\Trading-P2` —— 252 候选池机械 RS 轮动 Top-10

共享引擎文件在两盘间必须逐字节一致：`python check_p2_sync.py`（exit 0 = 同步）。
引擎 bugfix 必须落两边。`src/` 之外（config/ data/ logs/ research/）本来就不同步。

## 铁律（违反 = 返工）

1. **冻结区不碰**：`config/risk.yaml`、`config/watchlist.yaml`、`signals/`、
   `decision/`、仓位常数。任何"因为最近亏了所以调参"直接拒绝。
2. **HOLDOUT-LEDGER.md**：2023-01-03→2026-08-21 窗口已消耗，不得用于调参；
   新证据靠 paper-forward 时间累积。新增消耗必须登记到账本。
3. **ACTIVE-BOOK-VALIDATION-PLAN.md**：当前成长池 6 个月观察期内（至 ~2027-02）
   一切波动按噪音处理；旧 12 只书的回测数字不得引用为新书预期。
4. `backtest/factor_ic.py` / `backtest/overfit.py` 是纯测量工具，
   `run.py`/`decision/`/`risk/` 不得 import 它们。
5. 改共享文件后：同步 P2 → `check_p2_sync.py` exit 0 → 两盘测试全绿
   （当前基线 P1 424 / P2 405）。
6. 测试/回测不得污染 live 状态：`run_cycle(asof=...)` 不得写 heartbeat/journal
   到真实路径（2026-08-27 已修 heartbeat 一处，守住）。
7. 改 markdown 表格后自检列数与表头对齐；新测量工具上线后必须把输出复跑
   回写结论，并检查与既有论断是否冲突（Claude 验收 2026-08-27 的教训）。

## 当前状态（2026-08-30 核实）

- 测试：P1 **424** 全绿 / P2 **405** 全绿；sync exit 0。
- **首笔平仓已出现**：XOM 移动止损 2026-08-27 09:44 ET 触发，
  实现 **−$108.46（−6.02%）**，止损纪律首次实战检验通过。
  P1 自 08-18 起 $10,007.99 → $10,011.69（+0.04%，8 个交易日）；
  P2 自 08-24 起 $50,000 → $49,112.06（−1.78%，5 个交易日，0 笔平仓）。
  样本远不足以支撑任何策略结论——按 `ACTIVE-BOOK-VALIDATION-PLAN.md` §3，
  首次正式判读在 ~2027-02。
- **P1 敞口死锁（2026-08-29 起）**：持仓 83.1% vs neutral 上限 65%，
  book 止损风险 6.02% vs 上限 6.00%，两把锁同时锁死；neutral 下 TRIM 不启动，
  敞口只进不退。已通过 `legacy_sell_threshold` 让遗留仓位自然退出来解，
  **两个上限均未动**（实测只改上限解不了锁）。
- D1（锁竞争）、D2（持仓脱离引擎）、D3（双盘漂移）已修并**实盘验证通过**
  （2026-08-27 深周期）。
- **D4 的"自解路径"假设已被实盘证伪（2026-08-30 更正）**：8/27 XOM 止损释放了
  $1,692.94 现金——正是预期的自解触发条件——但仓位依然开不出来。真正的约束
  早已不是现金：`risk/manager.py:308-314` 的 `notional = min(capped,
  exposure_budget, cash)` 里，neutral 敞口预算
  `max(0, 10011.69×0.65 − 8318.80) = 0`，book 止损风险 6.02% 也已超 6.00% 上限，
  **现金从未参与**。`min` 只报出幸存值，文案又写成 "only $0 available"，
  于是 08-29 周报误诊为"执行层现金可见性 bug"。veto 文案现已点名绑定项。
- 核心认知：quant 分数更像**风险过滤器不是选股器**（exploratory 级；IC 经济上
  可忽略，quant 微弱为负但统计可辨、RS 与零不可辨；分层杠铃形）。
  **负 IC ≠ 反向 alpha**（单调性 0.33，U 形失真摘要）。
- **已全部提交（2026-08-31 更正）**：工作区干净，分支 `mechanics-2026-08-30`
  （未合回 master）。08-30 那句"从未 commit"已过期，不要再去找散落的未提交改动；
  当前 HEAD 用 `git log --oneline -1` 自己看，别信文档里写死的 hash。

## 唯一待决：Stage B 数据抓取

规格：`research/stage-b-data-spec.md`。需用户拍板：
① 退市股方案（A stub 敏感性 / B 付费 Polygon / C 降级声明）；
② 是否提供 `TIINGO_API_KEY` / `ALPHAVANTAGE_API_KEY`。
补齐 82 个缺失文件（80 个退市 ticker）前 audit verdict 保持 inconclusive。
**预期补齐后历史成绩变差——这正是目的。**

## 交接记录（新会话追加到这里）

- `HANDOFF-BACKTEST.md`（2026-08-21，Grok）：19 年回测全过程。诚实数字：
  **+15.1% CAGR / Sharpe 1.16 / MaxDD −26.5% / 2008 年 −11.6%**（2007-04 起算窗口）。
  别引用 22%（那是 2019 牛市区间）。
- `research/opensource-gap-analysis-2026-08-26.md`（Claude 主笔，Kimi/Codex 整改）：
  ~80 个开源项目逐项判定 + 因子诊断 + D1-D4 缺陷记录。
- `research/stage-b-data-spec.md`（2026-08-27，Kimi）：Stage B 补齐规格，待批准。
- 2026-08-27（Kimi）：Codex 审计整改方案全 9 项完成——heartbeat 假新鲜 bug 修复、
  P2 README 更正、overfit 接线（surface + audit）、factor_ic bootstrap CI、
  avoid/hold 决策纳入 outcome 评估（P1 已回填 506 条）、报告 exploratory 降级、
  Stage B 规格成文、D2 实盘验证通过。Claude 验收后自行完成五处收尾
  （区间落表/措辞/列数/244→252/重发 artifact），Kimi 复核合格。
  原始 Claude 长会话（8/18–8/27，12MB）：
  `~/.claude/projects/C--Users-helow-Documents-Trading/eb40f776-eca3-4cd5-8533-a28c94ebee71.jsonl`
- 2026-08-22/29（Kimi）：接手 Grok 的 Round 2 跨行业宇宙重建并收尾——补齐 PX 断点后
  26 只缺失 SA Quant（LMT/TAL 为 SB，CDRE 为 Sell），13 个板块筛选器 1180 行
  market-wide backfill，700 只 ADV/市值 + 253 只 Alpaca 真实可交易性验证，
  产出 252 只 Core Candidate Pool（`research/core-candidate-pool-2026-08-22.csv`）
  和建议的 Active 20 只 + 审阅包 `research/archive/GPT-REVIEW-PACK-2026-08-22-KIMI-COMPLETE.md`。
  **watchlist.yaml 未改**：按冻结区铁律 + 成长池 6 个月观察期，该提案已撤回，
  研究包仅作候选池档案留存。另建每周 AI 复盘：计划任务 `AgenticTradingWeeklyReview`
  （周六 08:30，已开 StartWhenAvailable 错过补跑），入口 `run_weekly_review.cmd` +
  `config/weekly-review-prompt.md`，k3-256k / effort high（KIMI_CODE_HOME 用
  trading 专用 home），周报送 `research/weekly-review-*.md`。已跑两期：
  08-22 基线、08-29（XOM 止损首平 −$108；新疑点：现金释放后 flush 仍报
  "only $0 available" 否决 ANET/HPE，与 D4 关系待查，下周第一排查项）。
  ⚠️ **该"新疑点"已于 08-30 查清并证伪：不是现金 bug，是敞口预算与 book 止损
  风险两把锁同时到顶，现金从未参与**——见下面 2026-08-30 条与 §当前状态 的 D4
  更正。周报原文的该段结论已在 `research/weekly-review-2026-08-29.md` 顶部标注
  更正，不要再按"排查现金可见性"的方向追。
  原始 Grok 会话：`~/.grok/sessions/C%3A%5CUsers%5Chelow%5CDocuments%5CTrading/`。
- 2026-08-30（Claude）：08-29 周报后的全面评估。**策略层零改动**——P1 才 8 个
  交易日 1 笔平仓、P2 5 个交易日 0 笔平仓，按铁律 #1/#3 与 HOLDOUT-LEDGER 一律
  不动。改的都是机制：
  ① **遗留仓位口径**（唯一行为变更，用户拍板）：新增 `legacy_sell_threshold: 0.10`，
     只作用于 `run.py` off-watchlist holdings 路径（该路径 `verdict=None`，
     故 combined==quant）。见 `ACTIVE-BOOK-VALIDATION-PLAN.md` §7。
     实测判定：JPM(+0.056) SELL；ACET(+0.146，周五尖峰) / MSFT(+0.600) /
     IQV(+0.662) HOLD；池内名字不受影响。用户明确否决了"赚钱才抛"的盈亏条件
     （那是处置效应，与 `take_profit_pct: null` 的设计正面冲突）。
     两个上限（`regime_max_exposure`、`max_portfolio_stop_risk_pct`）**未动**——
     实测只改上限解不了锁：不卖任何东西的话 neutral 要提到 87.1% 才开得出一笔
     最小仓，比 risk_on 的 85% 还高。
  ② **veto 文案点名绑定项**（`risk/manager.py`）：`notional` 是 5 项取 min 的结果，
     旧文案只报幸存值 "only $X available"，于是"敞口上限已超"被读成"没现金"。
     现在会写成 `only $0 available (limited by the exposure cap — invested 83.1%
     of a 65% ceiling)`；批准路径也加了 `set by ...`。
     `tests/test_risk.py` 有 4 个用例把这个归因钉住——这是本次误诊的直接防复发项。
  ③ **intent_events 新表**：gap / chase_signal / chase_open / sizing / ttl 五类
     flush 结果落库，`daily_report` 的 NOT_JOURNALED 清单清空。
  ④ **size veto 不再删除 intent**（`run.py`）——book 级的临时状态曾把已算好的
     分析永久扔掉，而个股级的 chase 反而保留；现已一致，只有 TTL 丢弃。
  ⑤ **dry_run 假净值不再污染曲线**：`daily_report` / `dashboard/views` 的 equity
     查询加 `mode='paper'`（此前 2026-08-23 日净值读数是 $100,000）。
  ⑥ **双盘指标改回预注册规格**：重叠度分母 min→**max**，相关系数从"净值水平
     20 期"改为"**日收益 60 日**"（PAPER2-THEME-ROTATION §5 原文）。净值水平
     相关对两本都在漂移的账簿几乎恒为 ~1.0，会让 >0.95 判负线误触发。
  ⑦ **evaluate 补 `quant<0.15` 分档**：n=266 是样本最大且表现最好的一档
     （+1d +0.5% / +5d +0.9%），此前从不出现在任何报告里——周报"0.30–0.45 最弱"
     是在被截断的视图上得出的。
  ⑧ **止损覆盖进心跳**：`_reconcile_protective_stops` 返回 (covered, total)，
     `heartbeat --check` 据此告警；`wanted >= market` 分支区分"已有挂单仍覆盖"
     与"裸奔"两种情况。
  ⑨ **铁律 #6 的一处长期违规已修**：`tests/` 没有 conftest，`write_heartbeat`
     的 path 默认值又绑定在函数签名上，于是**任何调用 `run_cycle(asof=None)` 的
     测试都在改写真实 `data/heartbeat.json`**。危害不是脏文件而是看门狗失灵：
     跑一次测试就会让心跳声称"深周期刚刚完成、positions=0"，从而在之后 26 小时
     内压制真实的停摆告警。已改为调用时解析 HEARTBEAT_PATH + 新增
     `tests/conftest.py` autouse 重定向；两盘 live 心跳已恢复为 08-28 的真实戳
     （P1 deep 20:18:17/cycle143、P2 deep 20:21:50/cycle77）——恢复值精确到秒，
     亚秒部分在污染中丢失，对 26 小时阈值无影响。
  ⑩ **二号盘周复盘上线**：`AgenticTradingWeeklyReviewP2`（周六 09:30，错开 P1 的
     08:30），XML 入库 `config/scheduled-tasks/`。此前 P2 从未跑过周复盘，
     其 `research/weekly-review-2026-08-22.md` 是克隆残留（与 P1 字节相同、
     覆盖 P2 还不存在的时段）。P2 的 `signal_outcomes` 也从 0 → 124 条。
  ⑪ **重跑 audit（`data/backtest/audit/20260830T072459Z/`）——项目首份多重检验校正，
     且首次让 manifest 对齐 live `risk.yaml`。两个结论必须分开读：**
     - **Deflated Sharpe 有了**：Sharpe 1.06 / 4,813 obs / skew 0.38 / kurtosis 9.32，
       27 次 look 的运气基准 0.0200 → **DSR 0.999，过 95% 线**；
       该 Sharpe 可与运气区分所需的最短回测长度 **3.7 年**。
       但工具自己写明：27 个 spec 是"对同一策略的重复观察，不是完整参数搜索，
       trial 数低估了真实的多重检验负担"——**这是下界，偏乐观**。
     - **PBO 仍然没有**：`status: skipped`，原因"fewer than 2 curves share a common
       date range of 17+ sessions (1 usable)"。CSCV 需要多条共享日期区间的净值曲线，
       而各 spec 的区间/宇宙不同。**不要对外说"我们有 PBO 了"。**
  ⑫ **新风控让回测成绩变差，且把与朴素基线的差距几乎抹平（重要，先记录不行动）：**
     同一引擎、同一 12 只旧书，只有 config 从 08-22 快照换成当前 live：

     | spec | 旧 config | 当前 live config |
     |---|---|---|
     | current_full CAGR | 13.6% | **11.3%** |
     | current_full Sharpe | 1.116 | **1.059** |
     | stress_full Sharpe | 1.113 | **0.942** |
     | **baseline_simple_trend Sharpe** | 0.889 | **1.000（反而变好）** |
     | alpha vs exposure-matched SPY | +7.89% [+3.44, +12.23] | **+5.52% [+2.12, +9.19]** |
     | Sharpe 差 95% CI | [+0.150, +0.817] | **[+0.015, +0.645]（下界贴近 0）** |
     | 平均敞口 / 买入次数 | 54.8% / 454 | 49.2% / 378 |
     | MaxDD | −20.9% | −21.0%（**没换来更浅的回撤**） |

     关键含义：**复合 quant 相对朴素趋势规则的优势从 Sharpe 0.227 塌缩到 0.059，
     CAGR 已经打平（11.3% vs 11.3%）。** 以前用来为复合评分辩护的"1.116 vs 0.889"
     在当前配置下不再成立。
     **但这不构成回滚理由**：全部是旧 12 只幸存者书上的 in-sample 数字（铁律 #2/#3），
     不描述 live 成长池；新风控的动机是风险纪律而非收益。此条按预注册节奏进季度
     评审，**本次不动任何参数**。
  基线更新：P1 424 / P2 405 全绿，`check_p2_sync.py` exit 0。
  文档同步更新：`ACTIVE-BOOK-VALIDATION-PLAN.md` 新增 §1.4（持仓过半不属于本池：
  池外 $4,717 = 净值 47.1%）与 §7（遗留仓位口径）；
  `research/weekly-review-2026-08-29.md` 顶部加了更正块（现金误诊、
  churn 可结案、0.30–0.45 结论出自被截断视图）。

  **未做 / 留给下次：**
  - **成交落库**：原计划有误。`decisions` 的 DDL 注释表明 `fill_price` 留空是
    刻意设计，`dashboard/broker_read.fills()` + `trades.round_trips()` 已有完整的
    成交与回合盈亏核算。真实缺口窄得多——那套核算只活在**手动启动、且只有 P1 有**
    的 dashboard 里，日报/周报够不着。正确做法是把已有 round-trip 核算接进
    `daily_report`，而不是往 journal 里复制第二份真相源。
  - **PBO 仍缺**：见 ⑪。要让 CSCV 可算，需要多条共享日期区间的净值曲线
    （例如让 `experiments.surface_overfit_report` 的 3×3 网格在同一区间上出曲线）。
  - 库里 4 行 `OrderStatus.ACCEPTED` 是 08-18 首个周期的历史遗留，`_enum_str`
    之后的代码路径已正确，**未改写历史行**（不动审计轨迹）。

  ⑬ **补：P2 的入口文档（同日追加）。** 此前 `Trading-P2` 里没有任何纪律文件，
     在那个目录工作的会话看不到铁律；而它的 `README.md` 是 P1 的**旧**副本
     （758 行，零处提到 rotation/roster），其中一句
     *"watchlist.yaml — hand-maintained … edit freely"* 对 P2 是**危险的**：
     P2 的 watchlist 由 `p2_roster` 每日两轮机器生成，手改会被覆盖并污染实验。
     处置：
     - 新建 `Trading-P2/AGENTS.md` —— **指针式**，只写 P2 独有事实（实验定位与
       预注册判负线、三条最易犯错、克隆残留清单、P2 自己的 5 个计划任务）。
       纪律正本仍只有本文件一份，避免两处漂移。
     - `Trading-P2/README.md` 顶部加警示块；就地更正 `watchlist.yaml`（机器生成）、
       `research.yaml`（P2 `enabled: false`，研究库一节不适用）、
       `risk.yaml`（冻结区 + 必须与 P1 逐字节一致，勿照 "tune here first"）三处。
       其余 700+ 行讲共享引擎的内容准确，未重写。
     遗留未处理：**`config/risk.yaml` 的双盘一致性没有任何自动化守护**——
     `check_p2_sync.py` 只比对 `src/**/*.py`。这是实验设计的核心前提却只能靠人守，
     值得下次加进同步检查。
- 2026-08-31（Claude）：**Dashboard 改造的 Phase 0**（设计+证伪，零引擎改动）。
  产出 `HANDOFF-DASHBOARD.md`（任务书，11 节）+ `research/dashboard-phase0-demo.py`
  （可跑的设计稿）+ `check_handoff_anchors.py` / `handoff-anchors.json`。
  两个 commit：`bc78b21`、`9e20d8d`。**这是本文件"不要新建散装 handoff 文件"
  规则的一个有意例外**——它是进行中的任务书，做完后归档到 `research/archive/`。
  - **动手前先跑 `python check_handoff_anchors.py`**：任务书里 53 处 `文件:行号`
    都锚定到具体代码行，脚本告诉你哪些漂了（`--fix` 自动改正，`--snapshot` 重设基线）。
    **MISSING 是唯一的非零退出**，意思是被引用的代码已被改写、相应结论可能失效，
    renumber 救不了，要人重新判断。当前状态：**OK 53 / MOVED 0 / MISSING 0**
    （基线 `ba9e4f5`）。
  - 任务书 §2 记了**三个已证伪方案**，接手者别重走；§0.1 记了**用户已拍板、
    不必重新讨论的决策**（只绑 `127.0.0.1` 不做响应式；实时三件事全都要；
    允许 dashboard 跑只读的 `signals/` 纯函数计算；Phase 2a 接受改 `run.py`
    吐事件，代价是要过 P2 同步）。
  - §11 建议**每个 Phase 开新会话**、按 1 → 2a → 2b/2c → 3 → 4 顺序，
    2a 先独立冒烟验证（后两阶段依赖它的事件格式），Phase 4 最后（唯一牵涉双盘真实同步）。
  - 本轮**没碰引擎、没碰冻结区**，测试基线不变（P1 424 / P2 405）。

## 运维速查

- 计划任务：**10 个** `AgenticTrading*`（Task Scheduler），入口 `run_cycle.cmd` /
  `run_weekly_review.cmd`。深周期 9:45/16:15 ET，fast scan 盘中每 20 分钟；
  周复盘两盘各一个（P1 周六 08:30 / P2 周六 09:30）。
- 看门狗：`python -m agentic_trading.heartbeat --check`（只信 live 戳）。
- 评估：`python -m agentic_trading.journal.evaluate`（avoid/hold 也出前瞻收益）。
- 因子诊断：`python -m agentic_trading.backtest.factor_ic --factor quant|rs`
  （带 95% block bootstrap 区间；以区间为准，不以点估计为准）。
- 全量审计：`python -m agentic_trading.backtest.audit`。**要 40–60 分钟，且产物
  只在全部跑完时才写盘**——中途被杀 = 目录空、什么都留不下（08-30 踩过一次）。
  在会话里跑必须真正脱开父进程（`nohup ... &`），否则会被任务生命周期带走。
  最新产物 `data/backtest/audit/20260830T072459Z/`，其 manifest 是**第一份**与
  live `risk.yaml` 对齐的快照；更早的 `20260822T183234Z` 描述的是旧 config。
