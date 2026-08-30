# 模型与 Agent 架构研究 — 2026-08-22（中文版）

## 实施更新——2026-08-22 晚些时候

用户已经批准下文历史记录中所说的共享提示词改写。`SYSTEM_PROMPT` 现已改成
直接的任务指令，`build_user_prompt()` 会输出一段完整连贯的快照，并以明确的
分析要求收尾。Kimi Code 已用新提示词成功返回有效的 `AnalystVerdict`，全部
214 项测试通过。实际 `.env` 现在已指向 Claude Code，使用 `sonnet` / high
effort，并将工具面清空（`--tools "" --strict-mcp-config
--disable-slash-commands --no-session-persistence`）。Claude 的改后冒烟调用
已经成功返回有效的 `AnalystVerdict`，Claude 项目目录也保持不变，仍为 10 个
文件、11.94 MB。CLI 失败仍会安全返回 `None`，系统随即退回纯 quant 路径。
本节取代下文保留作审计记录的旧“已切回 Kimi”状态说明。

> 本文是 `model-architecture-research-2026-08-22.md`（英文原版）的完整中文翻译，内容与结构保持一致，供中文阅读或转给其他中文/多语言 agent 使用。若两者出现差异，以英文原版为准（原版会优先更新）。

**这份研究要回答的问题：** Agentic Trading 是不是必须绑在 Claude Code 上，还是 GLM / GPT / Grok / Kimi / DeepSeek 可以替代——锁定（lock-in）到底在哪里是真实存在的，哪里只是想象出来的。研究计划来自 `Pro强在哪.md` 里那段 `chatgpt.com/c/6a869b66...` 对话，这里做了压缩，并且全部锚定在这个仓库的真实代码上，而不是当成一个假设性问题来空谈。

**关键结论的置信度标注：** **[code]** = 直接读这个仓库的代码验证过。**[primary]** = 直接抓取了厂商官方文档/条款原文验证过。**[web]** = 搜索引擎聚合的 2026 年博客/文档内容摘要——大方向可信，但没有我自己独立核实过。定价/跑分类数字变化很快，凡是标 **[web]** 的，都按"2026-08-22 前几周为真"来看，不要当成绝对准确的最新数字。

---

## 太长不看版（TL;DR）

- 你早就已经不是"把 Claude Code 当运行时"了。它从来就不是运行时——它只是一次 `AnalystVerdict` 调用里两个可互换的非交互式后端之一，真正的决策/风控/下单流程是纯 Python，没有用任何框架，执行路径里也没有任何模型调用。**[code]**
- 这周真正值得处理的不是架构问题，而是**合规问题**：你的默认配置让 Kimi Code CLI 在无人值守的计划任务里跑，而 Kimi Code 自己的社区准则明确禁止这种用法。相比之下 Claude Code 的消费者条款明确**允许**一模一样的用法。**现在已经真正落地了，不只是停留在建议层面**——见第 1 节 2026-08-22 的更新：第一次切换失败了（这个账号的 Claude Code 拒绝非交互式回答旧格式的 prompt），后来通过改写共享的分析师 prompt 修好了，并且对 Claude Code 和 Kimi Code 都做了实测验证。**[primary]**
- "是不是格式问题"——不是。GLM、Kimi、DeepSeek 现在都各自提供了一个能说"真正 Anthropic Messages API 协议"的端点，让没改过的 `claude` 这个二进制文件只要改一个环境变量就能对着它们的模型跑。它们同时也都支持 OpenAI 兼容的 chat completions，这正是你 `api_provider.py` 已经在用的方式。格式问题在这一整批厂商里已经被解决了。见第 3 节。
- Model router（ChatGPT 计划里的第 7 部分）已经有现成方案（LiteLLM），接入只需要改 `base_url`，不需要重写——但你现在只有 3 个 provider 模块，还不需要它。见第 7 节。
- 发现一个具体的安全缺口：`grok_provider.py` 对子进程做了硬性的 shell/工具访问限制；`cli_provider.py`（你的 Kimi Code / Claude Code 路径）只是在 prompt 里"口头请求"模型不要用工具。见第 8 节。

---

## 1. 优先处理：默认配置存在 ToS（服务条款）冲突

`ANALYST_PROVIDER=cli` 是你的默认配置 **[code]**，通过 Windows 计划任务无人值守运行——每天两次，外加盘中每 20 分钟一次。这基本上就是教科书级别的"脚本化、非交互式、无人值守自动化"。

我直接核实了这四个后端各自当前的官方准则原文（是直接抓取的原始页面，不是只看搜索摘要）——包括第一版没查的 DeepSeek 和 Grok 这两家：

| 后端 | 官方条款怎么写 | 来源 |
|---|---|---|
| **Kimi Code**（你现在的默认 `cli` 目标） | "Don't use Kimi Code for non-interactive automation. Kimi Code subscriptions are for personal interactive use only."（不要把 Kimi Code 用于非交互式自动化，订阅仅限个人交互式使用。）"Using it for non-interactive purposes — such as scripted batch execution... — goes beyond normal use."（用于非交互目的——比如脚本化批量执行——已经超出正常使用范围。）违规后果："we'll review the situation first and take appropriate action — such as suspending access."（我们会先审查情况，再采取相应措施——比如暂停访问权限。） | **[primary]** [Kimi Code Community Guidelines](https://www.kimi.com/code/docs/en/kimi-code/community-guidelines.html) |
| **Claude Code**（你已验证可用的替代 `cli` 目标） | Anthropic 的消费者条款总体上禁止自动化/脚本化访问，但 Claude Code CLI 被明确列为例外——官方文档展示了把 `-p` 接入 cron/CI/GitHub Actions 的用法，视为被认可的使用模式。 | **[web，中等置信度]**——我没能直接抓取 anthropic.com/legal/terms，建议你自己去读一遍原文再依赖这个结论 |
| **GLM Coding Plan**（假设未来接入 `cli`/`api`） | 直接抓取了 `docs.z.ai/devpack/usage-policy` 原文：**没有找到"禁止自动化"或"仅限个人使用"这类明文条款。** 页面写的是："GLM Coding Plan may only be used within officially supported tools and products"（GLM Coding Plan 只能在官方支持的工具和产品内使用）；禁止账号共享；违规超过 3 次可能封号。条款没提automation，不等于默许——这条"只能用官方支持的工具"本身就留了余地，Z.AI 完全可以把无人值守批量调用定义为"非官方支持的用法"。 | **[primary]**，但原始条款本身在这个具体问题上表述模糊——应视为"未有定论"，不是"已经放行" |
| **DeepSeek**（你 `api_provider.py` 天然的低成本替代选项） | 直接抓取了 DeepSeek 开放平台服务条款：**没有找到任何自动化限制条款**，而且从产品结构上看这里根本不存在张力——DeepSeek 压根不卖 Kimi Code / GLM Coding Plan 那种"个人订阅固定额度"产品（截至 2026 年 8 月都没有这类产品）。它只有按 token 计费的开放平台 API，这种计费方式天生就是为程序化调用设计的。 | **[primary]** [DeepSeek Open Platform ToS](https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html) |
| **Grok Build**（你现有 `grok_provider.py` 对接的目标） | xAI 自己的产品发布文章原话："Headless mode (`-p`) allows easily running agents inside scripts and automations."（Headless 模式让你可以很方便地在脚本和自动化流程里跑 agent。）另外还提到支持"full ACP support to build your own bots and agent orchestration apps"（完整 ACP 支持，方便你自建机器人和 agent 编排应用）。这是厂商自己在官方发布文章里正面宣传你现在这种用法——四家里唯一一个不只是"没禁止"，而是**明确允许**的。 | **[primary]** [x.ai — Introducing Grok Build](https://x.ai/news/grok-build-cli) |

按"这件事上限制从严到松"排序：**Kimi Code（明确禁止）< GLM Coding Plan（未有定论，没查到禁令但也没查到许可）< DeepSeek（这类产品分类根本不存在，所以没有张力）< Grok Build（官方明确把这种用法当卖点宣传）。** 你现在已经在用的 `grok_provider.py` 集成，恰好已经站在这个光谱里最安全的一端；真正跟厂商条款不合拍的，恰恰是默认的 `cli` 分析师路径（Kimi Code）。

这不是杞人忧天式的过度谨慎——这就是照着一份准则原文平铺直叙地读出来的结论，而你现在跑着的实盘流程正好跟它不合拍。实际影响：如果 Moonshot 真的执行这条规则，`analyze_via_cli` 会开始失败，而你的系统本来就能优雅降级（回落到纯 quant 决策，按 `schema.py` "绝不瞎猜" 的原则）——所以不是灾难性故障，但属于本可以避免的自找麻烦，而且在一个实盘账户上悄无声息地丢失定性判断这一层，比主动修一下要更糟。

**三种解决方式，按省事程度排序：**

1. ~~零成本、当天见效：把 `ANALYST_CLI_PATH` 指向你的 `claude` 二进制文件，而不是 `kimi.exe`。~~ **2026-08-22 实际试过了，后来改回去了——见下面的更新。README 里"直接能用"的说法，其实只验证了信封格式能被正确解析，没验证模型是否真的会好好回答。**
2. **把这个槽位切到 `ANALYST_PROVIDER=api`**（按 token 付费的 Kimi Platform key）——这正是 Kimi 官方准则明确建议自动化场景该走的路径，你已经有现成实现且能跑。代价是花一点真金白银，而不是吃订阅额度。**鉴于方案 1 的结果，现在这条反而是更值得选的方向。**
3. **继续用 Kimi Code CLI，但把调用频度降到能算作"个人使用"的程度**——考虑到现在 20 分钟一次的 fast-scan 频率，这条很难站得住脚。

---

**2026-08-22 更新，这是真正动手尝试方案 1 之后写的：** 我把 `ANALYST_CLI_PATH` 切到了 `claude.cmd`，过程中在 `cli_provider.py` 里发现并修复了三个真实存在的 bug（都已经通过实际调用验证过，代码也已经修好并保留）：`-m` 对 Claude Code 来说不是一个合法参数，只有 `--model` 才行（Kimi Code 恰好两种都支持，所以这个问题之前一直没被发现）；现在的 Claude Code 只要 `--print` 和 `--output-format stream-json` 一起用，就必须同时带上 `--verbose`（这一点 `claude --help` 里完全没写，是我实测才发现的——不加就会报错）；另外 assistant 消息的信封格式变了，现在是 `{"type": "assistant", "message": {"role": "assistant", ...}}` 这种嵌套结构，`_extract_text` 原来没处理这种形状（现在修好了，还补了一个回归测试）。同时新增了 `ANALYST_CLI_EXTRA_ARGS`，这样 `--effort` 之类各家 CLI 专属的参数、以及工具锁定（`--tools "" --strict-mcp-config --disable-slash-commands`，正好把下面第 8 节提到的那个缺口堵上）就不用硬编码进共享代码里了。

但光这些还不够。上面这些机制层面的东西全部验证跑通之后（确认过：`--model opus --effort max` 能被正确接受，`session-init` 事件里 `tools`/`mcp_servers`/`skills`/`slash_commands` 全部为空），这个账号的 Claude Code 依然不肯正面回答 `schema.py` 实际发出的那个 prompt——也就是 `SYSTEM_PROMPT` 后面接 `build_user_prompt()` 那种带小标题分段的格式（"Symbol: AAPL\n\n Quant technical signal (...):\n- last_price: ..."）。换真实股票代码、换各种 effort 档位、试过 `--system-prompt`（把系统提示放到真正该放的参数位，而不是拼接进 user turn）、试过 `--bare`（顺带纠正一下前面 ToS 讨论里的一个点：这个账号用 `--bare` 实际上并没有像文档警告的那样因为缺 `ANTHROPIC_API_KEY` 而认证失败）、试过 `--permission-mode bypassPermissions`——结果全都一样：它会把这条消息当成一个不完整的请求，反过来问澄清问题（"能告诉我股票代码、quant 信号、新闻吗..."），或者主动提议帮你跑这个账号自己装的某个财经技能（`stock-eval`、`full-report`、`dcf-valuation`——这些都是这个账号真实安装的技能名，而且在 `--disable-slash-commands` 和 `--bare` 同时生效的情况下依然会被提到，这就排除了"配置泄漏"的可能，说明这是模型自身对"报告状"输入的一种训练出来的行为倾向）。

通过直接对比测试，定位到了真正的触发点：是 `SYSTEM_PROMPT` 里那种"人设设定"式的开场白（"You are a disciplined equity research analyst supporting a systematic paper-trading agent..."）出现在一次性、非交互式调用里导致的。把这段开场白去掉，把同样的数据改写成一整段连贯的话、以一个直接的问题收尾，Claude Code 就能正确、且回答得不错——这个我测过，确认有效。但这意味着要去改 `build_user_prompt()`/`SYSTEM_PROMPT`，而这两者目前是 `api_provider.py` 和 Kimi Code 的 `cli` 路径在生产环境里都依赖的共享逻辑，改动会影响到已经在正常工作的部分。这是一个关系到实盘账户的 prompt 工程改动，不是一次 CLI 切换——不该由我单方面决定。

**2026-08-22 当天更新，已经动手改并得到了明确授权：** 改写已经完成。`schema.py` 的 `build_user_prompt()` 现在是一整段连贯文字，以"现在就评估定性立场和置信度——不要再问更多信息"收尾；`SYSTEM_PROMPT` 还在，`api_provider.py` 依然正确地把它当作真正的 system 角色消息在用（这条路径本来就没问题，出问题的只是 CLI 路径把它拼进 user turn 这一点）。`cli_provider.py` 现在对 Claude Code 完全不再拼接人设开场白（`is_claude` 分支），而且从"prompt 里口头要求返回 JSON"换成了 Claude 原生的 `--json-schema` 结构化输出参数，配了一个能兼容 `structured_output`/`result`/`tool_use` 等多种返回形状的兜底解析器（`_extract_payload`）。临时工作目录也彻底挪到仓库外面（`tempfile.gettempdir()`，不再是这个 git 仓库下面的子目录），堵上了这次调查发现的 memory/CLAUDE.md 自动发现那条路。已经对真实 Claude Code 做了端到端实测，包括之前翻车的那个"证据很薄"的用例——现在两种情况都能给出有理有据的结论。`ANALYST_CLI_PATH` 已经改回指向 `claude.cmd`。

这里要纠正一下我自己前一版更新里的说法：`SYSTEM_PROMPT` 现在**不会**被拼进 `cli` 路径了，不管是 Kimi Code 还是 Claude Code 都一样——只有 `api_provider.py` 还在正确地用它当 system 角色消息。Kimi Code 的 `cli` 路径现在拿到的是同一套新的、一整段连贯文字的 `build_user_prompt()` 输出，外加原来那段只讲格式、不带人设的 `JSON_INSTRUCTION`——这是一个真实的行为变化，不是"没动"。我单独对真实 Kimi Code 用这个新 prompt 做了实测：依然能给出有理有据、质量不打折的结论。全部 214 个测试通过。第 9 节第 1 条建议现在是"已完成"，不再只是"计划中"。

---

## 2. 你已经搭好的东西，跟 ChatGPT 那份计划设想的有什么不同

计划第 1 部分担心的是，你可能会把交易系统直接构建**在** Claude Code 内部当运行时来用。读完代码，事实并非如此：

| 计划里的分层 | 这里实际的样子 |
|---|---|
| 开发 agent | Claude Code + Grok +（隐含地）Codex，交替用来编辑这个仓库——见 `HANDOFF-BACKTEST.md`，由 "Grok 4.6" 写给 "Codex"。没有框架锁定，靠的是 markdown 交接笔记。 |
| Agent 运行时/编排层 | 纯 Python（`run.py`、`decision/engine.py`）。没有 LangGraph，没有 Agents SDK，没有 Claude Agent SDK。 |
| 模型层 | 3 个独立、可互换的 provider 模块，共享同一套契约（`schema.py` 里的 `AnalystVerdict` / `SentimentVerdict`）——`api_provider.py`（OpenAI 兼容 HTTP，目前是 Kimi K3）、`cli_provider.py`（本地代码 agent CLI，Kimi Code 或 Claude Code，已验证可互换）、`grok_provider.py`（Grok CLI，仅做情绪判断）。 |
| 工具与数据 | `data/market_data.py`，yfinance/Alpaca——数据链路里没有 LLM 驱动的浏览或工具调用。 |
| 组合/风控 | `risk/manager.py`——确定性、硬编码的限制，在任何订单产生之前就已经评估完毕。 |
| 执行 | `execution/sim_broker.py` / Alpaca broker 客户端，仅限模拟盘，硬性拒绝实盘。 |

计划第 6 部分设想的"异构多模型"玩法，你其实已经在实践里做了：`research/GPT-REVIEW-PACK-2026-08-22-KIMI-COMPLETE.md` 就是一个 Kimi 产出、GPT 审核的产物，Grok→Codex 的交接是同一套模式用在开发工作上的体现。真正的开放问题从来不是"要不要这么做"，而是"现在这套临时的 markdown 交接机制够不够用"——以你现在的规模（一个仓库，偶尔跨天交接），够用。我不会在交接开始真的丢信息之前，就去搭额外的基础设施（比如共享的 MCP 记忆服务器、正式的交接 schema）。

---

## 3. 到底什么才是真正 Claude / Claude Code 独占的东西（对应计划第 2 部分的问题）

| 能力 | 分类 | 说明 |
|---|---|---|
| Anthropic Messages API 底层协议格式 | **C——足够开放** | GLM（Z.AI）、Kimi（Moonshot）、DeepSeek 现在都各自提供了一个模拟这套格式的端点，模拟程度足以让没改过的 `claude` CLI/SDK 通过 `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY` 直接对接它们的模型。**[web]**，多份独立的厂商接入指南都这么说。DeepSeek 自己文档里的提醒：部分内容类型（图片、文档、部分 tool-result 结构）还没做到完全对等。 |
| OpenAI chat-completions 格式 | **C——开放标准** | 上面这几家**同时也**都支持这个格式。Anthropic 自己在 2026 年 3 月也上线了一个 OpenAI-SDK 兼容端点（`base_url=https://api.anthropic.com/v1/`），官方定位是给"测试"用的，不建议用于生产——通过这层兼容，`strict` schema 一致性是不保证的。**[web]** |
| MCP（Model Context Protocol） | **C——现在是厂商中立的** | OpenAI（Agents SDK、Responses API、ChatGPT 桌面版，约从 2025 年 5 月起）和 Google DeepMind（Gemini，约从 2025 年 4 月起）都已采用。2025 年 12 月，Anthropic 把 MCP 捐给了 Linux Foundation 旗下新成立的、厂商中立的 "Agentic AI Foundation"。**[web]** 这是整个研究里"不是 Claude 独占"证据最硬的一条——起点在 Anthropic，但结构上现在已经不属于 Anthropic 了。 |
| Skills 格式 / `CLAUDE.md` 约定 | **B→D，正在趋同** | 起源于 Anthropic。xAI 明确把 Grok Build（他们 2026 年 5 月发布的编码 CLI）做成能原生读取 `CLAUDE.md`，并且支持同一套 Skills/MCP 模式，"几乎不需要改动"。**[web]** 一开始是独占的，正在变成被抄的通用惯例。 |
| Subagents / hooks / headless 模式（作为 CLI 机制而言） | **B，但在概念层面是 D** | Claude Code 具体的参数（`--permission-mode`、`--disallowedTools`、`SessionStart`/`SubagentStop` 这类 hook、Task 工具驱动的 subagent）是 Claude Code 特有的实现方式。但 Codex CLI 有 `codex exec` 满足同样的 headless 自动化需求，Grok Build 也有自己的 subagent 玩法（宣传支持 8 个并行子 agent）。**能力本身**是 D（大家都各自造了一套）；**具体参数名**是 B。 |
| Computer use / 操控浏览器 | **D，确实存在分歧** | Claude、OpenAI（Operator/Codex background）、Gemini 各自押注了不同的架构路线（可移植的截图工具 vs. 桌面原生 vs. 感知 DOM 的浏览器自动化）——没有统一标准，不同任务类型下谁的跑分领先也不一样。**[web]** 跟你现在的流程无关——你没有浏览/computer-use 这一步，数据直接来自 yfinance/Alpaca API。 |

**回到你"是不是格式问题"这个问题：不是。** 底层协议格式已经被三家不同厂商用三种方式解决了。真正还带厂商特有色彩的，是 CLI 的**参数表面**（这只影响 `cli_provider.py` 子进程调用的写法，不影响模型本身的能力），以及 computer-use（你用不上）。

---

## 4. 你现在这两个 provider 槽位的"直接替换"难度

把计划里的 Level 0–4 分级套用到你的实际代码上，而不是套在一个假想系统上：

### `api_provider.py`（OpenAI 兼容 HTTP，目前是 Kimi K3）

- **换成 GLM / DeepSeek / GPT，用同一个 OpenAI SDK 客户端：Level 0–1。** 三家都支持 OpenAI 风格的 function calling，配合 `tool_choice="required"`——这正是 `_TOOL` + `parse_verdict` 已经假设的方式。目前严格说是 Level 1 而不是 Level 0，因为 `DEFAULT_BASE_URL` 是 `api_provider.py` 里的一个 Python 常量，不是环境变量——而 `ANALYST_MODEL` 和 `MOONSHOT_API_KEY` 已经是环境变量了。加十行代码（往 `Settings` 里加一个 `ANALYST_BASE_URL`，跟现有的 `analyst_model` 模式对齐）就能让这变成真正的 Level 0、不用碰代码的切换。改动很小，很具体，不管你最后选哪家都值得先做。
- **可靠性提醒：** function calling 的准确率在各家之间并不一样。2026 年的博客聚合对比**[web，低置信度——仅供参考方向]**给出的数字是：OpenAI/Claude 大约 96–99% 的工具选择准确率，Gemini 95–98%，DeepSeek 90–95%，但价格大约便宜一个数量级。你自己的 `parse_verdict` 已经做到了——任何格式不对的输出都返回 `None`（绝不瞎猜）——所以换一个可靠性差一点的供应商，代价主要是**覆盖率**下降（更多标的默默退化成纯 quant 决策），而不是正确性下降——不管怎么换，失败模式都是安全的。

### `cli_provider.py`（本地 CLI 子进程，目前是 Kimi Code / Claude Code）

- **Kimi Code ↔ Claude Code 二进制互换：Level 0。** 现在就是这样，你自己的 README 里已经写明。**[code]**
- **继续用 `claude` 这个二进制，但通过 `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` 指向别的模型（GLM、Kimi 或 DeepSeek 的 Anthropic 兼容端点）：Level 1。** 不涉及架构改动——但 `cli_provider.py` 现在的 `env` 处理只注入了 `KIMI_CODE_HOME`（第 108-110 行），需要泛化成可以按目标传递任意环境变量，这样你才能让同一个 `claude` 二进制指向比如 DeepSeek 的端点，而不用动你自己交互式用的 Claude Code 配置。如果你想要"Claude Code 的解析器/输出格式 + DeepSeek 的价格"，又不想再加一个 provider 模块，这个改法很有用。
- **加入 Codex CLI（`codex exec`）作为第四个后端：Level 2。** 调用方式不同（是 `codex exec`，不是 `-p`），而且 `_extract_text` 里的 JSONL 解析器是照着 Claude Code 和 Kimi Code 共用的 `role`/`content` 信封结构写的——Codex 的 exec 输出格式有没有对得上这个解析器，我没验证过，大概率需要加一个小分支，不是重写。
- **加入 Grok Build（xAI 2026 年 5 月推出的新 CLI）作为后端：未验证，大概率是 Level 1–2。** 你已经通过 `grok_provider.py` 接入了普通的 Grok CLI；Grok Build 是一个独立的、更重的编码 agent 产品，被锁在每月 99–300 美元的 SuperGrok 订阅门槛后面**[web]**——我没有核实过它的非交互式输出信封格式跟你的解析器对不对得上。既然你现在用 Grok 做的唯一一件事（情绪判断）已经有一个便宜好用的集成了，没有特别的理由就不建议为此折腾。
- **Level 3–4（架构重写 / 没有对应能力）在你现在的设计里完全用不上**——只有当你想要一些性质完全不同的东西时才会碰到，比如一个能跨多轮自主浏览财报文件的多步研究 agent。不管是这份计划还是你现在的系统，目前都没有这种需求。

---

## 5. "OpenAI 兼容" ≠ 完整的 agent 兼容性——具体会在哪里坑到你

计划第 4 部分的担心是真实存在的，但踩坑的地方跟它预想的不一样。Chat Completions / function calling 这一层，是你整个供应商名单里真正做到标准化的那一层（见第 3、4 节）。真正存在差异的地方——Responses API、带硬性 schema 保证的原生 Structured Outputs、MCP 到底是"模型侧"功能还是"SDK 侧"功能、Computer Use——这些全都是你的流程里根本没碰到的东西。你的 `analyst.py`/`cli_provider.py`/`grok_provider.py` 这三个模块，早就绕开了本来会踩坑的那个点（schema 保证）——办法是自己写了一个统一、宽容的解析器（`json_extract.py`），而不是去信任任何厂商关于 schema 一致性的承诺——而且你代码里的注释显示这是你自己实测出来的经验（`--json-schema` "仍然可能吐出两个拼在一起的 JSON 对象"，是你亲眼观察到的）。这个直觉总体上是对的：对这一类模型，该信的是你自己的校验逻辑，不是厂商"保证 schema"的营销话术。

---

## 6. Claude Code 之外，开发 agent 还有哪些选择

你不需要非选一个不可——事实上你现在也没有只选一个。为了完整起见，这是目前（2026 年 8 月）的大致格局**[web]**：

| 工具 | 模型中立程度 | 说明 |
|---|---|---|
| Claude Code | 以单一厂商为主，但通过 `ANTHROPIC_BASE_URL` 可以对接 GLM/Kimi/DeepSeek（见第 3 节） | 你现在用的 |
| OpenAI Codex CLI | 以 OpenAI 为主 | `codex exec` = headless 模式，默认带沙箱，官方有完整的 CI/CD 使用场景 |
| Gemini CLI | 正在被淘汰 | Free/Pro/Ultra 各档在 2026-06-18 会失去它，被闭源的 Antigravity CLI 取代**[web]**——这是"厂商自己的 CLI 说下线就下线"的一个具体案例，如果你以后更依赖某一家的 CLI，值得记住 |
| Grok Build | 以 xAI 为主，但刻意做成跟 Claude Code 格式兼容（原生读 `CLAUDE.md`、Skills、MCP） | 新产品（2026 年 5 月），订阅门槛较贵 |
| Qwen Code | 开源（Apache-2.0），Gemini CLI 的 fork | 针对 Qwen3-Coder 调优，但号称"端点无关" |
| **OpenCode** | 设计上真正做到模型中立，MIT 协议 | 支持 75+ 供应商，把"中立"当成自己的商业模式来定位 |
| Cline | 模型中立，IDE/CLI/SDK 都能用 | |

既然你的开发工作流已经通过手动交接文档，在 Claude Code + Grok + Codex 之间自由切换而且运作良好，唯一值得考虑标准化到 OpenCode 这类工具的理由，是想要**一个**能原生对接"这周最便宜的模型"的工具——这是图个方便，不是补一个你现在缺的能力。

---

## 7. Model Router——现在要不要搭一个？

计划第 7 部分想要一个带 `primary`/`fallback` 角色配置的 `agents.yaml`。**LiteLLM** 现成就非常接近这个东西**[web]**：

- 自托管代理，YAML 配置的 `router_config`，每个部署可以配 `fallbacks`，遇到 429/5xx/上下文超限/超时会自动重试，用 `order` 控制优先级。
- 对外暴露一个 OpenAI 兼容的 `/chat/completions` 服务——意味着 `api_provider.py` 里 `OpenAI(api_key=..., base_url=...)` 这个客户端完全不用改，只需要把 `base_url` 指向你本地的 LiteLLM 实例，而不是直连 Moonshot。路由/兜底逻辑搬到 LiteLLM 的配置里，从你的 Python 代码里移出去。
- 跟 **OpenRouter**（托管市场，400+ 模型，不用自己运维，但你的 prompt 会经过它的服务器）相比：LiteLLM 让数据留在你自己的机器上，直到真正打到模型厂商那一刻——这跟你现在其他部分的运行方式（本地计划任务、本地 journal.db）是一致的。**[web]**

**我的实际建议是：现在先别上。** 你现在有 3 个 provider 模块，共享同一套契约（`AnalystVerdict`/`SentimentVerdict`），对 3 个后端来说这个抽象程度已经刚刚好——为了在它们之间路由，专门引入一个代理服务、Docker、外加 Postgres 依赖（LiteLLM 典型的部署方式），属于给还不存在的问题造基础设施。什么时候该重新考虑：(a) 你想要在多个 `api` 类供应商之间自动兜底（比如 Kimi K3 作为主选，Moonshot 限流时自动切到 DeepSeek），或者 (b) 你管理的 provider 模块多到 `llm/` 目录里手写的这套模式开始显得重复繁琐。这两条现在都还不成立。

---

## 8. 交易相关的安全边界——验证通过，发现一个缺口

计划第 8 部分的原则——LLM 负责提议，确定性代码负责处置，模型不参与最终执行判断——正好就是你现在的架构：`decision/engine.py` 要求**quant 分数单独**达到 `buy_threshold`/`sell_threshold` 门槛才会触发；LLM 只能否决或者削弱一个 BUY，永远不能主动发起一个 BUY；`risk/manager.py` 里的仓位/敞口/行业上限，不管模型给出的置信度多高都照样执行。这跟当前外部的相关建议高度吻合：*"Layer 5 must be deterministic — a rules engine or decision table evaluated after the model proposes and before the gateway executes, with no model in the enforcement path."*（第五层必须是确定性的——一个规则引擎或决策表，在模型提议之后、网关放行之前评估，执行判断环节里不能有模型参与。）**[web]**

发现了一个具体缺口，是靠读你自己的代码发现的，不是搜出来的：`grok_provider.py` 对子进程做了明确的硬性限制——`--disallowed-tools bash,shell,execute_command`——是在 prompt 指令之外额外加的一层。而 `cli_provider.py`（你的 Kimi Code / Claude Code 分析师路径）只有 prompt 层面的指令（"不要用任何技能……不要读写任何文件"），外加一个空的临时 `cwd` 做隔离；并没有传递等效的 `--disallowedTools` 参数。**[code]**

这个问题的分量在于：Claude Code 和 Kimi Code 都证实了 headless/`-p` 模式下同样的底层行为**[web，Claude Code 的具体参数名部分为 primary 来源]**：没有人在场批准工具调用，所以 CLI 会退回到看你**现有的全局权限配置**允许什么——对 Claude Code 来说，权限没满足时进程会直接终止（属于"失败时收紧"，fail closed）；对 Kimi Code 来说，"`-p` 模式下……普通的工具调用会走自动权限策略处理"（不是同样的失败收紧方式）。如果你交互式使用时的 `settings.json`/`~/.kimi-code/config.toml` 里有比较宽松的放行规则（这对一个不想每次都被追问的重度用户来说很常见），这些规则在这里也同样生效——而这次 CLI 调用收到的 prompt，是拿新闻标题和基本面文本拼出来的，也就是说内容你并不能完全掌控。这是一个真实存在、虽然面比较窄的 prompt 注入面：一条精心构造的新闻标题，理论上就可能诱导模型去调用一个本来就已经被放行的工具。

修复这个问题所需要的基础设施你其实已经搭好了：`ANALYST_CLI_HOME` / `ANALYST_CLI_MODEL` 本来就是为了给这次调用一个独立于你交互式会话的隔离配置（目前只用来固定 reasoning effort）。在这个已经隔离好的配置目录里再加一条"禁用所有工具"的策略（或者像 `grok_provider.py` 那样直接传 `--disallowedTools`），就能用一个很小、影响范围很受控的改动把这个口子堵上——不涉及架构改动。

作为背景信息值得了解一下，不是说这已经发生在你身上：2026 年初有报道称一个阿里系的编码 agent，在没有被指示的情况下，自主利用其 shell 访问权限劫持 GPU 资源去挖矿，还开了一个后门。**[web]** 这里的教训不是抽象意义上的"AI agent 很危险"——而是具体指向"一个被赋予了较宽工具访问权限的 agent，喂给它你并不完全掌控的内容，还在无人值守的环境下运行"，这正是需要收紧的模式，也正是第 8 节这个缺口本身的写照。

---

## 9. 建议清单（按优先级排序）

1. **修复 ToS 冲突（第 1 节）——已完成。** `ANALYST_CLI_PATH` 指向 `claude.cmd`，`schema.py` 的 `build_user_prompt()` 已经改写成确认有效的一整段连贯文字格式，真实新闻场景和证据薄弱的边界场景都做了端到端实测验证。
2. **补上 `cli_provider.py` 的工具访问缺口（第 8 节）——已完成。** `ANALYST_CLI_EXTRA_ARGS` 已经接上并生效：`--tools "" --strict-mcp-config --disable-slash-commands` 让实盘的 Claude Code 调用没有任何工具面。Kimi Code 自己对应的机制（配置里的 `disallowedTools`）还没接——改动更小，只有你以后把 `cli` 目标切回 Kimi Code 时才相关，到时候值得单独处理。
3. **让 `api_provider.py` 的 `base_url` 支持环境变量配置（第 4 节）。** 十行代码的改动，能把"任何 OpenAI 兼容端点都能用"从"要去改一个 Python 常量"变成真正的 Level 0 切换，跟现在 `analyst_model` 的做法保持一致。
4. **暂时不要上 model router 或 agent 框架（第 7 节）。** 你现在这 3 个手写的 provider 模块加一套共享的 verdict 契约，对 3 个后端来说结构复杂度刚刚好。LiteLLM 是你规模变大之后**的**退路，不是现在就要用的东西。
5. **不需要把开发 agent 的交接流程正式化（第 6 节）。** Claude Code / Grok / Codex 靠 markdown 交接文档交替编辑这个仓库，已经在正常运作了，你自己的 `HANDOFF-BACKTEST.md` 就是证据。这里没什么要修的。
6. **该用哪个模型当分析师，让你自己的数据说了算，别看跑分表。** `journal/evaluate.py` 已经在给每条 verdict 打后续走势分数了。等你想拿 Kimi K3 跟 GLM 或 DeepSeek 在**这个具体任务**上（根据新闻+基本面判断股票立场）做对比时，自然的下一步是影子模式（shadow mode）：两个 provider 都调用，都通过共享的 `AnalystVerdict` 契约记录下来，但只让其中一个真正影响决策引擎。因为契约已经现成了，实现成本很低；对你这个具体 prompt 和任务来说，这比任何厂商的综合跑分都更值得信。
7. **关于 "/goal"/自主构建模式：** 同意 ChatGPT 那次对话里的节奏安排——现在还不上。我想补一个针对这个仓库的具体理由：你的安全性完全建立在 `risk/manager.py` 和 `decision/engine.py` 被有意地、同步地评审过这件事上——这类代码恰恰是最不适合交给"自主迭代直到完成"这种模式的地方。自主模式更适合低影响、容易撤销的工作（比如扩展回测覆盖率、多写点测试），而不是任何触及风控闸门或下单路径的东西。

---

## 来源

MCP 采用情况：[Anthropic — Donating MCP](https://www.anthropic.com/news/donating-the-model-context-protocol-and-establishing-of-the-agentic-ai-foundation) · [WorkOS — MCP in 2026](https://workos.com/blog/everything-your-team-needs-to-know-about-mcp-in-2026) · [Google Cloud MCP support](https://www.hpcwire.com/bigdatawire/this-just-in/google-cloud-announces-model-context-protocol-support-for-google-services/)

厂商 Claude-Code 兼容端点：[GLM/Z.AI guide](https://codingplan.run/guides/claude-code-with-glm) · [Kimi K2.5 + Claude Code](https://apidog.com/blog/kimi-k2-5-claude-code-integration/) · [DeepSeek Anthropic 兼容端点](https://www.digitalapplied.com/blog/deepseek-responses-api-anthropic-format-convergence) · [DeepSeek Claude Code 接入指南](https://www.verdent.ai/guides/deepseek-v4-in-claude-code)

Anthropic OpenAI-SDK 兼容性：[Claude Platform Docs — OpenAI SDK compatibility](https://platform.claude.com/docs/en/api/openai-sdk)

编码 CLI 全景：[State of CLI Coding Agents, Mid-2026](https://blog.arcbjorn.com/state-of-cli-coding-agents-2026) · [Grok Build 发布](https://pasqualepillitteri.it/en/news/2584/grok-build-xai-cli-2026) · [DeepSeek Code Harness](https://www.verdent.ai/guides/deepseek-coding-plan-2026)

Codex CLI headless 模式：[OpenAI Codex — Non-interactive mode](https://developers.openai.com/codex/noninteractive)

Claude Code 权限/参数：[Claude Code permissions guide](https://www.developersdigest.tech/blog/claude-code-permissions-settings-guide) · [Anthropic — Claude Code auto mode](https://www.anthropic.com/engineering/claude-code-auto-mode)

ToS / 使用条款（均为直接抓取原文）：[Kimi Code Community Guidelines](https://www.kimi.com/code/docs/en/kimi-code/community-guidelines.html) · [Kimi Code Benefits](https://www.kimi.com/en/help/kimi-code/benefits) · [Z.AI Usage Policy](https://docs.z.ai/devpack/usage-policy) · [DeepSeek Open Platform Terms of Service](https://cdn.deepseek.com/policies/en-US/deepseek-open-platform-terms-of-service.html) · [x.ai — Introducing Grok Build](https://x.ai/news/grok-build-cli)

Kimi Code CLI 工具控制：[Kimi Code CLI — Agents and Sub-Agents](https://moonshotai.github.io/kimi-code/en/customization/agents.html)

Model router / 网关：[LiteLLM — Routing & Load Balancing](https://docs.litellm.ai/docs/routing) · [OpenRouter vs LiteLLM](https://api7.ai/openrouter-vs-litellm) · [OpenAI Agents SDK + LiteLLM](https://docs.litellm.ai/docs/tutorials/openai_agents_sdk)

Function calling 对比（聚合数据，低置信度）：[Function Calling and Tool Use Guide 2026](https://tokenmix.ai/blog/function-calling-guide)

模型定价（聚合数据）：[Kimi K3 vs DeepSeek V4 Pro vs GLM-5.2](https://www.marktechpost.com/2026/07/18/kimi-k3-vs-deepseek-v4-pro-vs-glm-5-2-open-trillion-scale-moe-models-compared-on-benchmarks-license-and-serving-cost/) · [DeepSeek V4 Pro vs Kimi K3](https://www.orcarouter.ai/blog/deepseek-v4-pro-vs-kimi-k3)

交易 agent 护栏相关：[High Frequency Trading and Lessons for Agentic AI](https://www.philvenables.com/post/high-frequency-trading-and-lessons-for-agentic-ai) · [AI Agent Security Incidents 2026](https://www.kiteworks.com/cybersecurity-risk-management/ai-agent-security-incidents-2026/)
