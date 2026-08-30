# Stage B 数据补齐规格（point-in-time 标普 500 价格档案）

**日期**：2026-08-27
**状态**：规格文档 —— 本轮**只定方案，不抓取**。任何实际下载需单独批准。
**依据**：`research/opensource-gap-analysis-2026-08-26.md` §7.1 第 1 项；Codex 审计边界
"Stage B 本轮只写规格、不抓取"。

---

## 1. 为什么这件事是钥匙

`audit.py` 的 Stage B 在 point-in-time 标普 500 上回测：历史上每一天用那一天真实的
成分股名单，包括后来破产、被收购、被踢出指数的公司。这是本仓库**唯一**能给出不含
后见之明证据的路径。它同时是两件事的前提：

1. audit verdict 从 inconclusive 变为有效；
2. §5.5 做空开闸条件所要求的"在无选择偏差宇宙上重跑因子分层测试"。

**预期管理：补齐的大概率是"失败者"，历史成绩会变差。这正是目的。**

## 2. 现状（事实，来自 `data/backtest/point_in_time/data_quality.json` 与 `STAGE-B-STATUS.md`）

| 项 | 值 |
|---|---|
| 成分段（membership intervals） | 950 |
| 已有价格文件 | 868 |
| 缺失 | **82 个文件 / 80 个 ticker** |
| 活跃成员-交易日覆盖率 | 84.85%（质量门要求 ≥ 99%） |
| 窗口 | 2007-04-11 → 2026-08-21（4,873 个交易日） |

质量门 7 道中 3 道未过：

- [ ] `all_price_files_present`
- [ ] `active_member_session_coverage_ge_99pct`
- [ ] `sector_neutral_benchmark_coverage_ge_90pct`（依赖前两者）

凭证现状：Alpaca ✅ 已配；Tiingo ❌ 缺 `TIINGO_API_KEY`；Alpha Vantage ❌ 缺
`ALPHAVANTAGE_API_KEY`。

现有抓取链（`backtest/pit_data.py`）：`--providers yahoo,alpaca,tiingo`，
按序回退，带 provider_cache。868 个已有文件即由该链产出。

## 3. 缺失清单的结构（决定策略的事实）

80 个缺失 ticker 几乎全是**退市股**，分三类：

| 类别 | 例子 | 数据可得性 |
|---|---|---|
| 破产程序中（Q 后缀） | LEHMQ 雷曼、BSC 贝尔斯登、WAMUQ 华盛顿互惠、EKDKQ 柯达、CFC Countrywide、CITGQ、ABKFQ、ANRZQ、BTUUQ、MTLQQ、SUNEQ、RSHCQ | 免费源基本归零；Yahoo/Alpaca 对退市代码无历史 |
| 被并购/私有化 | BNI、BOL、CEPH、COV、CVH、HNZ、KRFT、NYX、SIAL、XTO、MHS、MFE | 多数免费源在并购除牌后停止提供；Tiingo 对部分退市股有历史（需实测） |
| 代码更名/复用 | CMCSK、DJ 等 | 需人工核对是否其实以另一代码存在于缓存 |

完整清单：`data/backtest/point_in_time/price_gaps.csv`；ticker 样本见
`STAGE-B-STATUS.md`。**没有任何缺失名字被从 membership 里静默移除**——这是底线。

## 4. 补齐方案（回退链设计）

按"每 ticker 一条回退链"，每步记录来源与覆盖区间，全部写入 `price_manifest.json`：

1. **provider_cache 复查** —— 先确认缓存里是否已有部分区间（上一轮可能只缺末端）。
2. **Tiingo** —— 需用户提供 `TIINGO_API_KEY`（免费档即可试）。Tiingo 是免费源里
   对退市股支持最多的；对 80 个 ticker 全量试抓，记录每个的命中/覆盖区间。
   *待验证假设：Tiingo 免费档是否提供退市股完整历史，规格阶段不承诺。*
3. **Alpha Vantage** —— 需 `ALPHAVANTAGE_API_KEY`。`TIME_SERIES_DAILY_ADJUSTED`
   对活跃股好用，对退市股大概率无数据，作为第三回退。
4. **Stooq**（无需 key）—— 对部分老 ticker 有 CSV 历史，可作补充源，需新增
   一个小抓取器（~50 行）。
5. **Yahoo 变体代码** —— 退市股在 Yahoo 有时以 `.PK`、`^` 变体或历史代码存在
   （如 LEHMQ.PK）。属人工逐名核对，只适合个位数兜底。

## 5. 免费源永远拿不到的名字怎么处理（核心取舍）

三个选项，按推荐排序：

**选项 A（推荐）：退市收益 stub，显式记账。**
对确认拿不到价格的名字，在其 membership 终止日附加一个**预注册的退市收益常数**
（学术惯例：破产类 −100% 或 Shumway 式 −30% 违约退市惩罚；并购类用公开对价的
最后一跳，若对价也不可得则用 0% 并计入"温和情形"）。每种常数作为
`cost_scenarios` 式的敏感性档位跑三遍（−100% / −30% / 0%），结论报告区间而非
单点。**代价**：精确度让位于诚实；**收益**：方向正确——宁可高估失败者的坏，
不可假装它们不存在。

**选项 B：付费源（Polygon.io / CRSP）。**
Polygon 付费档提供退市股完整历史（含 OTC）。一次性历史抓取，成本约一单月度订阅。
**代价**：引入付费依赖与供应商锁定；**收益**：真数据，stub 降级为交叉验证。
若用户愿意付费，这是质量最高的路径。

**选项 C：丢弃并在结论中声明覆盖率上限。**
**不推荐**——这正是 Stage B 要消灭的偏差本身；一个没有雷曼和贝尔斯登的
2008 年回测不配叫 2008 年回测。仅在 A/B 都不可行时作为明确的、写进报告首页的
降级声明存在。

## 6. 对质量门与 audit verdict 的影响

- 选项 B 全中：覆盖率 → ≥99%，三道门全过，verdict 有效。
- 选项 A：stub 名字的价格序列需在质量门里**单独标注**（新增一个
  `stubbed_delisting_count` 指标而不是篡改 coverage），门阈值不变；verdict 有效，
  但报告必须带"含 N 个 stub 名字，敏感性区间 [a, b]"的字样。
- 无论哪条路，`data_quality.json` 的 `status` 只能从 `incomplete` 变为新一次
  实跑的结果，不得手改。

## 7. 执行检查单（下一轮批准后照做）

1. [ ] 用户提供 / 确认不提供 `TIINGO_API_KEY`、`ALPHAVANTAGE_API_KEY`（决定 §4 链路深度）
2. [ ] 用户选择 §5 的选项（A stub / B 付费源 / C 降级声明）
3. [ ] 逐 ticker 回退链抓取，全部命中情况写入 `price_manifest.json`
4. [ ] 重跑 `pit_data.py` 质量门，核对 `STAGE-B-STATUS.md` 自动更新
5. [ ] 若含 stub：实现退市收益 stub 注入（`pit_data.py` 或 audit 层），三档敏感性
6. [ ] Stage B 实跑 → audit verdict；报告与 868/950 版本并列展示，不许只报新版
7. [ ] 在无偏宇宙上重跑 `factor_ic.py` 分层测试（§5.5 做空开闸条件的前提）

## 8. 明确禁止

- 不得把缺失名字从 membership.csv 里删掉来"凑过"质量门。
- 不得用今天的代码表去映射历史 ticker（更名污染，见 data_quality.json 的
  ticker_label_caveat）。
- 不得在未经批准的情况下发起大规模抓取（对数据源是数百次请求）。
- 抓取结果一律走 provider_cache + manifest 哈希，可复现。
