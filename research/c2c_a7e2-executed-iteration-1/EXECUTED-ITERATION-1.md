[C2C]
STATE: EXECUTED TASK_ID: c2c_a7e2 ITERATION: 1
EXECUTOR: ZCode 领导（GLM-5.3，亲任甲：基线轨迹 + run.py 接线）+ 四个 ZCode 子 agent（乙·存储 / 乙·订单 / 丙·后端 / 丙·前端）。原 Claude 员工会话因周额度中断后由用户指定 ZCode 接管，全部过程与验收记录在案。
BASELINE: mechanics-2026-08-30 @ 97a65d6
DIFF_TO_REVIEW: git diff 97a65d6 -- src tests frontend（工作树未 commit）
新增未跟踪文件（连接器可直接读）：src/agentic_trading/execution_funnel.py；tests/test_intent_identity.py、test_order_observation.py、test_execution_funnel.py、test_funnel_aggregation.py、test_frontend_render.py、test_trade_trace_baseline.py、tests/fixtures/trade_trace_baseline.json
LEDGER: PROGRESS.md §14（契约）+ §6「2026-09-20（ZCode 领导，接任）」条目 1–9（逐包执行记录、红绿证据、验收结论、全部自报偏离）
EVIDENCE: research/c2c_a7e2-executed-iteration-1/ —— pytest-Trading.txt（761 passed）/ pytest-Trading-P2.txt（642 passed, 1 skipped）/ pytest-Trading-P3.txt（927 passed）/ pytest-Trading-P4.txt（891 passed）/ check_p2_sync.txt（65 shared files identical, exit 0）/ npm-build.txt（含 tsc 零错误）/ git-head.txt / git-diff-stat.txt

完成的工作包（每包红测试先行、领导逐 diff 复验）：

- A-1 意图身份与持久关联（乙·存储）：trade_intents.version（UUID4，save 生成 / 读与 flush 不生成 / 删除重建不复用 / 旧行保持 NULL）；IntentSaveReceipt；intent_events 8 个可空关联列（增量幂等迁移，先列后唯一索引，INSERT OR IGNORE 按 event_id 去重）；record_intent_event 白名单 payload。test_intent_identity.py 9 例。
- A-3 唯一回查入口（乙·订单）：OrderObservation（frozen，is_terminal：status=None 不算终态）；Broker.observe_order 协议（DryRun→None 不伪报）；AlpacaBroker 用 get_order_by_id 实现，异常→status=None+error；submit_stop_sell 改走唯一入口，P0-B-2 三分类与 fallback 逐字保留；submit_notional_buy 零改动。test_order_observation.py 24 例。
- A-2 漏斗事件与接线（领导亲任甲）：新共享模块 execution_funnel.py（FunnelRecorder：SQLite + live_events JSONL 双目的地、构造/序列化/写库/写 JSONL 四层独立容错、payload 惰性构造、身份仅 uuid4 不碰 OrderIdMinter、observe 每订单每周期一次限额）；run.py：decision_buy 只记最终 decide()（快扫 quant-only 不入分母）、八门禁 intent_not_created、save 直调取回执后 intent_created/intent_replaced 带 decision_key、flush_skipped（运行级）/flush_wait（not_before/no_quote/open_missing）/intent_cleared（已持仓清除，词表扩展）、旧五类带身份列续写、order_submitted（提交事实：原响应状态+order_id+client_order_id+sizing 快照）、flush 后统一观察（order_filled/partial/observed/unknown 映射，缺 observe_order 降级 unknown 不伪报）、"filled @ ~价"改为 submitted/预留/成交未核验。test_execution_funnel.py 15 例。
- 基线轨迹（领导）：tests/fixtures/trade_trace_baseline.json 六条交易调用逐条人工核对（float.hex + client_order_id）；记录时工作树含 SizingDiagnostics 改动，其路径被丙 13 例逐位基线覆盖故等价 97a65d6。
- 丙第一步 SizingDiagnostics（原 Claude 丙完成、ZCode 领导验收）：SizeResult.diagnostics 全字段，算术/round 时点/reason 逐位不变，拒绝时 available_notional 保留归零前值。
- A-4 后端聚合（丙·后端）：views.py funnel_summary() 只读聚合（本会话 decision_buy 分母、ET 会话日、carryover 单列、order 去重、fills 只作正面证据、sizing 快照只转述、旧 schema/旧行/fills 故障显式降级）+ api.py GET /api/books/{id}/funnel。test_funnel_aggregation.py 15 例 + test_dashboard.py +1（含读取前后 itdump 逐字节一致的只读性断言）。
- A-4 前端（丙·前端）：api.ts 全可空类型；Today.tsx 执行漏斗面板（KPI/逐链/承接/降级），旧事件按阶段标注不再一律"丢弃"；test_frontend_render.py 6 例（esbuild+react-dom/server 渲染真实源码；诚实缺口：useEffect 生命周期与 CSS 未覆盖）。npm run build 含 tsc 零错误。

验收核心（PLAN §七）：
- 基线轨迹不变性：test_execution_funnel.py 四个不变性测试——观测健康 / SQLite 写失败 / JSONL 写失败 / payload 构造失败，四种状态下交易调用轨迹与 fixture 逐位一致，意图与持仓不变。
- 每包红→绿输出在 §6 台账（红证据关键行已记录）。
- 四盘全量 761 / 642+1skip / 927 / 891 全绿；check_p2_sync.py exit 0（65 共享文件含新模块 execution_funnel.py；未改排除清单）；前端 build 零 TS 错误。
- 冻结区 / .env / 分析师 / 订单参数 / P0 止损链不变（test_stop_reconciliation + test_p0_hardening 止损回归原样全绿）；新回查全部 mock / 临时库，无真实订单。

留你终审的设计决定（§6 台账有完整上下文）：
1. record_intent_event 按位置参数个数区分旧式 (timestamp,symbol,kind) 与新式 (symbol,kind,deferred,detail)（向后兼容所需）；结尾带 conn.commit()（与模块 save/clear 风格一致）。
2. 词表扩展 intent_cleared（already_held/pending_buy）——flush 清除旧意图的终态事件。
3. A-3 回查成功路径 status 经 .lower() 规范化（Alpaca 枚举本为小写）。
4. payload 白名单含 submit_status/notional/filled_at（领导补充：显示必要、无敏感性）。
5. A-4：attempts=触碰意图的不同 run 数（另列 wait_events）；身份列 NULL 旧行进 mode 无关降级桶；filled_verified 规则（order_partial 优先）。
6. 基线轨迹记录语义（含丙改动的工作树，路径被逐位基线覆盖证等价）。

未 commit——等你 APPROVED 后由用户提交（新文件清单见上）。
NEXT: 等你的复审结论。
