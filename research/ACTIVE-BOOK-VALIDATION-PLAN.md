# 当前持仓池（Active Book）验证计划

**日期**：2026-08-23
**适用对象**：`config/watchlist.yaml` 当前的成长池（MU, SNDK, AMD, NVDA, AMZN,
ANET, CRDO, ALAB, NBIS, HPE, FLEX, CIEN, ARGX, VEEV）。
**地位**：本文件是当前这本书的证据契约。在它被替代之前，引用旧书的任何回测
数字来为这本书辩护都是无效论证。

---

## 1. 事实陈述（先承认，再谈方法）

1. **历史回测不覆盖本书。** 2007-04-11 → 2026-08-21 的 +15.1% CAGR / Sharpe
   1.16 / 2008 年 −12% 是在旧的 12 只分散大盘股上测的（`HANDOFF-BACKTEST.md`）。
   当前池约半数上市不足 3 年（SNDK、ALAB、NBIS 等），根本无法回放；能回放的
   名字也带着"今天还在池里"的幸存者选择。
2. **选池规则本身就是动量规则。** 本池由 SA Quant 高分筛选产生——"过去表现
   好"是入池条件。用历史数据检验一个按过去表现挑选的组合，无论怎么切窗口都
   是 in-sample。
3. **集中度是真实的。** 14 只里 12 只是 Technology，MU–SNDK 相关 ~0.87，整本
   书实质是一个 AI/算力主题篮子。
4. **当前持仓过半不属于本池（2026-08-30 补记）。** 6 个持仓里只有 AMZN、NVDA
   在池内；ACET、IQV、JPM、MSFT 是 2026-08-22 换池之前的遗留仓位，合计
   **$4,717（持仓市值 56.7% / 净值 47.1%）**。journal 里决策数最多的也是这四只
   （MSFT 50 / JPM 47 / IQV 42 / ACET 41）。**观察期前段的数据有超过一半与
   "成长池好不好"无关**，判读时必须按 §7 的口径拆开，不得整本书混算。

## 2. 唯一有效的证据来源

| 来源 | 用途 | 状态 |
|---|---|---|
| **Paper-forward 实盘记录**（`journal.db` + `python -m agentic_trading.journal.evaluate`） | 主证据：真实决策的前瞻收益/MFE/MAE | 持续累积 |
| **SPY 与 exposure-matched SPY+cash**（`backtest/benchmarks.py`） | 必须打败的下限基准 | 已有 |
| **252 候选池随机等权 placebo**（audit 的 placebo 机制，`research/core-candidate-pool-2026-08-22.csv`） | 回答"换成随机选的 14 只会不会一样好"——这是对 SA Quant 选池本身的检验 | 已有机制，随 audit 运行 |
| 2023-2026 holdout / 任何旧书回测 | ❌ 对本书无效（见 `HOLDOUT-LEDGER.md`） | 封存 |

## 3. 判读协议

- **最短观察期**：paper-forward 满 6 个日历月后才做第一次正式判读；此前一切
  波动都是噪音，`evaluate` 的 MIN_REPORT_N=5 纪律照常执行——小 n 如实展示，
  不当作发现。
- **主指标**：相对 exposure-matched SPY 的超额收益与 Calmar；次要：MaxDD 是否
  在预算内（book stop-risk 上限 6% 的语义）。
- **判读节奏**：每季度一次，写入 `research/` 下带日期的评审文件，不许盘中
  心血来潮加看。

## 4. 升级 / 降级规则（预注册）

- **降级触发**：连续两个季度同时满足 (a) 跑输 exposure-matched SPY 且 (b) 实现过 >20% 的
  book 级回撤 → 把池子缩回分散核心（旧 12 只结构），成长名字降级为小仓位或移出。
- **维持条件**：任一季度跑赢 exposure-matched SPY 即重置连续计数。
- **升级**：无。这本书的目标是不输给指数的风险调整收益，不存在"证明自己后
  加杠杆"的条款。

## 5. 集中度设计意图（显式确认）

`config/risk.yaml` 的 `max_sector_pct: Technology: 0.35` 对这本 12/14 都是
Tech 的书是**事实上最强的约束**——大多数信号最终会被 sector_room 缩掉或否决。
这不是副作用，而是设计：它就是这本书的主题集中度上限，替代了 theme basket
机制（`theme_symbols` 留空）。相关性减半（corr>0.75 → size×0.5）只控制单笔
叠加，不控制主题总量；行业 cap 才是总量阀门。

**不要**因为"信号总被 sector cap 否决"而放松这个数字——那等价于主动放大单一
主题敞口，应当作为独立的风险决策走完整的评审流程（并更新本文件第 1 节的事实
陈述）。

## 6. 明确禁止

- 把旧书 2007 起的 CAGR/Calmar 引用为新书的预期。
- 为了让新书"可回测"而把它塞进历史 replay 并引用结果（幸存者选择直接进结论）。
- 在未满 6 个月 paper-forward 时基于短期表现调 `buy_threshold` 或换池。

## 7. 遗留仓位口径（2026-08-30 决定）

**定义**：持仓中不在 `config/watchlist.yaml` `symbols`（= `settings.core_watchlist`）
里的名字。它们走 `run.py` 的 off-watchlist holdings 路径，该路径不调 LLM，
因此 `combined == quant`。

**规则**：遗留仓位适用 `risk.yaml` 的 `legacy_sell_threshold: 0.10`，
池内名字维持 `sell_threshold: -0.25` 不变。一视同仁按分数评判——指标掉下来就
退出，指标还好就继续持有，只是门槛比池内高。

**为什么**：遗留仓位占着与被验证名字同一份敞口预算和 book 止损风险预算，却对
本文件要回答的问题不提供任何信息。2026-08-29 的实测状态是这个矛盾的极端形式：
持仓 83.1% vs neutral 敞口上限 65%、book 止损风险 6.02% vs 上限 6.00%，两把锁
同时锁死，**即使现金 100% 也开不出任何新仓**；而 neutral 体制下 TRIM 不启动
（`trim_regimes: [risk_off]`），敞口只进不退，没有任何回到目标的路径。
更高的退出门槛让预算随遗留仓位自然退出而回流到池内名字。

**这不是调参**：`buy_threshold`、`sell_threshold`、仓位常数、池子、sector cap
全部未动，本条只对**已退役宇宙**定义记账边界。决定时点 2026-08-30，依据是当时
的分数分布（最近 10 次读数：JPM +0.035~+0.056 有 10/10 低于 0.10，
ACET −0.002~+0.146 有 9/10，MSFT +0.593~+0.612 与 IQV +0.657~+0.666 从未接近）。

**判读口径**：§3 的正式判读只用池内名字计算超额收益与 Calmar；遗留仓位单独
列示，不混入本池结论。二号盘的 roster 总把持仓保留在 watchlist 内
（`p2_roster.py`），因此该路径在 P2 永不触发。
