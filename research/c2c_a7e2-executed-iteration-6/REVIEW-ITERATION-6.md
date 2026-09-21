# c2c_a7e2 ITERATION 6 复审原文（ChatGPT 6 Pro，2026-09-20）

> 结论：CHANGES_REQUESTED，OPEN: R8（仅剩 submit_rejected 例外）。证据全部核验无缺口。
> 复审明确：其他三盘若无源码或测试变更可沿用本轮已核验记录（前端只在本盘）。台账 §26。

```
[C2C]
STATE: PLAN
TASK_ID: c2c_a7e2
ITERATION: 6
WORKSPACE: Trading
BASELINE: 97a65d6
REVIEW: CHANGES_REQUESTED
OPEN_ITEMS: R8（仅剩 submit_rejected 例外）

核验结果：execution_output(id=13)=844 passed 已读；三盘/同步/构建全部核验；无连接缺口。上轮点名的三处均已修复，其他关闭项保持关闭。

R8｜中：保留的拒绝例外仍会把旧尝试的结果赋给新快照

位置：Today.tsx:438–486；views.py:762–778, 889–912。

`unpairedChainRejected = unpaired && phase === "submit_rejected"`——"链为 submit_rejected"不证明"携带当前快照的那次提交被拒绝"。_chain_phase() 无带 order_id 订单时任一历史尝试 submit_rejected 即优先返回拒绝；sizing 快照来自最后一条含 sizing 的事件，两者可属不同尝试。

反例（分支推导）：同一 intent 两次提交——A 较早（order_id=None，submit_status="rejected"）、B 较晚（order_id=None，submit_status="unknown"，带最新快照）→ 链级 phase=submit_rejected，snapshotOrder() 排除所有无 order_id 条目，例外把 B 的结果不明快照显示为"历史快照（链已终结：提交被拒绝）"——借用 A 的结果。即使只有 A 被拒后下一轮新 sizing 否决快照，也借旧拒绝。

最小修改：只收尾快照展示。将提交尝试的结果与是否取得 order_id 分开：无 order_id 的尝试也可有可核验的提交事件。快照只有可靠对应到该次尝试才用其 submit_status/submit_phase；无法对应保持未知，不回退链级拒绝。可扩展匹配使其对 event_kind="order_submitted" 的快照匹配唯一提交尝试（含 order_id 为空条目），或直接携带该事件已有关联信息。不生成假 order_id、不把普通 sizing 事件按时间误配。单次明确拒绝展示保留，证据必须来自那次提交。

红测试：①A 明确拒绝→B 提交结果不明、最新快照属 B——B 的 sizing 区不得显示被拒绝/链已终结；A 的拒绝事实在 A 自己明细。②A 拒绝→后续 sizing 否决或快照关联缺失/歧义——不把旧拒绝套给新快照。③保留"唯一明确拒绝尝试+它自己的提交快照"对照（真实拒绝仍可展示）。继续用 chain_sizing_region()；本轮三回归和此前控制保持通过。无需动 run.py/风险参数/fixture。

NEXT_EXPECTED_STEP: 修正后回报 EXECUTED / ITERATION: 7，发布定向渲染回归、P1 全量、前端构建及同步输出；其他三盘若无源码或测试变更，可沿用本轮已核验记录。本轮尚不进入 DONE。
```
