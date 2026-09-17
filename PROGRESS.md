# PROGRESS.md — 执行进度台账（ChatGPT 规划 → 多模型执行）

> **给任何接手者（ChatGPT / Claude / Kimi / 人）：这是唯一的进度真相源。**
> 规划来源：ChatGPT「Codex-Planning-Rules」会话（2026-09-15，四个 C2C 任务：
> 工作区盘点 c2c_9c4d → P1 迭代五包计划 c2c_b71a → 模型选择 c2c_e5a1 → 决策 C 隔离实验书 c2c_f7c9）。
> 原始导出在 `C:\Users\helow\Downloads\Codex-Planning-Rules.md`（未入库，含外部链接）。
>
> **协作规则**
> 1. `AGENTS.md` 铁律优先于本文件；冻结区（`risk.yaml` / `watchlist.yaml` / `signals/` / `decision/` / 仓位常数）不碰。
> 2. 每个工作包由**一个员工 chat** 执行；完成后执行者把「状态 / 改了哪些文件 / 测试输出 / 同步证据」追加到 §6，并把 §3 状态表改成 `DONE-待审`。
> 3. 领导（Claude Opus 5 本会话）核验测试与四盘同步；ChatGPT 通过 MCP 独立读 diff 复审后才改成 `DONE`。**执行者自述完成不算完成。**
> 4. 不 `git commit` / `stash` / `reset`，除非用户明确要求。当前脏工作树是既有工作，不能覆盖。
> 5. 改 `src/agentic_trading/**` 里非 `dashboard/` 的文件 = 共享文件：必须拷到 `Trading-P2` / `Trading-P3` / `Trading-P4`，`python check_p2_sync.py` exit 0，并在**每个有该文件的盘**跑测试。
> 6. 测试不得污染 live 状态（`tests/conftest.py` 只自动隔离 heartbeat / live_events / progress 三项；journal、cache、意图文件要自己用 tmp 路径）。
> 7. **汇报方式（用户 2026-09-17 定）**：员工 chat 做完一个子包后，直接给领导会话「项目文件审查与执行规划」发消息（改了什么 / 测试数字 / sync 输出 / 卡点），领导审完再下发下一个子包；过程中不监督、不催。员工之间不互相改对方的文件。

---

## 1. 基线（2026-09-17 11:40 ET，Claude 核实）

| 项 | 值 |
|---|---|
| 分支 / HEAD | `mechanics-2026-08-30` @ `b626748`（无 upstream，未合回 master） |
| 工作树 | 23 个已跟踪文件未暂存修改 + 15 个未跟踪条目（见 `git status`）；**共享模块 `broker_read.py` / `round_trips.py` / `progress.py` / `live_events.py` 仍未跟踪但已被已跟踪代码 import** |
| 测试 | **P1 462 passed**（`AGENTS.md` 写的 424 已过期，勿再引用） |
| 四盘同步 | `check_p2_sync.py` exit 0：63 个共享文件在 P1/P2/P3/P4 逐字节一致，`risk.yaml` 一致 |
| P1 账户（GET-only） | equity **$8,263.29**，cash $3,634.75，invested $4,628.54，last_equity $9,729.22；持仓 4：MSFT / NVDA / IQV / ARGX；开口止损 4/4 |
| P2 账户 | equity $48,312.56，持仓 6（HALO / NIQ / ZETA / OKTA / VEEV / PSX） |
| P3 | $3,000 现金，无持仓 |
| P4 | `.env` 无 Alpaca 凭证（读取器拒绝） |
| 计划任务 `AgenticTrading` | Ready，Last Run 09-17 09:45 结果 0，Next 16:15 |
| LLM 分析师 | `codex.exe` v0.154.0 / gpt-5.6-terra / effort high —— **当前不可用，见 F2** |

---

## 2. 取证发现（P0-A 只读部分 —— 已完成）

### F1 ⚠️ VEEV 持仓在 P1 账户里凭空消失（账实不符，需用户去 Alpaca 后台核查）

| 时间 (ET) | 事件 | 证据 |
|---|---|---|
| 09-16 15:15 | VEEV 市价买入 $1,538.55 → 成交 5.831337174 股 @ $263.84 | 券商 order `8e69bd52`，两笔 FILL |
| 09-16 15:35 | 保护性止损 @ 239.68 提交，**DAY 单，收盘 expired** | order `a5889964` status=expired |
| 09-16 19:19 | 深周期（本应 16:15，**晚了 3 小时**）读到 **5 个持仓、equity $9,751.83**；重挂 VEEV 止损 @ 239.69 | `logs/2026-09-16.log:661,673`；order `3030b51d` |
| 09-17 04:00 | 券商处理排队单：VEEV 止损 **rejected**（`failed_at 08:00:03Z`），其余四只正常转 new | order `3030b51d` |
| 09-17 09:35 | 快扫读到 **4 个持仓、equity $8,240.62**，cash 不变 $3,634.75 | `logs/2026-09-17.log:4` |

- 全类型 activities（09-16 / 09-17）只有 7 条 FILL，**没有 VEEV 卖出、没有任何非交易活动**（无 CA / 分红 / 调整）。
- **P2 账户的 VEEV 10.65 股仍在** → 不是公司行动，是 P1 账户独有异常（Alpaca paper 侧或人为）。
- 净值 $9,729 → $8,263 的跌幅（≈ −$1,466）≈ VEEV 市值，**不是策略亏损**。
- 系统**完全没有察觉**：`progress.json` 写 `unresolved: []`、`stops 4/4`；heartbeat 健康；journal 静默把 `position_state` 从 5 行改成 4 行。
- `round_trips(fills)` 仍把 VEEV 算成 open 持仓 5.83 股 → fills 派生账本 vs 券商 positions 不一致，且没有任何代码路径报告这个差异。
- **用户待办**：登录 Alpaca paper 后台看 P1 账户的 Positions / Account Activity / Order History，确认是否有人为操作或平台侧调整；必要时联系 Alpaca 支持。系统侧对应工作包：P0-A-3。

### F2 ⚠️ LLM 故障根因 = Codex 用量额度耗尽（不是 stdin）

- 09-16 09:45、09-16 19:19、09-17 09:45 三个深周期各 3 次 `Analyst CLI exited 1` 后熔断，**决策全部 quant-only，新开仓 fail-closed 降级 WAIT**（VEEV +0.39 过阈值但被挡）。
- 日志只截 stderr 前 500 字，只看到 `Reading additional input from stdin...`（这是 Codex 在非 TTY 下的无害前缀，`stdin=DEVNULL` 修复是对的）。
- 用相同参数隔离复现（`codex exec --ephemeral --sandbox read-only`，stdin `/dev/null`）拿到完整 stderr：
  `ERROR: You've hit your usage limit. ... try again at Sep 20th, 2026 4:00 PM.`
- 09-09 / 09-10 的失败大概率同因（同前缀）；09-08、09-11、09-14、09-15 全部成功（每日 19–22 次 `quant + LLM` 决策）。
- 含义：**P1 从 09-16 起到至少 09-20 16:00 ET 不会开任何新仓**（周末），实际最早 09-21 周一恢复。P2 若同用 Codex 亦同。
- **用户待办**：决定 ① 等额度恢复；② 临时把分析师切到 `claude` / `kimi` CLI（改 `.env`，属运行配置变更，ChatGPT 建议不更换分析师，但当下是 0 LLM 状态）；③ 购买额度。系统侧对应工作包：P0-B-1（把真实错误尾部记进日志 + 分类 quota/auth/timeout）。

### F3 `round_trips.py` 已平仓回合 avg_entry = 0（已用真实数据复现）

- `round_trips.py:51` 平仓时 `cost = avg * max(qty, 0)` 清零，`:61` 再用 `cost / bought_qty` 算均价。
- 真实 P1 fills（45 条）跑出的 5 个已平仓回合 **全部 `avg_entry: 0.0`**：AMZN −108.25、JPM −25.58、ACET −34.33、XOM −108.49、AVGO +8.01。
- realized_pnl 本身另行累计，数值不受影响；ChatGPT 指出的 JPM 周报 −25.98 vs 重算 −25.58 差 $0.40 尚无解释。
- `tests/test_dashboard.py:152-160` 只断言 pnl，不断言均价。对应工作包：P0-A-2。

### F4 「Protective stop placed」≠ 券商已接受

- `execution/broker.py` 提交后即记 INFO「placed」，不回查订单终态。VEEV 那笔 `3030b51d` 提交时 accepted，04:00 ET 转 **rejected**，日志与心跳都当作已保护。
- 覆盖判定按 symbol 存在性（`run.py` `_reconcile_protective_stops`），不校验数量、不校验 status ∈ {new, accepted, held}。对应工作包：P0-B-2。

### F5 周期晚跑未被标记

- 09-16 深周期计划 16:15 ET，实际 19:19 ET 才跑（日报也 19:18 才生成）；机器大概率休眠，Task Scheduler `StartWhenAvailable` 补跑。heartbeat / progress 只写「ok」，没有「晚 184 分钟」的字段。对应工作包：P0-B-3。

### F6 fills 读取边界

- `broker_read.fills()` 上限 1000 条、无 `id` / `order_id`、分页按最后 id 推进但不去重、返回值没有「是否截断」信号。当前 45 条未触顶，但 AVGO 2026-06-15 的历史成交说明账户早于系统启动（08-18）就有交易，回合核算必须能标「期初买入缺失」。对应工作包：P0-A-2。

---

## 3. 工作包状态表

状态：`TODO` / `进行中` / `DONE-待审`（执行者自报）/ `DONE`（ChatGPT 或领导审 diff 后）/ `阻塞`

| ID | 工作包 | 状态 | 执行者 | 涉及文件 | 验收 |
|---|---|---|---|---|---|
| P0-A-1 | 只读取证基线 | **DONE** | Claude Opus 5（领导） | 本文件 §1–2 | — |
| P0-A-2 | 成交核算修复：avg_entry；孤儿卖出标 `incomplete`；fills 带 `id`/`order_id`、按 id 去重、截断标记 | TODO | 员工 A | `round_trips.py`、`broker_read.py`、新 `tests/test_round_trips.py`、`test_daily_report.py`、`test_dashboard.py` | 见 §5-A |
| P0-A-3 | 账实对账：fills 派生 open lots vs 券商 positions 差异列示（VEEV 场景必须报出来），进日报 + progress `unresolved` | TODO | 员工 A（第二步） | `daily_report.py`、`progress.py`、`run.py`（只加观测，不改决策） | 见 §5-A |
| P0-B-1 | CLI 故障诊断：脱敏记录 stderr **尾部**、退出码、耗时；分类 quota / auth / timeout / parse；心跳与 progress 显示「LLM 不可用原因」 | TODO | 员工 B | `llm/cli_provider.py`、`heartbeat.py`、`progress.py`、`tests/test_cli_provider.py` | 见 §5-B |
| P0-B-2 | 止损保护链：提交后核验订单终态；覆盖按数量 + status；rejected/expired 未重挂 → 心跳告警且 progress `unresolved` | TODO | 员工 B | `execution/broker.py`、`run.py`、`broker_read.py`、`tests/test_stop_reconciliation.py`、`test_p0_hardening.py` | 见 §5-B |
| P0-B-3 | 周期时效：heartbeat 记录计划槽 vs 实际开始时间；晚跑 > N 分钟标记；`--check` 据此告警 | TODO | 员工 B | `heartbeat.py`、`run.py`、`tests/test_heartbeat.py` | 见 §5-B |
| P1-A | 「为什么没成交」漏斗（意图→尝试→闸门→提交→成交），`accepted` 不再显示为 `filled` | TODO（等 P0 完） | 员工 C | `run.py`、`live_events.py`、`journal/logger.py`、`progress.py`、`dashboard/views.py`、`Today.tsx` | ChatGPT 计划 P1-A |
| P1-B | paper-forward 质量台账：evaluate 按 `paper` 过滤、各 horizon 成熟样本数、池内/遗留分列；健康展示区分盘中停摆 vs 休市 | TODO（等 P0 完） | 员工 C | `journal/evaluate.py`、`dashboard/views.py`、`daily_report.py` | ChatGPT 计划 P1-B |
| P2-A | 研究预注册议案（只写文档，不跑实验） | TODO | ChatGPT + 用户 | `research/` | 用户批准 |
| EXP-C | 隔离 paper 实验书（SPY 200 日均线月度趋势规则） | **取消**（用户 09-17） | — | — | — |

---

## 4. 用户决定（2026-09-17 12:05 ET 拍板）

| # | 事项 | 决定 | 落实 |
|---|---|---|---|
| 1 | VEEV 消失（F1） | **向 Alpaca 申诉**，由 Claude 起草 | 申诉稿见 §7；发送前用户过目 |
| 2 | LLM 分析师停摆到 09-20（F2） | **不改 `.env`、不换分析师**，等额度恢复；P0 工作照推 | 无系统改动。P1 在此期间只 HOLD/EXIT，不开新仓（fail-closed 正常行为） |
| 3 | 脏工作树 | **直接 commit 作为基线** | 已提交，见 §6；`.gitignore` 补 `data/*.jsonl`。员工在途改动（A：`round_trips.py` `broker_read.py` `daily_report.py` `run.py`；B：`cli_provider.py`）**未入基线**，基线用的是 P2 同名副本（即员工动手前的版本），所以员工的 `git diff` 只含自己的改动 |
| 4 | 隔离实验书 EXP-C | **不做** | §3 该行改为「取消」；ChatGPT 决策 C 的四个门槛不再作为本轮目标 |

## 5. 员工 chat 任务书（可直接整段粘贴到新 chat）

### 模型分配建议

| 员工 | 工作包 | 建议模型 | 理由 |
|---|---|---|---|
| **A** | P0-A-2 → P0-A-3 | Claude Code **Sonnet 5**（Trading 目录） | 规格已钉死、有真实数据复现、纯 Python + 测试；不碰订单生命周期。便宜且够用 |
| **B** | P0-B-1 → B-2 → B-3 | Claude Code **Opus 5**（Trading 目录） | 碰 `execution/broker.py` / `run.py` 保护链，交易安全关键路径，对应 ChatGPT 的 `xhigh` 档 |
| **C** | P1-A → P1-B | Claude Code **Sonnet 5** 或 **Kimi k3**（等 P0 完再开） | 观测层为主；Kimi 已熟悉本项目周报 |
| 复审 | 每包 diff | **ChatGPT**（现有 Codex-Planning 会话，MCP 读 Trading） | 保持 C2C 协议：EXECUTED → 独立读 diff → 通过/退回 |
| 领导 | 验收 / 同步 / 冲突仲裁 | Claude **Opus 5**（本会话） | 只做核验与调度，不写代码 |
| ❌ 暂不用 | — | Codex（gpt-6-astra / gpt-5.6-*） | 额度耗尽至 09-20 16:00 ET；恢复后可按 ChatGPT 建议接手 B 类任务 |

### 5-A 员工 A 任务书（P0-A-2 + P0-A-3）

```
你在 C:\Users\helow\Documents\Trading 工作。先读 AGENTS.md 铁律和 PROGRESS.md 全文，然后只做下面两步，不做任何策略/冻结区改动，不 commit。

第一步 P0-A-2：成交核算修复
1. src/agentic_trading/round_trips.py：已平仓回合 avg_entry 为 0（第 51 行清零 cost 后第 61 行再用）。改为单独累计买入名义额（bought_notional），avg_entry = bought_notional / bought_qty；avg_exit 同理用 sold_qty 而非 bought_qty。
2. 同文件：卖出时若无持仓（历史窗口缺期初买入），不要静默丢弃——产出一条 {"open": False, "incomplete": True, "avg_entry": None, "realized_pnl": None, ...} 并保留 symbol / closed_at / qty。消费者 daily_report._closed_trips_table 与 dashboard api 对 None 要能显示「不完整」而不是 $0。
3. src/agentic_trading/broker_read.py fills()：每条加 "id" 和 "order_id"；跨页按 id 去重；触到 max_records 时把最后一条之外的截断信息暴露出来（建议返回 list 但在 reader 上记 self.last_fills_truncated: bool，并在 daily_report 里显示「成交历史可能不完整」）。
4. 新建 tests/test_round_trips.py，覆盖：买1@100 卖1@110 → avg_entry 100 / avg_exit 110 / pnl 10；分批买入后一次卖出；部分卖出保持 open 且 avg_entry 正确；卖完再买开新回合；重复 fill id 去重；同一时间戳；缺期初买入 → incomplete；截断标记。用固定样本，不连网。
5. 用真实数据验证（只读）：python -c 里调用 BookBrokerReader(Path('.')).fills() → round_trips()，确认 AMZN/JPM/ACET/XOM/AVGO 五个回合 avg_entry 不再是 0，realized_pnl 不变。

第二步 P0-A-3：账实对账
6. 在 daily_report.py 增加「账实对账」节：fills 派生的 open lots（round_trips open=True）与券商 positions 逐 symbol 比对 qty；差异（symbol 在一边不在另一边、或 qty 差 > 1e-6）逐行列出，标「未解释」。当前真实场景：fills 说 VEEV open 5.831337174 股，positions 里没有 → 必须报出来。
7. progress.py / run.py：把同样的差异写进 progress.json 的 unresolved（只加观测，不改任何决策或下单逻辑；写入走 _journal_safe 类的吃错误包装，失败不能中断周期）。
8. 补 tests/test_daily_report.py、tests/test_progress.py 用例：一致 / 少一只 / 多一只 / 数量不等。

收尾（每步都要）：
- 改了共享文件（round_trips.py、broker_read.py、daily_report.py、progress.py、run.py）后拷到 ..\Trading-P2、..\Trading-P3、..\Trading-P4 同路径；python check_p2_sync.py 必须 exit 0。
- 在 P1 和 P2 各跑 .\.venv\Scripts\python.exe -m pytest -q（P1 基线 462 全绿；P2 自己记录基线数）。P3/P4 若有 tests 也跑。
- 把「改了哪些文件、测试数字、sync 输出、真实数据验证输出」追加到 PROGRESS.md §6，把 §3 对应行改成 DONE-待审。不要 commit。
```

### 5-B 员工 B 任务书（P0-B-1 → B-2 → B-3）

```
你在 C:\Users\helow\Documents\Trading 工作。先读 AGENTS.md 铁律和 PROGRESS.md 全文（尤其 §2 F1/F2/F4/F5）。只做机制与观测，不改冻结区，不改分析师型号，不改 .env，不 commit，不下单。

P0-B-1 CLI 故障诊断（先做，最小改动）
1. src/agentic_trading/llm/cli_provider.py analyze_via_cli：非零退出时记录 stderr 的「首 200 字 + 尾 600 字」（现在只截前 500 字，把真实错误 "You've hit your usage limit" 截掉了），加退出码与耗时；对已知模式分类：quota（"usage limit"）、auth、timeout、parse、unknown。分类结果通过返回值或模块级 last_failure 暴露给 run.py。
2. run.py 熔断后把失败分类写进 heartbeat（新增可选字段 llm_status）与 progress.json 的 unresolved（例如「LLM 不可用：quota，恢复 09-20 16:00 ET」）。
3. tests/test_cli_provider.py：假进程覆盖 quota / timeout / 坏 JSON / 三连败熔断 / 恢复；断言日志含尾部而非只含头部。

P0-B-2 止损保护链
4. execution/broker.py：保护性止损提交后回查订单 status；rejected/expired/canceled 不得记为「placed」，要 WARNING 并返回失败。
5. run.py _reconcile_protective_stops：覆盖判定改为「status ∈ {new, accepted, held, partially_filled} 且 qty ≥ 持仓 qty」；数量不足或状态未知 → 不计入 covered。心跳的 stops_covered/total 据此计算；--check 对 covered < total 告警。
6. broker_read.open_stop_orders() 补 qty 与 status 字段。
7. tests/test_stop_reconciliation.py、test_p0_hardening.py：同价但数量不足、rejected、expired 未重挂、挂单不可读 → 均不得报绿。

P0-B-3 周期时效
8. heartbeat.py：写入时附计划槽（deep 09:45/16:15，fast 网格）与实际开始时间，算 late_minutes；--check 对 late > 30 分钟输出告警（09-16 深周期晚了 184 分钟，heartbeat 却只写 ok）。
9. tests/test_heartbeat.py 补：准时 / 晚跑 / 漏跑。

收尾：同 5-A 的四盘同步 + P1/P2 测试 + 追加 PROGRESS.md §6。改 run.py / broker.py / heartbeat.py / cli_provider.py 全部是共享文件。
```

### 5-C 员工 C 任务书（P1-A / P1-B）

等 P0-A、P0-B 全部 `DONE` 后再开；任务书按 ChatGPT 计划 c2c_b71a 的 P1-A / P1-B 原文起草（见 Downloads 导出文件 §四），由领导届时补充。

---

## 6. 执行日志（按时间追加，最新在下）

- **2026-09-17 11:40 ET（Claude Opus 5，领导）**：读完全部一方文本 + ChatGPT 四个 C2C 输出；跑 P1 测试 462 绿；`check_p2_sync.py` exit 0；GET-only 读四盘券商状态；隔离复现 Codex 故障拿到完整 stderr（额度耗尽）；真实 fills 复现 avg_entry=0；发现 VEEV 持仓消失且系统未察觉。**零代码改动，零下单，零 commit。** 产出本文件。下一步：用户开员工 A / B chat，粘贴 §5 任务书。
- **2026-09-17 12:10 ET（Claude，领导）**：用户拍板四项（§4）。**基线 commit `72431f0`**——员工在途的 6 个共享文件（round_trips / broker_read / daily_report / run / cli_provider / heartbeat）按 Trading-P2 副本（动手前版本）入库，员工新建的 `tests/test_round_trips.py` `tests/test_broker_read.py` 未入库；`.gitignore` 补 `data/*.jsonl`。commit 后工作树只剩员工改动。已给员工 A / B / C 发协作消息（A 继续 P0-A-2→A-3；B 做完 B-1 停下汇报、B-2 方案先审后改；C 只读拆包，不动代码）。Alpaca 申诉稿已存 Gmail 草稿（见 §7），等用户发送。

## 7. Alpaca 申诉（F1 VEEV）

- 状态：**Gmail 草稿已建**（收件人 support@alpaca.markets，主题 "Paper account PA369LRIBAYU: VEEV position (5.831337174 sh) vanished overnight…"），用户过目后自行发送或让领导发。
- 内容要点：账户 PA369LRIBAYU；买入 order `8e69bd52` 09-16 19:15Z 成交 5.831337174 @ 263.84；23:19Z 仍有 5 持仓 equity $9,751.83；止损 `3030b51d` 09-17 08:00:03Z 被 rejected；09-17 13:35Z 只剩 4 持仓，cash 不变；全类型 activities / 全状态 orders 均无 VEEV 卖出或调整；同登录下另一 paper 账户 VEEV 仍在。请求：解释机制、恢复股份或等值净值、确认是否可复发。
- 回复到达后：把 Alpaca 的答复摘要追加到本节，并决定 P0-A-3 对账逻辑是否需要针对"平台侧删除"加特殊分类。

