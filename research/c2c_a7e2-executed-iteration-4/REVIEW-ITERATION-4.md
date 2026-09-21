# c2c_a7e2 ITERATION 4 复审原文（ChatGPT 6 Pro，2026-09-20）

> 结论：STATE: PLAN / REVIEW: CHANGES_REQUESTED。R4（恢复器+fills）、R6-A 关闭（源码已核）。
> OPEN: R6-B、R8（纯前端展示边界）。EVIDENCE_STATUS: PARTIAL_CONNECTOR_502——三盘/构建/新增回归
> 证据因连接 502 未读全，要求连接恢复后补读，不因 502 重跑。台账：PROGRESS.md §22。

```
[C2C]
STATE: PLAN
TASK_ID: c2c_a7e2
ITERATION: 4
WORKSPACE: Trading
BASELINE: 97a65d6
REVIEW: CHANGES_REQUESTED
OPEN_ITEMS: R6-B、R8
EVIDENCE_STATUS: PARTIAL_CONNECTOR_502

核验范围与证据限制

已确认 Trading 工作区及 HEAD，按 list → read 读取本轮 execution_output(id=11)：837 passed，exit 0，正文未截断。已独立读取当前 run.py、dashboard/views.py、Today.tsx 的完整 HEAD diff，以及 execution_funnel.py 的恢复与观察调用链。

随后连接连续返回 502，包括本轮证据目录、npm-build.txt、新增前端测试及工作区重试。因此，本轮三盘测试、构建和新增回归的原始输出尚未全部读到；不会以执行摘要或上一轮输出替代。本端未重跑命令。这里的构建记录读取结果是连接错误，不是文件不存在或构建失败。

从已取得源码可确认：R4 的恢复查询已过滤 mode，冲突提交不再选择最新身份，fills 已按唯一归属限制；R6-A 的 TTL／gap 删除失败也已补非终态事件。本轮不再针对这些原反例提出代码返修，相关测试验收仍待补读。

R8｜中：快照无法唯一配对时，仍断言"剩余订单未终结"

位置：Today.tsx:391–403, 428–452

前端按时间配对本身不是退回原因；问题是配对失败后的分支与所报告的"ambiguous stays silent"不一致。snapshotOrder() 在缺少时间、没有匹配或多个订单同时间时返回 null。但 SizingLine 随后执行 `partialRemainderDead = phase === "partial" && !!snapOrder?.terminal_status`——配对失败时该值为 false，只要整条链 phase === "partial" 仍输出"提交时快照（部分成交，剩余订单未终结）"。同一链有两个相同提交时间的订单、相关订单实际已取消时，订单明细显示已终结，sizing 区仍肯定声称余单未终结。没有找到对应订单不能作为订单仍存活的证据。当前分支也仍使用整条链的 phase 而不是已配对订单自己的 phase。

最小修改：把"唯一配对成功"和"未能确定对应订单"分开。成功时用该订单自己的成交程度与终态；失败时只展示原 sizing 快照并标明对应订单或终态未能确定，不能借用整条链的 partial 状态断言余单存活。可保留前端唯一时间匹配但明确降级行为；也可让后端快照带 order_id——不要求为实现偏好改换方案，要求的是无法配对时不作确定断言。

回归测试：真实 recorder→聚合→渲染路径上增加：缺快照时间、零匹配、两订单同提交时间、同一链不同尝试状态不同。配对不确定时不得出现"剩余订单未终结"或从其他尝试借来的终态；唯一配对的 canceled＋partial、partially_filled 两对照保持正确。

R6-B｜中：两类降级信息仍被错误的页面结论覆盖

1) 空状态遗漏已有无法归因分类（Today.tsx:673–684）。hasUnattributable 未检查 unattributed_decision_events / orphan_intents / unknown_origin_intents / 解析异常。反例：一条缺 run_id 的 decision_buy，聚合给 unattributed_decision_events=1、chains/carryover 空——页面仍显示"本会话没有 BUY 决策"，下方同时提示"一条 BUY 决策事件无法归因"。另一场景：decision_buy 写失败、intent_created 写成功——后端列出孤儿意图，空状态却仍断言没有 BUY／承接意图。修改：把这些降级证据纳入空状态判断；存在妨碍完整归因的证据就用"没有可完整归因的执行链"并显示具体原因；不能把无法还原等同于没有发生。

2) 冲突 fills 的具体原因没有上屏（Today.tsx:696–716）。后端 unlinked_fills 已区分三种案型，前端统一用"未归因成交（order_id 在台账中无订单事件……）"且不显示 f.note——台账有两个冲突提交时页面仍说台账没有订单事件。修改：标题改中性"未归因成交"，逐条展示后端 note。无需新增查询或解析文本。结构化上限名单、mode 分组、无法归因订单列表的本轮接线保留。

回归测试：直接渲染实际聚合结果，覆盖"仅有无法归因 BUY""仅有孤儿／来源不明意图"，断言无错误的"没有 BUY"结论；用 R4 两冲突提交＋一条 fills 场景断言页面显示归属冲突原因而非"台账中无订单事件"。正常无事件空会话保留原空状态。

下一步

仅修正以上两个展示边界，不改订单归属策略、下单参数、预算预留或业务意图队列；此前已关闭项保持关闭。连接恢复后需补读 ITERATION: 4 尚未取得的三盘、构建和新增回归证据；不应仅因这次 502 重跑已有测试。代码修复后再发布新一轮定向回归、四盘全量、同步及前端构建／渲染的可读输出。

NEXT_EXPECTED_STEP: 补齐本轮证据读取，修复 R6-B／R8 后回报 EXECUTED / ITERATION: 5。本轮不进入 DONE。
```
