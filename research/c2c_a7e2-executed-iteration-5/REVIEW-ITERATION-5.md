# c2c_a7e2 ITERATION 5 复审原文（ChatGPT 6 Pro，2026-09-20）

> 结论：STATE: PLAN / REVIEW: CHANGES_REQUESTED。R6-B 关闭；上轮 502 证据补读完成
> （三盘/构建/恢复器冲突/mode 隔离/冲突 fills/TTL·gap 回归均已核）。OPEN: R8 一项。
> 台账：PROGRESS.md §24。

```
[C2C]
STATE: PLAN
TASK_ID: c2c_a7e2
ITERATION: 5
WORKSPACE: Trading
BASELINE: 97a65d6
REVIEW: CHANGES_REQUESTED
CLOSED_THIS_ITERATION: R6-B；R4／R6-A 上轮待补证据
OPEN_ITEMS: R8（1 项）

核验结果

已确认工作区与 HEAD，按 list → read 读取本轮 execution_output(id=12)：841 passed、exit 0、正文未截断。另读取本轮三盘、同步和构建原始输出，确认 P2 682（1 skipped）/ P3 997 / P4 961 passed，65 个共享文件一致，TypeScript 检查及 Vite 构建通过。本端未重跑命令。

上轮因 502 中断的三盘测试、构建输出，以及恢复器冲突／mode 隔离、冲突 fills、TTL／gap 清除失败回归均已补读。相应证据缺口关闭。结束前补读 run.py 两次返回 502；该文件的相关修改已在上一轮审阅，本轮不据此提出新的交易代码问题。

R6-B 可以关闭。无法归因 BUY、孤儿／来源不明意图和解析异常已参与空状态判断；未归因成交标题不再预设原因，逐行展示后端 note。相关聚合→渲染断言已检查：tests/test_frontend_render.py:1237–1361。

R8｜中：整条链的终态仍优先于快照对应订单

位置：Today.tsx:438–475；关联数据语义 views.py:763–778。

本轮修复了 chain phase 为 partial 时的配对失败和不同尝试状态，但另一个旧分支仍在：`const orderTerminal = TERMINAL_CHAIN_PHASES.has(phase ?? "")`——这里 phase 仍来自整条链，且该 else if 排在已配对订单判断之前；"对应订单／终态未能确定"仅在 `phase === "partial" && !snapOrder` 时出现。

反例（分支推导，非重跑）：沿用"一条链、两个尝试"结构——订单 A 已完整成交、订单 B 后提交仍 partially_filled、最新 sizing 快照来自 B；后端 _chain_phase() 因 A 完成返回 complete；即使 snapshotOrder() 唯一正确匹配 B，SizingLine 仍先进入 orderTerminal，把 B 的快照显示成"历史快照（链已终结：整单完成）"——B 的未完成状态被 A 的终态覆盖。同样，链 complete／terminal_unfilled 且快照无法配对时不进未知分支，仍借链终态。现有新增测试把 AMBG/ZMTS/DIFF/缺时间场景的 chain phase 都固定为 partial，未触发该优先分支。

最小修改：只收尾 Today.tsx 的快照状态来源。意图自身 discarded/cleared/superseded 判断保留；订单相关判断必须在配对之后——唯一配对成功：用 snapOrder 自己的 phase、完成证据、部分成交证据及 terminal_status（该订单已确认完成时，完成证据优先于历史部分成交证据），不得用整条链 phase 决定快照的订单终态；需要订单关联但未能配对：统一"对应订单／终态未能确定"，不论链是 partial/complete/terminal_unfilled，不借其他尝试状态。可保留唯一时间匹配（限提交事件快照，无法可靠匹配即降级）。不要求后端 order_id、不要求改 run.py。

回归测试：①A complete、B live partial、快照唯一匹配 B——B 的 sizing 区不得显示"链已终结：整单完成"，A 的完成事实保留在 A 的订单明细；②链 complete／terminal_unfilled 但快照缺时间/零匹配/多匹配——显示未确定、不借终态。保留全部现有对照。断言定位到对应 sizing 区域，不因另一订单明细合法出现"整单完成"而误判。

下一步

仅修正这一项 R8 的分支优先级和状态来源。R6-B 及此前关闭项不重开，轨迹 fixture 不改写，冻结区、分析师、订单参数和 paper-only 边界不变。

NEXT_EXPECTED_STEP: 修复后发布定向回归、前端构建／渲染、四盘测试及同步的可读输出，回报 EXECUTED / ITERATION: 6。本轮不进入 DONE。
```
