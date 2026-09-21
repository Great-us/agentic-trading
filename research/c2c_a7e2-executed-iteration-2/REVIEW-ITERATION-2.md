# c2c_a7e2 ITERATION 2 复审原文（ChatGPT 6 Pro，2026-09-20）

> 原文逐字存档（从对话页转录）。结论：STATE: PLAN / REVIEW: CHANGES_REQUESTED。
> CLOSED: R2、R3、R7、R9。OPEN: R1、R4、R5、R6、R8。台账：PROGRESS.md §18。

```
[C2C]
STATE: PLAN
TASK_ID: c2c_a7e2
ITERATION: 2
WORKSPACE: Trading
BASELINE: 97a65d6
REVIEW: CHANGES_REQUESTED
CLOSED_THIS_ITERATION: R2、R3、R7、R9（原提交轨迹补证）
OPEN_ITEMS: R1、R4、R5、R6、R8

核验结果

已确认工作区和 HEAD，按 list → read 读取本轮 execution_output(id=9)，正文未截断；另读取四盘证据、同步与构建输出、原提交导出轨迹对照，以及与退回项相关的当前 diff、调用链和新增回归测试。

执行记录确认：P1 811 / P2 670（1 skipped）/ P3 974 / P4 938 passed；65 个共享文件一致；TypeScript 检查及 Vite 构建通过。原提交导出后的轨迹测试记录为 1 passed，这项基线来源问题可以关闭。本端未重跑命令，以下反例来自当前源码与测试的独立检查。

本轮已经落实回测 JSONL 门禁、外层事务所有权、双目的地共用清洗，以及交易处理完成后的统一观察位置。剩余问题主要是不同层之间没有完全接通，不能据此认定 R1–R9 已全部关闭。

R1｜高：意图身份生成和 sizing 早退仍能改变交易行为

位置：journal/logger.py:521–552；run.py:2021–2035；risk/manager.py:69–77, 352–360；测试缺口 test_execution_funnel.py:302–328, 362–367。

- 意图保存：save_trade_intent() 在业务写入前直接调用 uuid.uuid4()，失败会进入 run.py 的 save_failed → WAIT，原本可保存的意图消失。保存回执又在 conn.commit() 之后构造；回执构造失败时，会出现"意图实际已提交，但决定被改成 WAIT、事件宣称保存失败"的相反错误。当前"坏回执"测试只调用 intent_saved(None/object())，没有覆盖保存函数自身构造回执失败；身份故障测试也特意保留了 logger 的 UUID 生成。
- sizing 早退：主计算路径的 SizingDiagnostics 构造已包 try，但 invalid price、max_open_positions、invalid stop distance 三个早退仍直接调用 _empty_sizing_diagnostics()。诊断构造失败时，原来的正常否决会变成异常，可能中断剩余周期和周期末保护处理。

修改：将实际业务保存结果与身份／回执是否可用分开。身份元数据不可用时明确降级，不阻止原业务保存，也不为新决定复用旧版本身份；业务提交成功后，回执故障不能改判为保存失败。所有 sizing 出口使用同样的可选诊断容错，保持原 approved/notional/reason。
红测试：分别注入 logger 的 UUID 失败、保存后回执构造失败、三个 sizing 早退的诊断构造失败。断言意图业务字段和队列结果、决定 action、完整交易轨迹及周期末处理与基线一致；真正的 SQLite 保存失败仍走原 WAIT 分支。

R4｜高：跨周期成交虽然已落库，却没有关联回原意图链

位置：execution_funnel.py:190–246；dashboard/views.py:993–1019, 1082–1116；测试缺口 test_execution_funnel.py:438–471。

恢复函数只返回 (symbol, order_id)，随后 candidates.append((symbol, order_id, None))——第三周期产生的 order_filled/order_observed 没有 intent_id。聚合的历史索引只收录 intent_id 非空的事件，_intent_chain() 也只读这个索引；order_to_intent 只用于匹配 fills。结果：fills 不可用时原链仍停留在 accepted；跨日无其他意图事件时承接链可能不出现。现有三周期测试只断言新增行的 order_id，没有继续调用聚合；后端跨日测试手工给观察事件填了 intent_id，绕过了真实恢复路径。

修改：恢复订单时同时保留原提交事件中可靠的 intent_id；聚合也应支持将已有的无 intent_id 观察，通过同一本书、同 mode、完全相同 order_id 关联到唯一已知意图。冲突或原提交无身份时显式列为无法归因，不能按 symbol 猜测。
红测试：扩展现有三周期用例，直接把其真实 journal 送入 funnel_summary()，不手工补列；同日、跨 ET 日期、fills=None 三种。断言原订单只提交一次、观察仍属于原版本、链显示确认完成；跨日结果进 carryover 不放大当天分母。

R5｜中：None 仍被误报明确拒绝；部分成交后的取消终态仍会丢失

1) 提交失败归因：submit_notional_buy() 对所有提交异常返回 None（包括请求异常）。run.py 仍把 None 写成 submit_status="rejected"，聚合解释成明确拒绝。submit_unknown 类型没解决问题——真实 None 路径不产生这个状态。test_funnel_aggregation.py:789–822 仍把"None 就是明确拒绝"写成预期且手工写事件。
修改：没有明确拒绝证据的 None 记"提交结果不明"；保留实际 client_order_id 及可安全记录的失败原因。只有确证拒绝才进拒绝分类。不改变返回值、意图保留、预算、重试行为。
2) 取消终态：观察 status="canceled"、filled_qty=2 时 recorder 因数量非零生成 order_partial；恢复器只从 order_observed 识别 DEAD → 已取消订单被反复回查；聚合先进 order_partial 分支跳过 DEAD 分支，terminal_status 丢失。当前 canceled 测试手写的是 order_observed(canceled)。
修改：成交程度和订单终结状态作为独立事实保留；终态识别不依赖事件恰好叫 order_observed。部分成交后取消应同时显示部分数量与取消状态，后续周期不再轮询。
红测试：真实适配器模拟提交异常→unknown 非拒绝；真实 FunnelRecorder.observe() 写"部分成交→取消"→恢复+聚合+渲染，终态可见、无完成误报、下轮不重查；>8 张已终结候选不挤占未完成订单预算。

R6｜中：尝试计数、旧身份事件及失败原因仍有漏记

1) attempts 仍含四类订单观察事件（_FUNNEL_ATTEMPT_KINDS:640–647）——1 次提交+10 轮回查显示 11 次尝试。应以实际 flush 工作计数；修正链测试预期 attempts=1、observed_runs=10。
2) 旧身份降级只覆盖旧五类（views.py:624, 1022–1040）：新 flush_wait/intent_cleared/order_* 事件来自 version=NULL 意图时不进 identityless_observed_today 也无法成链。应覆盖所有需要意图身份的事件，按 mode 保持可区分；历史 mode=NULL 作为未分模式参考，不能把已知 dry-run 事件归成 paper 的本轮观察。
3) 清除失败无归因事件（页面只剩旧排队原因）——可记 clear_failed 等非终态事件，不改变删除行为。上限事件的剩余名单只在 detail，聚合取 payload.reason 后不再输出名单——把已知范围作为结构化字段传到底端，不能要求前端解析文本。
红测试：参数化覆盖旧身份的等待/清除/提交/观察；清除失败既无假终态又有明确失败原因；订单上限时 recorder→聚合结果保留受影响范围。

R8｜中：余量与时间已补齐，但 sizing 历史标签没有读取意图终态

SizingLine 只接收 supersededBy 和订单 phase，没有意图 classification。意图先因预算不足保留快照、后 TTL 删除（classification="discarded"、phase=None）时，前端仍在旧快照下写"意图保留待后续尝试"；intent_cleared 同理。另把所有 partial 放进 TERMINAL_CHAIN_PHASES，会把仍在等待余单成交的订单写成"链已终结"。
修改：分开使用意图生命周期与订单终态。discarded/cleared/superseded 的 sizing 是历史快照；已提交未全部成交的 sizing 也只是提交时快照，不应称订单已终结。
红测试：实际事件聚合渲染三条链（sizing 否决→TTL 删除 / sizing 否决→已持仓清除 / 部分成交未终结）：前两条不得出现"意图保留"，后一条不得出现"链已终结"。

下一轮执行与验收

先完成 R1 的剩余隔离，再由执行与后端负责人共同解决 R4/R5 的身份和状态契约，随后收尾 R6/R8。run.py 继续由领导单人修改；已关闭项不重开。下一轮重点补真实 writer→journal→聚合→渲染的穿透测试，统一冻结周期与事件时钟。现有轨迹 fixture 不重写；新增故障/恢复/终态场景继续断言订单参数、调用顺序和业务队列不变。变异补证可保留，但不替代本轮新增反例的回归断言。

NEXT_EXPECTED_STEP: 修复上述五项后回报 EXECUTED / ITERATION: 3。本轮尚不进入 DONE，不批准以验收通过为由提交。
```
