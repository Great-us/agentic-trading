# c2c_a7e2 ITERATION 1 复审原文（ChatGPT 6 Pro，2026-09-20，经 Trading 连接器独立读取 execution_output id=8 与工作区）

> 原文逐字存档。结论：STATE: PLAN / REVIEW: CHANGES_REQUESTED / OPEN_ITEMS: R1–R9。
> 修复顺序按其建议：R1–R3 隔离与容错 → R4–R6 状态链；R7 可并行；R8 等后端契约稳定后接线；R9 补证。

```
[C2C]
STATE: PLAN
TASK_ID: c2c_a7e2
ITERATION: 1
WORKSPACE: Trading
BASELINE: 97a65d6
REVIEW: CHANGES_REQUESTED
OPEN_ITEMS: R1–R9

核验结果

已确认 Trading 工作区及 HEAD=97a65d6，按 list → read 读取本轮 execution_output(id=8)，正文未截断。另读取证据目录中的三盘 pytest、同步及构建原始输出，独立检查相关源码和测试 diff、全部新增测试文件、execution_funnel.py 与轨迹 fixture。

项目	已读取的结果
P1	761 passed，执行记录 exit 0
P2	642 passed、1 skipped
P3	927 passed
P4	891 passed
同步	65 个共享文件一致，config/risk.yaml 字节一致
前端	TypeScript 检查及 Vite 构建通过

本端未重跑命令。测试通过可以确认，但不足以关闭以下代码缺口；其中有些测试将错误语义写成了预期。

可保留增量迁移、不补造旧身份、独立 OrderObservation、sizing 诊断字段及 SQLite 持久漏斗的整体设计。双调用签名和 .lower() 本身不作为退回原因；intent_cleared 词表扩展也可保留，但记录时机必须修正。

R1｜高：观测异常仍能进入交易路径，且共享回查削弱了 P0 止损容错

位置：
src/agentic_trading/execution_funnel.py:60–78, 115–147, 210–252
src/agentic_trading/run.py:1484, 1508, 1757–1760, 2010–2027
src/agentic_trading/execution/broker.py:267–293, 325–334
src/agentic_trading/risk/manager.py:347–357, 412–429

目前保护了两个写入目的地和惰性 payload，但没有保护完整观测调用：run ID／event ID 生成、部分语义方法的字段访问、观察结果读取都可能在 try 外抛出。mint_run_id() 失败会直接中断周期；SizingDiagnostics 构造失败也会阻止原本应完成的 sizing。

P0 还有明确的回归反例：回查成功返回有效 status，但可选 filled_qty="bad"，会在 observe_order() 的 float() 转换处抛错；submit_stop_sell() 调用它时已经没有原来的外层捕获。新增的成交展示字段因此能使已提交止损的处理抛出异常。

修改：对 recorder 初始化、身份生成、语义方法及诊断构造建立完整的降级边界，失败时不影响原决定、保存结果或订单调用。可选成交字段单独防御性解析；有效订单状态不能因无效可选字段被丢弃。止损调用保留原有回查失败兜底。身份元数据失败也不能把本可成功的业务意图保存变成 WAIT。

红测试：在完整轨迹中分别注入 run/event 身份生成失败、诊断构造失败、异常观察对象；在止损测试中加入无效成交数量／价格及回查方法抛错。验证交易轨迹、预算预留、意图清除和止损返回语义不变，而不是只验证 to_dict()、SQLite writer 和 JSONL writer 三个现有注入点。

R2｜高：回测绕过原 asof 门禁，写入 live JSONL

位置：run.py:1457–1459, 1484, 1508, 1757–1760；execution_funnel.py:74–78, 91–96, 139–146。

funnel_mode="backtest" 只是标签，并没有禁用输出。新 recorder 默认直接调用 live_events.emit，没有经过原 _emit_live(asof, …)。因此，run_cycle(asof=...) 即使用临时 journal，仍可能写入真实 data/live_events.jsonl；快扫休市出口同样直接调用 flush_run_skip()。

现有 autouse fixture 将路径重定向到临时目录，能防测试污染，却不能证明生产回测路径不会污染 live 文件。

修改：明确分离历史／模拟观测与 live JSONL 输出。asof 非空时不触碰 live 事件、心跳或 progress；需要保留的回测事件只能写调用方明确提供的隔离目的地。检查普通路径与提前退出路径。

红测试：在临时目录设置一个代表 live 文件的哨兵文件，运行带 asof 的正常周期及提前退出周期，断言文件字节不变、live emitter 零调用；临时 journal 中需要保留的 backtest 记录仍可存在。

R3｜高：savepoint 后的无条件 commit 会提交调用方业务事务

位置：journal/logger.py:572–589；execution_funnel.py:128–137。

record_intent_event() 释放 savepoint 后无条件 conn.commit()。若调用者已有未提交业务事务，新增观测事件会顺带提交整个事务；调用者随后 rollback 已无法撤销原业务写入。savepoint 不能隔离其后的顶层 commit。

当前 tests/test_intent_identity.py 的失败测试先完成了业务提交，再让 payload 构造失败，没有覆盖未提交事务。另需纠正台账：基线函数本来就有 commit()；问题不是"本轮首次加 commit"，而是将这种提交语义接入新增观测路径后，仍声称满足事务隔离。

修改：区分函数是否拥有事务；已有调用方事务时只能释放自己的 savepoint，不能提交或回滚外层事务。无外层事务时正常持久化。同步检查插入、释放及提交失败后的连接可用性。

红测试：先开启业务事务并修改一行，写漏斗事件，再 rollback，断言业务改动仍可撤销；用第二连接验证事件没有提前提交业务数据。另测事件插入失败后，外层事务仍可由调用方提交／回滚，无遗留锁。

R4｜高：accepted 订单没有后续周期回查，跨日聚合还会丢失原提交关联

位置：
run.py:1148, 1323–1349, 1926–1939
execution_funnel.py:210–252
dashboard/views.py:904–917, 1008–1051

执行侧只观察本次 flush 的 submitted_orders，下一周期该列表重新创建，而意图已经删除。没有从持久记录加载未终结 order_id 的路径。现有 accepted→filled 测试只是手动调用 broker.observe_order() 两次，不能证明 run_cycle 会接续追踪。

聚合侧 _intent_chain() 又只读取 today_by_intent。昨日提交、今日成交时，昨日的 order_id 和 sizing 快照不会进入今日承接链；没有今日意图事件时，整条链甚至不出现。仅靠 Today 的 fills 输入无法补齐这个关联。

此外，当前回查位于 flush 末尾，但仍在本轮新意图处理和周期末保护止损之前，不是计划要求的"全部影响下单的处理完成之后"。

修改：从持久事件恢复尚未终结的订单，使用同一 observe_order() 入口，在既有周期的交易处理完成后进行有总请求预算的观察；不新增调度、不恢复意图、不释放预算、不重试下单。聚合保留截至截止时间的历史创建／提交／状态关联，今日活动与完整链历史分开处理。创建日期不能限定订单关联的检索范围。

红测试：连续运行三个真实测试周期：第一轮创建、第二轮 accepted 并清除意图、第三轮无新买单但回查到 filled／rejected；再覆盖跨 ET 日期及今日仅有 fills 的场景。断言订单只提交一次、身份不变、后续结果可见、请求预算有界，且观察调用发生在交易调用之后。

R5｜中：提交、部分成交、完成和未知状态的聚合仍不准确

位置：
run.py:1304–1320
dashboard/views.py:636–689, 771–776, 988–1005
dashboard/frontend/src/pages/Today.tsx:260–292

这里有四个需要统一修正的状态规则：

单条 fills 被当成整单完成。qty_positive and last_kind != "order_partial" 就增加 filled_verified。accepted 后只有 1 股部分成交、尚未成功回查 partial 的订单，也会计入完成。tests/test_funnel_aggregation.py:123–164 和截断场景将此写成了通过条件。

后续 unknown 会覆盖已确认完成。_funnel_orders() 更新 last_observation_kind，但汇总仅检查该最新 kind；已有 order_filled 后再记录 unknown，且 fills 不可用时，已证实完成会从完成数消失。

提交失败信息被丢弃。order is None 被强写成 submit_status="rejected"；聚合又不读取 submit_status，仍按 order_submitted 计为"已提交"。没有区分提交尝试、确认受理、明确拒绝及结果不明。

前端把任意非空状态解释为受理。rejected／canceled 等状态也会附上"已受理，未确认成交"。

另外，fills 建索引时没有应用 transaction_time <= now；指定历史截止时间时，会混入截止之后的成交证据。

修改：分离"已提交尝试／受理""存在成交""整单完成""订单终结但未完成"及"最近观察失败"。部分成交数量不等于整单完成；新失败不能抹掉可靠的历史完成证据。保留提交响应和观察失败原因，统一后端与前端口径。fills 使用同一截止时间，按活动 ID 去重。

红测试：accepted＋一条 partial_fill、partial 后 canceled、filled 后 unknown、None 提交结果、明确 rejected、重复 fills、截止后 fills；同步断言汇总和实际渲染文字。修改上述错误预期，而不是继续保留它们。

R6｜中：意图生命周期仍有静默分支、假终态和错误尝试计数

位置：
run.py:1173–1194, 1211–1219
execution_funnel.py:122–127, 152–188
dashboard/views.py:855–858, 904–917, 953–975

需要一并补齐以下链路：

周期订单上限。flush 的 break 仍没有结构化原因，尚未处理的意图只显示旧状态。记录运行级"因订单额度停止处理"及可确定的受影响范围，不改变原 break，不伪造逐项检查。

清除成功尚未发生就发终态。intent_cleared 在 clear_trade_intent() 之前写入，之后 _journal_safe() 的失败结果被忽略；数据库清除失败时，页面仍可显示已清除。gap／ttl 的"决定丢弃"也应与"删除成功"区分。不能为修观测而改变重试或删除策略。

替换后旧版本仍显示等待。新版本携带 previous_version，但旧链没有 superseded 终态；tests/test_funnel_aggregation.py 甚至断言被替换的 v1 "still waiting"。应保留旧 sizing 失败历史，同时明确它已被哪个版本替代。

迁移后的旧活动意图被漏掉。旧 intent 的 version 合法地为 NULL；新代码产生的事件却有 mode="paper"。它既不能成链，也不符合 legacy 桶要求的 mode is None，因此从漏斗降级统计消失。应列为"本轮已观察、旧身份缺失"，不能补造版本。

尝试数不是创建／观察运行数。创建即自动带 attempt，聚合按所有相关 run 计数，十次 flush 被显示为十一次尝试。按真实 flush 尝试计数；创建和状态观察另列。open_missing 是跳过一个检查，不是整次执行等待，也不能增加"等待次数"。

红测试：对以上五种情形分别走实际 recorder／flush→聚合链，断言原因、旧版本终态、降级记录和计数一致；不要仅手工插入一组符合实现的事件。

R7｜中：SQLite 与 JSONL 没有共用安全清洗，嵌套字段仍能泄漏

位置：journal/logger.py:206–231；execution_funnel.py:128–145；live_events.py:48–65。

SQLite 只白名单过滤顶层 payload；嵌套字典递归时保留所有键。JSONL 则直接收到原 payload，连 SQLite 的顶层白名单及非有限数处理也没有复用。

可直接用测试值验证：payload={"sizing":{"api_key":"SENTINEL"}, "prompt":"SENTINEL"}。嵌套敏感键会留在 SQLite，JSONL 的嵌套 payload 也不受现有顶层 _SECRET_KEYS 保护。本轮没有发现实际凭据泄漏，但清洗契约未实现。

修改：先生成一份递归白名单、非有限数规范化后的事件，再分别投递两个目的地；不要将原 payload 发给 JSONL。一个目的地失败不影响另一个，清洗失败不影响交易。

红测试：顶层、嵌套字典、数组中的敏感键及 NaN／Infinity，同时检查 SQLite 和 JSONL 内容一致、安全且可解析。当前名字含 nested 的测试没有把敏感键放到嵌套位置，需要补齐。

R8｜中：结构化 sizing 数据已产生，但 Today 未展示要求的余量和时间

位置：
risk/manager.py:25–66
dashboard/views.py:692–705
dashboard/frontend/src/api.ts:243–251
dashboard/frontend/src/pages/Today.tsx:295–321

SizingLine 只展示可用预算、最低仓位和限制项，没有展示现金、敞口、行业／主题、组合止损风险余量。后端 _funnel_sizing_snapshot() 也没有保留事件时间或 attempt 身份，页面无法说明"这组金额是哪次尝试的"。

修改：直接展示现有诊断中的各项余量、单位和未评估状态，并带快照时间／attempt；不重新计算、不解析 reason、不增加交易查询。行业字段明确为"行业／主题有效余量"。被替换／终结链上的旧 sizing，应标明历史快照，不能继续无条件声称"意图保留"。

红测试：使用不同的现金、敞口、行业和止损风险金额，渲染断言每项标签、金额和时间均存在；覆盖零与 NULL 的区别。至少一个测试使用真实 size_position() 返回值贯通事件、聚合、渲染，避免当前手工 fixture 中"敞口余量为 0、最终预算却为 $65.62"的不一致数据。

R9｜验收证据：尚未建立来自原提交的完整轨迹对照

位置：
PROGRESS.md:355–356
tests/test_trade_trace_baseline.py:114–161, 235–258
tests/test_execution_funnel.py:246–279
tests/fixtures/trade_trace_baseline.json:2–8

台账明确记录：六笔轨迹生成时，树上已经包含 risk 修改。13 个 sizing 样例及静态 diff 可以支持局部不变性，但不能替代同一场景在原始 97a65d6 上的完整运行证据。这不是断言当前六笔数值错误，而是验收来源尚未满足要求。

此外，轨迹使用的 TraceBroker 没有 observe_order()；所谓健康观测轨迹实际走的是 observe_unavailable，没有覆盖正常回查、拒绝回查和异常观察结果对完整周期的影响。

补证方式：从 git 将 97a65d6 导出到临时目录，使用同一场景运行器和隔离数据路径生成轨迹，与现有 fixture 及修复后轨迹比较；不要 reset／checkout 当前或兄弟盘，不为获得通过而改写 expected values。发布实际模块来源、命令、输出与比较结果。

扩展轨迹测试覆盖真实观察 fake 的 accepted／filled／rejected／异常结果，以及 R1–R3 的故障场景；统一冻结事件与周期时钟，再加入 recorder→聚合→Today 的完整链测试。现有四个通过用例保留。

下一轮执行与验收

先处理 R1–R3 的隔离与容错，再完成 R4–R6 的状态链；R7 可由存储／事件负责人并行处理，R8 等后端契约稳定后接线。run.py 仍由领导单人修改，其他员工只交接口和回归用例。

下一轮通过本任务的可读 execution_output 发布原提交轨迹对照、定向红绿测试、四盘全量、同步及前端构建／渲染输出。保持冻结区、分析师和订单参数不变；不通过真实下单验证，不提交未验收代码。其他未跟踪 research 文件保留，不混入修复范围。

NEXT_EXPECTED_STEP: 修复并补证后回报 EXECUTED / ITERATION: 2。本轮不进入 DONE。
```
