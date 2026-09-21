# c2c_a7e2 ITERATION 3 复审原文（ChatGPT 6 Pro，2026-09-20）

> 原文逐字存档。结论：STATE: PLAN / REVIEW: CHANGES_REQUESTED。
> CLOSED: R1、R5。OPEN: R4（中）、R6-A、R6-B、R8（中）+ 证据补齐（npm-build.txt）。
> 注：本轮复审分两段——先因连接器令牌失效 BLOCKED_CONNECTOR（CODE_VERDICT: NOT_ISSUED，
> 明确"不因连接失败改代码/重跑/加迭代号"），领导重建连接器（换固定域名
> connector.anbostein.indevs.in + 重新配对）后重发同一 EXECUTED，恢复复审。

```
[C2C]
STATE: PLAN
TASK_ID: c2c_a7e2
ITERATION: 3
WORKSPACE: Trading
BASELINE: 97a65d6
REVIEW: CHANGES_REQUESTED
CLOSED_THIS_ITERATION: R1、R5
OPEN_ITEMS: R4、R6（两处）、R8

核验结果

连接已恢复，工作区及 HEAD 均已确认。已按 list → read 读取本轮 execution_output(id=10)，正文未截断；另读取三盘 pytest、同步记录、§19 台账，检查相关当前 HEAD diff、新增模块及贯通测试。HEAD 即指定基线，本轮没有基线歧义。

原始输出确认：P1 829 / P2 678（1 skipped）/ P3 991 / P4 955 passed；65 个共享文件一致。本端未重跑命令。

R1 可以关闭：UUID 失败不再阻止业务保存，提交后的回执构造失败不再触发假 save_failed，三个 sizing 早退已采用容错诊断。R5 本轮退回的两条也可以关闭：新漏斗的 None 提交结果记为 unknown；部分成交与取消终态在观察、恢复及聚合中分别保留。此前关闭的 R2、R3、R7、R9 保持关闭。

R4 的普通唯一归属链，以及 R8 的 TTL／清除、未终结部分成交三个原反例均已修复。仍有以下四处缺口；下面的反例是从当前代码推导，不是本端重跑所得结果。

R4｜中：唯一归属检查只保护无身份观察，恢复器与 fills 仍可绕过它

位置：execution_funnel.py:190–240；dashboard/views.py:830–852, 1078–1125, 1198–1218

新聚合能够把无 intent_id 的观察接回唯一原提交，两个真实三周期测试也验证了正常路径。问题在于其他入口没有遵守相同归属约束。

恢复器的第二次查询只按 order_id 读取历史，没有 mode／book 条件；遇到多个提交版本时，按倒序找到第一个非空 intent_id 就使用它，没有检查冲突。这样由恢复器生成的观察已经带有一个"确定身份"，聚合便不会进入仅处理无身份观察的冲突检查。

fills 则仍按裸 order_id 传给每条意图链，未使用 unique_owner/conflict_keys。直接扩展现有 test_conflicting_order_ownership_is_unattributable：两个版本都记录了同一 order_id，再传入该订单的一条完成成交，两条链都能取得同一份 fills，完成数可能由 0 变为 2。当前测试使用 fills=None，没有覆盖这一旁路。

修改：将订单归属判定贯通恢复、观察接链和 fills 接链：同书、同 mode、唯一可证实的原提交身份才可归因；冲突不得按最新提交、symbol 或第一个匹配选择。冲突订单仍可保留订单级观察／成交证据，但不能归功于任一意图，更不能重复计数。共享观测层不得为此导入 dashboard。

红测试：沿用现有冲突 fixture，分别通过真实 observe_unresolved() 生成观察，以及传入匹配 fills；断言不选择任一版本、不重复计完成，并保留无法归因的原因。补不同 mode 的同 order_id 历史不能抢走身份或压制 paper 回查；现有唯一归属、同日／跨日三周期贯通测试保持通过。

R6-A｜中：TTL 和 gap 删除失败仍无持久归因

位置：run.py:1166–1180, 1232–1240；对照已修路径 run.py:1188–1206；测试缺口 test_execution_funnel.py:734–775

clear_failed 目前只接在"已持仓／已有 pending buy"的清除分支。TTL 和 gap 分支仍是：删除成功才记录终态；删除失败后不写任何漏斗事件，直接继续。

因此，先前有 sizing／等待记录的意图，本轮因 TTL 或 gap 被判定不可执行、但删除失败时，SQLite 漏斗仍只显示旧原因。不伪报删除成功已经做到，但失败仍没有被归因。

修改：给这两个分支补非终态删除失败事件，保留 intent_id、原判定原因 ttl/gap 及可用说明；不要把它们记成成功丢弃，也不要改变原删除调用、重试或订单逻辑。复用现有 clear_failed 语义即可。

红测试：参数化 TTL 与 gap，单独让 clear_trade_intent 抛出 SQLite 异常、事件写入正常；完整经过 flush→journal→聚合，断言意图仍在、无成功删除终态、有本轮失败原因，不继续展示为只有旧 sizing／等待记录。现有已持仓／pending buy 失败用例保留。

R6-B｜中：新增归因字段到达 API 后，被 Today 丢掉

位置：api.ts:363–392；Today.tsx:544–576, 620–621, 656–661

后端已输出结构化数据但前端契约和展示没有同步：
- run_skips[].unprocessed、detail：类型未声明，展示只取 run_id 和 reason，受影响标的名单不可见。
- degraded.identityless_by_mode 及每行 mode：不展示分组也不展示每行 mode；paper、dry_run、未分模式记录被并列写成"本轮已观察"。
- degraded.unattributed_order_events：类型与渲染均未接入，订单归属冲突等具体原因没有上屏。
- 有无法归因记录但无已归因 chain 时，空状态仍直接写"本会话没有 BUY 决策，也没有承接意图"——把"无法建立完整链"表达成了"没有发生"。

修改：补齐 TypeScript 字段并直接展示：受影响名单、mode 分组／标签、无法归因的 order_id 与原因。有缺失证据时空状态只能说明"没有可完整归因的链"，不能断言没有 BUY。无需解析 detail、增加查询或改动交易模块。

红测试：实际聚合返回值送入现有离线渲染器，同时包含订单上限名单、混合模式旧身份记录、订单归属冲突。断言标的、模式、order_id 和原因可见，且不出现错误的"没有 BUY"结论。不能只断言 JSON 已包含字段。

R8｜中：部分成交后取消，sizing 区仍宣称余单未终结

位置：Today.tsx:391–421 及 ChainCard／CarryoverCard 传参路径；测试缺口 test_frontend_render.py:720–857

本轮把 partial 从终态集合移除，修复了"仍在执行的部分成交被说成已终结"。但 SizingLine 随后对所有 phase="partial" 都输出"提交时快照（部分成交，剩余订单未终结）"。R5 已正确保留 phase="partial" 与 terminal_status="canceled" 两个独立事实。然而 SizingLine 没有接收对应订单的 terminal_status，所以部分成交后取消时，订单行会显示"已终结"，其下 sizing 区却同时显示"剩余订单未终结"。

修改：使用快照对应订单的独立终态事实判断，而不是从 partial 推断活／死。部分成交且已取消应注明余单已取消、快照为历史；部分成交且尚无终结证据保持原未完成口径。必要时让快照携带对应 order_id，避免把同一意图其他尝试的状态用于这张快照。不要将 partial 改成 complete，也不要改变恢复器行为。

红测试：从真实 order_submitted 事件开始，经 FunnelRecorder.observe() 写入 canceled + filled_qty>0，再聚合并渲染：取消与部分成交同时可见、不计整单完成、sizing 不再说余单未终结。以仍在 partially_filled 的订单为对照；现有 TTL／清除历史标签测试继续通过。

证据补齐与下一步

本轮构建有用户报告及 §19 成功摘要，但指定证据目录未包含构建文件；实际读取 research/c2c_a7e2-executed-iteration-3/npm-build.txt 返回 FILE_NOT_FOUND。这不等于构建失败，只是尚未读到本轮原始构建输出；不会拿第二轮输出替代。

下一轮仅收尾以上范围。恢复器及 run.py 修改继续由领导单人负责；前端接线与渲染测试可由前端负责人完成。保留已通过的三周期、身份故障和原提交轨迹测试，不改写 fixture，不动冻结区、分析师、订单参数或 paper-only 边界。

通过本任务可读 execution_output 发布定向红绿回归、四盘全量、同步及本轮前端构建／渲染输出。

NEXT_EXPECTED_STEP: 修复后回报 EXECUTED / ITERATION: 4。本轮不进入 DONE。
```
