# c2c_a7e2 — P1-A 执行漏斗 PLAN（ChatGPT 6 Pro，2026-09-18，基线 97a65d6）

> 通过 Codex with ChatGPT 桥接，由 6 Pro 在核实 HEAD 与调用链后给出的原文（仅去掉页面噪音）。
> 领导钉死的三份契约、所有权与顺序在 `PROGRESS.md` §14；两者冲突以 §14 为准并报领导。

```
[C2C]
STATE: PLAN
TASK_ID: c2c_a7e2
ITERATION: 0
WORKSPACE: Trading
BASELINE: 97a65d6
EXECUTION_READY: YES
SCOPE: P1-A-1 ～ P1-A-4；仅执行归因与展示，不改变交易行为
```

## 一、基线与共同设计

已通过 Trading 连接器核实工作区、HEAD、git 状态与 diff，并读取 PROGRESS.md §5-C 及指定调用链。源码与 HEAD 一致；工作树并非完全干净，仅 PROGRESS.md 追加了一条提交及新任务交接记录，保留该改动。§5-C 的旧源码行号已经过期，以下使用当前行号。

本次实现应围绕三个不同对象建立关联，不能用 symbol 代替它们：

| 对象 | 身份及计数规则 |
|---|---|
| BUY 决策 | 独立 decision_key，关联实际运行的 run_id、mode、symbol。取最终 decide() 返回 BUY 的时刻，不取后来可能改成 WAIT 的 journal action。 |
| 意图版本 | 一次创建／替换一个不可变 intent_id；跨周期等待、sizing 否决和提交重试不换身份。 |
| 尝试与订单 | attempt_id 区分处理尝试；order_id 区分券商订单。同一订单的多次状态观察、多个部分成交不是多笔下单。 |

漏斗主线：BUY 决策 → 意图成功创建／替换 → flush 检查 → sizing 通过 → 提交 → 券商确认成交。每条未前进的链必须有结构化原因，或者明确标为证据缺失／状态未核验；没有事件不能直接解释成"未成交"或"被拒绝"。

保留两个关键顺序：当前代码先 flush 旧意图，再保存本轮新意图；提交返回非空后即预留资金、更新计数并清除意图。这些执行语义不属于本次修改范围。尤其不能因新回查发现 rejected，就释放预算、恢复意图或追加重试，否则会改变后续买单参数。证据：run.py:1257–1283, 1848–1858, 1919–1947。

建议新增共享观测模块 src/agentic_trading/execution_funnel.py，集中事件构造、身份关联及容错。它不向交易逻辑返回放行／否决决定，不导入 dashboard。

## 二、A-1｜意图身份、版本与持久关联

**原因与文件修改**

当前 trade_intents 以 symbol 为主键，保存会覆盖旧行，清除会删除该行；intent_events 只有时间、symbol、kind 和文本。无法区分"同一意图被拦十次"和"十个不同意图"。证据：journal/logger.py:129–156, 357–413。

`src/agentic_trading/journal/logger.py:129–157, 198–226, 357–413` —— 采用以下最小明确契约：

- trade_intents.version TEXT NULL：本轮采用不透明 UUID 版本标识，不要求递增整数；新事件的 intent_id 使用该版本值，跨盘聚合另带 book 标识。新决策创建／替换时生成，读取和 flush 时不生成，删除后重新创建不得复用。
- TradeIntent 末尾增加可空版本字段，保持现有构造方式兼容。旧活动意图的版本保持 NULL，直到真实的新决策替换它；不在迁移或首次读取时替旧意图编造身份。
- intent_events.intent_id 可空。为完整漏斗增加可空关联字段：事件唯一键、run/mode、decision、attempt、order，以及结构化 payload。client_order_id、替换前版本和 sizing 快照可放入 payload；需要检索的关联键使用列。
- 保留旧五类 gap/chase_signal/chase_open/sizing/ttl 的统计语义。新阶段使用不同 kind；已有 flush 事件应扩展关联字段，不要在 SQLite 重复写一份同义事件。
- 迁移必须是增量、可重复执行；依赖新列的索引在列迁移完成后创建，不能提前放进 SCHEMA 导致旧库启动失败。历史 intent_events 的新增字段保持 NULL。
- 保存意图时，实际保存的版本与业务内容一起提交；可返回保存回执，包含本次版本、创建／替换类型和已知前版本。旧调用者可忽略回执。不要在保存后重新查询"当前 symbol 的版本"来猜这次写入的身份。

`run.py:1155–1253, 1919–1947` —— 传递加载出的版本，为现有事件补关联；意图创建事件只在业务保存成功后发出。当前 _journal_safe() 只返回 bool，不能把它当保存回执；采用局部适配，不全局改写通用 helper。原保存失败仍走现有 WAIT 分支；附加事件或身份元信息处理失败，不得把已经成功的业务保存改判失败。证据：run.py:451–462, 1926–1933。

**必须先红的测试**

新增 tests/test_intent_identity.py，验证旧库迁移两次仍幂等、旧行不补造 ID、旧业务字段不变；新意图经过十次 deferred flush，只有一个 intent_id、十次真实尝试；替换得到新版本并保留前版本关联；删除重建不复用身份；同一事件重复投递不双计。

另验证事件写失败不回滚意图保存，事件写成功也不能使失败的意图保存被报告为成功。新观测写入使用隔离的事务／savepoint 边界，不提交或回滚调用方尚未完成的业务事务，不留下锁阻塞下一步。

**主要风险**：以 symbol 或可重置整数作为永久身份；迁移补造历史；附加日志与核心保存共用失败结果；事件表新 kind 被旧展示错误解释为"丢弃"。

## 三、A-2｜补齐 flush 前后事件，建立可核算的漏斗

**原因与文件修改**

最终模型决定在 run.py:1671–1680 产生；随后 entry eligibility、pending buy、已持仓、LLM fail-closed、未知 regime、订单数上限、dry-run 和保存失败等分支会阻止意图创建。当前不能仅靠最后的 action 还原这一过程。flush 内还有未到 not_before、缺报价等静默等待分支。证据：run.py:1132–1172, 1856–1947。

`run.py:1327–1330, 1432–1438, 1671–1680, 1848–1947` —— 在最终 decide() 返回 BUY 后记录 decision_buy，作为分母；快扫前置的 quant-only 候选不是该分母。各后续门禁原地记录结构化结果，不移动判断、不再次调用模型，也不修改已有 decision 内容。本轮完成时，每个已记录 BUY 决策应能归入：创建／替换成功、明确未创建原因，或流程中断／证据缺失。保存后的成功事件必须带保存回执中的 intent_id。新 run_id 使用独立运行身份，不使用当前预估的 MAX(cycles.id)+1 作为可靠主键。保留原 cycle 字段供旧消费者使用；不能为了补真实 cycle ID 而改变 journal 的提交时机。

`run.py:1099–1283` —— 补齐窗口外、挂单不可读、未到执行时间、已持仓／已有 pending buy、周期订单上限、无报价、gap、chase、sizing、提交结果等状态。全局跳过记运行级原因；未实际遍历的意图不要伪造一次"检查通过"。max_new_orders_per_cycle 当前使用 break，不能为了逐项记日志改成继续执行检查。缺失今日开盘价时记录该检查未执行，不能写"通过"。

`execution_funnel.py（新增）；live_events.py:45–104` —— 同一规范化事件送往两个独立容错的目的地：SQLite 持久记录与 live_events 即时展示。新增 JSONL 阶段可使用 execution_funnel，不要破坏现有 cockpit 阶段。JSONL 会轮转，默认只回放 120 行，不能作为全日漏斗的唯一账本。Today 的完整统计从 SQLite 读取；JSONL 用于即时过程。证据：live_events.py:24–26, 69–104。异常保护应覆盖构造字段、序列化、写库、写 JSONL，不能只包最后的文件写入。当前 _journal_safe() 仅捕获 SQLite 异常，不能直接承担全部新观测容错。结构化 payload 使用白名单，并处理嵌套字段、非有限数值；不收录凭据、原始 prompt 或原始券商响应。

`progress.py:94–136, 240–252` —— 保持原有职责。它是覆盖式交班卡，不是历史漏斗；不必扩展为第二套账本。观测故障可通过现有 unresolved 表达，不能新增交易锁或改变 next_job。也不要把 headroom 随意塞进 risk 字典——当前白名单会丢弃它。

**必须先红的测试**

新增 tests/test_execution_funnel.py：五个最终 BUY，其中三个保存成功，统计明确为 3/5；其余两个有对应原因。覆盖上述创建前门禁、flush 等待／丢弃／失败分支、旧意图跨周期重试、本轮新意图不会提前 flush。分别让 payload 构造、SQLite 事件写入、JSONL emit 抛异常，验证交易调用轨迹及意图保存／清除结果与基线一致；一个观测目的地失败不阻止另一个。模拟周期中断时，漏斗显示未完成，不填造终态。保留 tests/test_live_events.py 的容错测试并补嵌套字段与重放兼容。

**主要风险**：重复统计 quant 候选与最终 BUY；把十次重试计成十个决策；新增 emit 消耗 OrderIdMinter 序号；观测代码改变原来的循环顺序或异常传播。

## 四、A-3｜提交与成交分离，复用唯一订单回查入口

**原因与文件修改**

submit_notional_buy() 返回的是提交响应；其中 qty 来自 order.qty，不能当累计已成交数量。flush 却把任何非空结果写成 filled。另外，P0-B-2 的回查目前仍内嵌在止损方法中，需要先提取再复用。证据：execution/broker.py:199–220, 263–304；run.py:1257–1283。

`execution/broker.py:31–37, 112–121, 199–220, 263–305` —— 把现有 get_order_by_id 回查及状态规范化抽为单一共享入口，返回独立的只读观察结果，例如：原始状态、观察时间、实际 filled_qty、成交均价／时间（存在时）、失败原因。止损路径改用该入口，但保留已验收的 fallback、状态判断和返回语义。买单的观察结果与原 OrderResult 分离：不因为观察到拒绝而把原非空提交结果改成 None。不能把 stop_is_resting() 当买单成交判断；accepted 是止损覆盖语义之一，不等于买单 filled。

`run.py:1257–1283, 1982–2009；execution_funnel.py` —— 先记录提交事实、原响应状态、实际 order_id 和原 client_order_id。原来 mint 一次的地方仍只 mint 一次，保存该值供日志关联；身份生成不得借用该计数器。将错误的"filled @ ~live_price"改成提交／预留语义。只有券商明确的成交证据才产生成交阶段；部分成交单独表示，报价不是成交价。原接口返回 None 时，没有充分证据便标记"提交失败／结果不明"，不能一概断言券商明确拒绝。买单附加回查放在本周期会影响下单的处理完成之后，不插入多个买单之间，不等待成交、不新增 sleep／下单重试。每个订单每轮最多一次，限制总请求预算；失败或超预算显示未核验。意图已被删除的 accepted 订单仍要能追踪：从持久事件取得未终结 order_id，在后续既有周期的观测阶段继续调用同一入口，不新增调度，也不恢复意图。Today 已有的 fills 可按 order_id 补成交证据，不再另起 HTTP 查询实现。

**必须先红的测试**

新增 tests/test_order_observation.py：accepted 未成交无 filled；accepted 后 filled、部分成交后完成、rejected／canceled／expired、未知状态、回查异常／超预算、缺 order_id 均准确降级；缺少新观察接口的旧 fake／DryRunBroker 不崩溃，也不伪报 paper 成交。覆盖跨周期 accepted→filled、同一状态重复读取、重复活动 ID、多个部分成交，确认只计一张订单；一次成交活动不自动证明整张订单完成。在完整 flush 测试中，原提交响应相同、新回查分别返回 accepted、filled、rejected 或抛异常，后续 submit_notional_buy 参数、资金预留、意图清除行为必须相同。现有 tests/test_p0_hardening.py:168–258、tests/test_stop_reconciliation.py 的止损回归全部保留。

**主要风险**：为修统计而改变下单控制；清除 intent 后失去关联；状态查询失败覆盖已经证实的成交；使用 OrderResult.qty 或 live quote 伪造成交数据。

## 五、A-4｜结构化 sizing 余量与 Today 漏斗

**原因与文件修改**

当前 SizeResult 只有四个字段，拒绝时返回金额为 0，实际可用预算只剩在文本中。Today 又只取最近 20 条 intent events，前端再截取 8 条，不能作为完整漏斗。证据：risk/manager.py:25–29, 311–358；dashboard/views.py:556–564；Today.tsx:132–159。

`risk/manager.py:25–29, 279–358` —— 在 SizeResult 末尾增加有默认值的可选诊断对象，保持旧构造调用兼容。从本次 sizing 的实际中间变量带出：cash_available、exposure_room、目标／单仓上限、剩余组合止损风险及其可买金额、有效 sector room、相关性折扣前后金额、最终可用预算、最小仓位金额、结构化限制项／拒绝码。保留现有运算顺序、浮点计算、round 时点、approved、notional 和原 reason。拒绝时原 notional=0 不动，另保留归零前的 available_notional。早退导致未计算的字段用 NULL／未评估，不补假零，也不为补诊断执行原本不会运行的除法。

`run.py:1057–1096, 1226–1253, 1954–1981` —— 只传递 sizing 返回的诊断快照，不在 run／前端复制公式。特别注意 _entry_sizing_inputs() 已把 theme room 与 sector room 取 min；没有分别保留原值时，应显示"行业／主题有效余量"，不能错标为纯行业限制。

`dashboard/views.py:502–564, 567–581, 611–660` —— 新增只读漏斗聚合，按 book、paper mode、统一 ET 会话及截止时间筛选。将"本会话产生的 BUY 决策"作为 cohort；往日意图今日继续 flush 单独显示为承接意图，不能混入本会话分母。按 decision／intent／order 去重，重试次数另列。每条链显示最新已知阶段、原因、尝试次数、意图替换关系、订单状态和 sizing 快照时间。旧行无身份、缺事件、数据源不可用时显式降级；不按 symbol＋相近时间强行配对。替换前版本的失败不能覆盖新版本，也不能把新版本成交计给两个决策。

`dashboard/api.py:192–217；broker_read.py:170–260` —— 复用当前 fills 输入及其 id/order_id，把读取失败、截断和观察截止信息传给聚合。已有 reader 不需新增成交查询路径。fills 不完整时，匹配到的成交仍是正面证据；没有匹配不能证明零成交。GET 展示路径不得迁移或写库；旧 schema 直接降级。

`frontend/src/api.ts:163–219；pages/Today.tsx:92–159` —— 增加可空的身份、订单观察、sizing 和 funnel 类型。用漏斗摘要及逐链明细替代笼统的"想买但没买成"；保留现有交班、止损与实际 fills 展示。预算示例应表达为：可用预算 $65.62，低于最低仓位 $390.23，本次 sizing 未通过、意图保留，并展示结构化限制项，而不是统一写"现金不足"或"vetoed"。该数值必须来自对应尝试的 sizing 快照，不是页面打开时重新计算。旧 intent_events 展示不能继续对所有 deferred=False 一律写"丢弃"：要么保留旧五类过滤，要么改成按明确阶段展示，否则新提交／成交事件会被误标。

**必须先红的测试**

扩展 tests/test_risk.py:40–111, 330–365：现金、敞口、组合止损风险、行业／主题、仓位数量、相关性折扣、最低仓位边界、相同预算值的限制项及非法输入。除新增诊断外，原返回结果与基线精确一致；特别覆盖先 haircut、再 sector 限制、再 round、再最低仓位比较的顺序。扩展 tests/test_dashboard.py:831–893 并新增聚合测试：3/5 漏斗、十次重试不放大分母、跨日承接、替换版本、accepted 无成交、部分成交、混合模式、ET／UTC 日期边界、超过 20／120 条事件、JSONL 轮转、旧 schema、fills 失败／截断。读取前后临时数据库内容不变。前端增加最小离线渲染测试，覆盖预算不足、部分成交、旧数据未知、加载失败及非 P1 书的兼容显示；运行 npm run build。目前 build 已包含 TypeScript 检查，但项目尚无测试脚本，不能把构建成功当作展示断言通过。证据：frontend/package.json:6–10。

## 六、三名员工的执行顺序

| 负责人 | 独占范围 | 顺序 |
|---|---|---|
| 员工甲 | 唯一可编辑 run.py 的员工；execution_funnel.py、live_events.py、执行漏斗及完整周期测试 | 先建立基线轨迹／红测试；乙的数据接口稳定后接 A-1、A-2，再接 A-3 和 sizing 诊断。 |
| 员工乙 | journal/logger.py、execution/broker.py；身份迁移、订单观察及止损适配测试 | 先 A-1 存储，再提取 A-3 唯一回查入口；不编辑甲的完整周期测试文件。 |
| 员工丙 | risk/manager.py、dashboard 后端／前端、risk／dashboard／渲染测试 | 先用固定契约做 sizing 和聚合红测试，可与乙并行；甲接线完成后做 A-4 端到端验收。 |

领导先确认三份共享契约：事件及身份、OrderObservation、SizingDiagnostics，再允许实现。其他员工发现 run.py 问题只交位置和反例，由甲修改；不以"不同函数"作为并发编辑例外。PROGRESS.md 汇总和四盘复制同步由领导集中完成，避免互相覆盖。

## 七、EXECUTED 验收标准

首先建立不可被新代码重写的基线。在修改前，以 97a65d6、固定时钟、固定行情／LLM／券商响应、临时 journal 生成调用轨迹。记录每次 submit_notional_buy 的 symbol、notional、atr14、client_order_id、顺序和次数；金额可附 float.hex()，不能用误差容忍掩盖变化。现有 fake 只记录 symbol/notional，不足以证明本次不变量。证据：tests/test_p0_hardening.py:290–295, 475–477。

验收时比较基线、正常观测、各观测环节故障三组轨迹，并核对业务意图队列、预算预留、SELL／止损调用未受影响。新增身份字段不进入任何 sizing 或订单参数；真实核心保存失败与附加观测失败分开处理。

同时要求：上述各包红测试的失败输出与修复后输出；正常证据下每个 BUY 链均可分类，缺证时诚实显示未知，不生成虚假成交或成功保存。四盘全量测试、同步检查、前端构建与渲染测试。共享新模块也须复制到 P2/P3/P4，不能通过修改同步排除清单获得 exit 0。冻结区、.env、分析师、订单参数及 P0 止损安全链保持不变；所有验证离线、临时库／临时事件文件，新订单回查也必须 mock。现有 tests/conftest.py:22–50 的隔离继续有效。相对 97a65d6 的变更清单、每包负责人与接口交接记录；原始证据通过本任务的可读 execution_output 发布。本端将在 EXECUTED 后 list → read，再独立审查 git diff 和新文件，不仅接受测试数字摘要。

NEXT_EXPECTED_STEP: 按上述文件所有权建立基线轨迹、共享契约与红测试，再实施 A-1～A-4；完成集成后回报 EXECUTED / ITERATION: 1。本计划不授权调参、实际下单或提交未审阅代码。
