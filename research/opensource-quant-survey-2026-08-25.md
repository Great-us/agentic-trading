# 开源量化交易模型与持续交易策略全景调研 — 2026-08-25

**这份研究要回答的问题：** 开源世界里现在有哪些量化交易模型、策略框架和持续运行的自动交易项目？各自的定位、热度、健康度如何？版图长什么样？

**按用户要求，本文只做"全面地找"——客观描述各项目是什么、多活跃、能干什么。哪些适合本仓库（Agentic Trading），是下一步单独做的事，本文不含任何针对本仓库的取舍建议。**

**数据与置信度标注：**

- **[gh]** = GitHub API 硬数据（stars / forks / 最近推送 / license），全部采集于 **2026-08-25**。
- **[primary]** = 直接抓取该项目 README/官方文档原文验证过。
- **[web]** = 搜索引擎聚合或领域常识性描述——方向可信，未逐条核实。
- stars 数字变化很快，一律按 2026-08-25 快照理解。

**发现方法（四路并进）：** ① 以 `wilsonfreitas/awesome-quant`（29,163★ [gh]）和 `paperswithbacktest/awesome-systematic-trading`（13,952★ [gh]）两份索引清单为底爬取全量条目；② GitHub 多路 topic/关键词搜索按 stars 排序；③ Exa 网页搜索补 GitHub 外的论文代码与新项目；④ 中文生态单独一路。对入选项目统一用 `gh repo view` 拉硬数据，共覆盖约 **80 个核心项目 + 附录长尾若干**。

---

## TL;DR

1. **LLM/Agent 自动交易是当前增长最快的赛道。** `TauricResearch/TradingAgents` 从 2024 年底的论文代码涨到 **100,010★**（2026-07 仍在发版 v0.3.x）[gh][primary]，是全版图最大的单体项目之一。围绕它已形成生态：中文增强版（31.4k★）、A股适配版（3.1k★）、以及一批同构竞品（Vibe-Trading 31.7k★、ai-hedge-fund 63.0k★、OpenAlice 6.7k★ 等）。注意：这一批里多数是**分析/研究型 agent，不直接下单**。
2. **回测框架完成代际更替。** 老一代 backtrader（23.0k★，2024-08 后停更）和 zipline（原 Quantopian 遗产，20.1k★ 已死）让位给 nautilus_trader（27.8k★ Rust 内核，当天仍在提交）、vectorbt（8.8k★ 向量化批量回测）、backtesting.py（8.9k★）、Lean（21.3k★ C#，QuantConnect 云生态）[gh]。
3. **"持续运行的自动交易机器人"绝大多数是加密货币向。** freqtrade（53.6k★）、hummingbot（19.6k★ 做市向）、Jesse（8.4k★）、OctoBot（6.5k★）全是 crypto。**美股向的持续运行开源 bot 是少数派**：lumibot（1.98k★，同一套代码回测+实盘，支持 Alpaca/IBKR/Schwab）、thetagang（2.7k★，IBKR 期权）、StockSharp（10.6k★，C# 多资产）[gh][web]。
4. **ML/DL 量化模型的"事实标准"是 Microsoft Qlib。** 47.9k★ MIT，从数据处理到模型训练到回测的全链路，model zoo 收录 LightGBM/ALSTM/HIST/TRA/MASTER 等一众 SOTA 论文实现，并已接入 RD-Agent（14.3k★）做 LLM 自动化因子挖掘 [gh][primary]。强化学习方向的代表是 FinRL（16.1k★）。
5. **中文生态自成一体、体量巨大。** vn.py（44.8k★）、abu（18.2k★）、QUANTAXIS（11.0k★）、easytrader（10.1k★）、myhhub/stock（14.1k★）、czsc 缠论（5.9k★）等，覆盖国内期货 CTP 到 A 股全自动交易 [gh]。
6. **值得警惕的一类：** 高星 ≠ 可用。gekko（10.2k★）已 archived，Stock-Prediction-Models（9.5k★）archived，mlfinlab（4.9k★）核心代码转商业闭源，blankly（2.5k★）一年半没动 [gh]。选型时"最近推送时间"比 stars 重要。

---

## 版图总览矩阵

状态判定：**活跃** = 近 3 个月内有推送；**减速** = 3–18 个月无推送；**停更/archived** = 更久或官方归档。

| 项目 | 类别 | Stars | 语言 | License | 资产类别 | 最近推送 | 状态 |
|---|---|---:|---|---|---|---|---|
| TauricResearch/TradingAgents | LLM Agent | 100,010 | Python | Apache-2.0 | 美股(可扩展) | 2026-07 | 活跃 |
| OpenBB-finance/OpenBB | 数据层 | 72,269 | Python | 自定义 | 全品类 | 2026-07 | 活跃 |
| ZhuLinsen/daily_stock_analysis | LLM 分析 | 63,836 | Python | — | A股/美股 | 2026-08 | 活跃 |
| virattt/ai-hedge-fund | LLM Agent | 63,039 | Python | — | 美股 | 2026-08 | 活跃 |
| freqtrade/freqtrade | 交易机器人 | 53,628 | Python | GPLv3 | 加密货币 | 2026-08 | 活跃 |
| microsoft/qlib | ML/DL 平台 | 47,932 | Python | MIT | 美股/A股 | 2026-07 | 活跃 |
| vnpy/vnpy | 中文生态框架 | 44,762 | Python | MIT | 国内期货/股票 | 2026-08 | 活跃 |
| ccxt/ccxt | 数据层/交易所API | 43,743 | JS/PY/PHP | 自定义 | 加密货币 | 2026-08 | 活跃 |
| HKUDS/Vibe-Trading | LLM Agent | 31,689 | Python | — | A股/美股/Crypto | 2026-08 | 活跃 |
| hsliuping/TradingAgents-CN | LLM Agent | 31,367 | Python | — | A股/港美 | 2026-07 | 活跃 |
| Fincept-Corporation/FinceptTerminal | 研究终端 | 30,645 | Python | — | 全品类 | 2026-08 | 活跃 |
| nautechsystems/nautilus_trader | 回测+实盘平台 | 27,764 | Python/Rust | LGPL-3.0 | 多资产 | 2026-08 | 活跃 |
| yfinance (ranaroussi) | 数据层 | 25,071 | Python | Apache-2.0 | 全球股票 | 2026-08 | 活跃 |
| mementum/backtrader | 回测框架 | 22,960 | Python | GPLv3 | 多资产 | 2024-08 | 停更(fork活跃) |
| QuantConnect/Lean | 回测+实盘引擎 | 21,349 | C#/Python | Apache-2.0 | 多资产 | 2026-08 | 活跃 |
| AI4Finance-Foundation/FinGPT | 金融LLM | 21,148 | Python | MIT | — | 2026-08 | 活跃 |
| stefan-jansen/machine-learning-for-trading | 学习/复现库 | 20,636 | Python | — | — | 2026-08 | 活跃 |
| quantopian/zipline | 回测框架(遗产) | 20,064 | Python | Apache-2.0 | 美股 | 2020 | 死(reloaded接力) |
| hummingbot/hummingbot | 做市机器人 | 19,601 | Python | Apache-2.0 | 加密货币 | 2026-08 | 活跃 |
| bbfamily/abu | 中文生态 | 18,228 | Python | GPL | A股/期货/Crypto | 2026-01 | 减速 |
| AI4Finance-Foundation/FinRL | 强化学习交易 | 16,099 | Python | MIT | 美股为主 | 2026-07 | 活跃 |
| waditu/tushare | 数据层(A股) | 15,363 | Python | — | A股 | 2024-03 | 减速(转商业API) |
| myhhub/stock | A股自动交易 | 14,130 | Python | — | A股 | 2026-04 | 活跃 |
| microsoft/RD-Agent | 自动因子挖掘 | 14,334 | Python | MIT | — | 2026-08 | 活跃 |
| akfamily/akshare | 数据层(A股) | 22,232 | Python | MIT | A股/期货 | 2026-08 | 活跃 |
| UFund-Me/Qbot | AI量化平台 | 18,388 | Python | — | A股/Crypto | 2026-03 | 减速 |
| je-suis-tm/quant-trading | 策略复现集 | 10,621 | Python | MIT | 多资产 | 2026-06 | 活跃 |
| StockSharp/StockSharp | 交易平台(C#) | 10,637 | C# | 自定义 | 股票/外汇/Crypto | 2026-08 | 活跃 |
| shidenggui/easytrader | 券商自动化接口 | 10,089 | Python | MIT | A股 | 2026-02 | 活跃 |
| askmike/gekko | 交易机器人 | 10,186 | JS | MIT | Crypto | 2020-02 | archived |
| kernc/backtesting.py | 回测框架 | 8,883 | Python | — | 通用 | 2026-08 | 活跃 |
| polakowo/vectorbt | 回测框架 | 8,828 | Python | 自定义 | 通用 | 2026-08 | 活跃 |
| jesse-ai/jesse | 交易机器人 | 8,377 | Python | MIT | Crypto | 2026-08 | 活跃 |
| ranaroussi/quantstats | 绩效分析 | 7,586 | Python | MIT | 通用 | 2026-07 | 活跃 |
| AI4Finance-Foundation/FinRobot | LLM Agent平台 | 7,858 | Python | Apache-2.0 | 美股 | 2026-08 | 活跃 |
| lballabio/QuantLib | 定价库(C++) | 7,545 | C++ | BSD | 衍生品 | 2026-08 | 活跃 |
| tensortrade-org/tensortrade | RL交易框架 | 7,060 | Python | Apache-2.0 | 通用 | 2026-02 | 减速 |
| ricequant/rqalpha | 回测框架(A股) | 6,720 | Python | Apache-2.0 | A股/期货 | 2026-08 | 活跃 |
| TraderAlice/OpenAlice | LLM Agent | 6,699 | Python | — | 多资产 | 2026-08 | 活跃 |
| quantopian/pyfolio | 绩效分析 | 6,406 | Python | Apache-2.0 | 通用 | 2023-12 | 停更(reloaded接力) |
| Drakkar-Software/OctoBot | 交易机器人 | 6,462 | Python | GPLv3 | Crypto | 2026-08 | 活跃 |
| wondertrader/wondertrader | 中文C++框架 | 6,297 | C++/Python | Apache-2.0 | 国内期货/股票 | 2025-09 | 减速 |
| waditu/czsc | 缠论工具箱 | 5,917 | Python | — | A股/期货 | 2026-08 | 活跃 |
| robertmartin8/PyPortfolioOpt | 组合优化 | 5,982 | Python | MIT | 通用 | 2026-07 | 活跃 |
| Superalgos/Superalgos | 交易机器人 | 5,622 | JS | — | Crypto | 2026-08 | 活跃 |
| hudson-and-thames/mlfinlab | AFML实现 | 4,912 | Python | 自定义 | 通用 | 2023-10 | 核心转商业 |
| dcajasn/Riskfolio-Lib | 组合优化 | 4,456 | Python | 自定义 | 通用 | 2026-08 | 活跃 |
| nkaz001/hftbacktest | HFT回测 | 4,478 | Py/Rust | MIT | Crypto/期货 | 2025-12 | 活跃 |
| zvtvz/zvt | 中文模块化框架 | 4,284 | Python | MIT | A股 | 2026-07 | 活跃 |
| tradytics/eiten | 组合策略集 | 3,289 | Python | — | 美股 | 2022-07 | 死 |
| pst-group/pysystemtrade | 系统化交易 | 3,481 | Python | — | 期货为主 | 2026-07 | 活跃 |
| fasiondog/hikyuu | C++量化框架 | 3,468 | C++/Python | MIT | A股 | 2026-08 | 活跃 |
| mhallsmoore/qstrader | 回测框架 | 3,448 | Python | AGPL? | 美股 | 2024-06 | 停更 |
| edtechre/pybroker | ML回测框架 | 3,515 | Python | Apache-2.0 | 美股/通用 | 2026-08 | 活跃 |
| charliedream1/ai_quant_trade | A股学习/实盘 | 6,368 | Python | — | A股 | 2026-08 | 活跃 |
| brndnmtthws/thetagang | 期权收益bot | 2,712 | Python | — | 美股期权(IBKR) | 2026-08 | 活跃 |
| blankly-finance/blankly | 机器人框架 | 2,465 | Python | — | Crypto/股票 | 2024-12 | 减速 |
| skfolio/skfolio | 组合优化 | 2,240 | Python | BSD | 通用 | 2026-08 | 活跃 |
| barter-rs/barter-rs | Rust交易框架 | 2,243 | Rust | — | Crypto | 2026-08 | 活跃 |
| simonlin1212/TradingAgents-astock | LLM Agent(A股) | 3,072 | Python | — | A股 | 2026-08 | 活跃 |
| OpenByteInc/QuantDinger | AI量化平台 | 11,080 | Python | — | Crypto/股票/外汇 | 2026-08 | 活跃 |
| chrisworsey55/atlas-gic | LLM Agent | 2,086 | Python | — | 多资产 | 2026-05 | 减速 |
| Lumiwealth/lumibot | 实盘机器人框架 | 1,980 | Python | — | 股票/期权/Crypto等 | 2026-08 | 活跃 |
| asavinov/intelligent-trading-bot | ML信号bot | 1,856 | Python | MIT | Crypto | 2026-08 | 活跃 |
| pipiku915/FinMem-LLM-StockTrading | LLM Agent | 946 | Python | MIT | 美股 | 2024-08 | 停更 |
| ta4j/ta4j | 技术指标库(Java) | 2,480 | Java | MIT | 通用 | 2026-08 | 活跃 |
| dragon1086/prism-insight | LLM分析+自动交易 | 724 | Python | — | 韩股/美股 | 2026-08 | 活跃 |

---

## 第 1 章 ML/DL 收益预测模型

### 1.1 Microsoft Qlib — AI 量化研究平台的事实标准

`microsoft/qlib` — **47,932★** / 7,599 forks / MIT / Python / 最近推送 2026-07-23 [gh]

- 定位：AI 导向的量化投资全链路平台——数据处理 → 特征/因子 → 模型训练 → 回测 → 组合 → 执行分析，一条龙。[primary]
- 支持三类建模范式：监督学习（挖非线性规律）、市场动态建模（concept drift 自适应）、强化学习。[primary]
- **Model zoo 是它最大的资产**：收录了大量 SOTA 论文的开源实现——LightGBM、ALSTM、GATs、HIST、TRA、KRNN、Sandwich、MASTER 等，配 Alpha158/Alpha360 两套标准因子库。[primary]
- 2024-08 起接入 `microsoft/RD-Agent`（14,334★，2026-08 仍活跃）：用 LLM agent 自动化"提出因子假设→写代码→回测→迭代"的研发循环，对应论文 R&D-Agent-Quant (arXiv 2505.15155)。[primary]
- 社区健康度：微软官方维护，文档完整，学术圈引用最多；但主仓推送节奏近月放缓（最近一次 2026-07）。[gh]

### 1.2 FinRL / FinRL-Meta — 深度强化学习交易的旗手

`AI4Finance-Foundation/FinRL` — **16,099★** / MIT / 最近推送 2026-07-13 [gh]

- 定位：把 DRL（DQN/PPO/SAC/A2C 等，基于 stable-baselines/gym 风格接口）应用到交易的标准开源框架，NeurIPS 2020 出道。[gh][web]
- `FinRL-Meta`（1,926★）提供市场环境库：美股、A股、crypto、高频订单流等仿真环境 [gh]。
- 定位偏学术研究与教学，内置策略直接实盘使用的人少；工程化程度低于 Qlib。[web]
- 同基金会还有 FinGPT（见第 2 章）与 FinRobot。

### 1.3 其他值得知道的

- **tensortrade**（7,060★，Apache-2.0，2026-02 后减速）— 可组合组件式 RL 交易环境（exchange/reward/action scheme 可插拔），曾是最流行的 RL 交易 gym 之一。[gh][web]
- **DeepTrader**（`CMACH508/DeepTrader`，126★）— 论文《DeepTrader》官方实现：资产打分网络 + 风险管理网络联合优化；社区极小，属论文复现性质。[gh]
- **mlfinlab**（hudson-and-thames，4,912★，2023-10 停更）— 《Advances in Financial ML》(López de Prado) 的标准实现库（fractal 数据结构、meta-labeling、特征工程）；**公司已转向商业授权，开源版冻结**。[gh][web]
- **Stock-Prediction-Models**（9,480★，archived 2023）— 一大包 LSTM/GAN/Transformer 预测脚本合集，教学参考用，勿用于生产。[gh]
- **aurumq-rl**（yupoet）— A股 RL 选股框架，因子输入含 alpha101+主力资金+北向+游资席位，ONNX CPU 推理；小众但设计完整。[web]
- **je-suis-tm/quant-trading**（10,621★，MIT，活跃）— 经典策略的纯 Python 复现合集：VIX 计算、形态识别、CTA、蒙特卡洛、期权跨式、伦敦突破、Heikin-Ashi、配对交易、Dual Thrust 等；学习价值高。[gh]

---

## 第 2 章 LLM / Agent 自动交易

这是与本仓库（quant 分数 + LLM 分析师）最同源的赛道，也是 2025–2026 变化最快的一章。

### 2.1 TradingAgents — 赛道标杆

`TauricResearch/TradingAgents` — **100,010★** / 19,332 forks / Apache-2.0 / 最近推送 2026-07-18 / 论文 arXiv 2412.20138 [gh][primary]

- 架构：多角色 agent 公司模拟——分析师团队（基本面/情绪/新闻/技术）→ 多空研究员辩论 → 交易员 → 风险讨论组 → 组合经理裁决。[primary][web]
- 迭代非常勤：v0.2.x（2026 年上半年）陆续加入 LangGraph checkpoint、结构化输出、多供应商 LLM 支持（DeepSeek/Qwen/GLM/Kimi/Groq/Mistral/Bedrock/Ollama/OpenAI 兼容端点）；**v0.3.0（2026-06）加了数据访问契约、FRED 与 Polymarket 数据源；v0.3.1（2026-07）修了 Alpha Vantage 前视偏差过滤、graph-router 崩溃安全等正确性问题**。[primary]
- 衍生：**Trading-R1**（推理型交易模型技术报告，arXiv 2509.11420；`TauricResearch/Trading-R1` 472★）走 RL 训练路线。[primary][gh]
- 注意：定位是**决策研究框架**，产出买卖建议与论证链，不带券商执行层。

### 2.2 中文世界的高星衍生与竞品

- **TradingAgents-CN**（hsliuping，31,367★，2026-07 活跃）— TradingAgents 中文增强版：接 DeepSeek/Qwen/GLM 等国产模型与 A股数据源。[gh][web]
- **TradingAgents-astock**（simonlin1212，3,072★，活跃）— 深度改造适配大A：龙虎榜/游资/解禁数据源，7 位分析师按 A 股规则辩论。[gh]
- **Vibe-Trading**（HKUDS，31,689★，2026-08-25 当天仍推送，极其活跃)— 自然语言多 agent 金融研究 agent：29 个 swarm preset、70 skills、28 工具；**7 个回测引擎覆盖 A股/美股/crypto/期货/外汇/期权**，跨市场 CompositeEngine 共享资金池；数据层 tushare/okx/yfinance/akshare/ccxt 五源自動降级；17 工具 MCP server；附带券商导出账单的行为诊断。[primary]
- **daily_stock_analysis**（ZhuLinsen，**63,836★**，当天活跃）— LLM 驱动的多市场个股智能分析系统：多源行情+实时新闻+决策看板+自动推送，支持零成本定时跑。**只做分析与推送，不做执行**——高星说明"AI 盯盘简报"这个形态需求巨大。[gh][web]

### 2.3 其他代表性项目

- **ai-hedge-fund**（virattt，**63,039★**，2026-08 活跃）— 用多个"著名投资人人格"agent（巴菲特/芒格风格等）对同一批股票各自打分的模拟对冲基金，教学演示属性强，社区巨大；带 Web demo。[gh][web]
- **FinRobot**（AI4Finance-Foundation，7,858★，Apache-2.0，活跃）— 金融 AI agent 平台：市场预测/文档分析/交易策略等多 agent 工作流，插件化设计。[gh][web]
- **FinGPT**（AI4Finance-Foundation，21,148★，MIT，活跃）— 开源金融 LLM 系列（金融情感分析、FinGPT-Forecaster 等），是"给交易系统供模型"而不是"交易系统"。[gh][web]
- **QuantDinger**（OpenByteInc，11,080★，活跃）— 较新的 AI 量化平台，宣称 crypto/股票/外汇全覆盖：回测+实盘+行情+multi-agent 研究一体。[gh][web]
- **OpenAlice**（TraderAlice，6,699★，活跃）— "一个人的华尔街"：覆盖股票/crypto/商品/外汇/宏观的研究→建仓→持仓管理→退出全流程 AI agent。[gh][web]
- **atlas-gic**（2,086★）— "自我改进的 AI 交易 agent"，Karpathy 式 autoresearch 路线，概念性强、验证弱。[gh][web]
- **FinMem**（pipiku915，946★，2024-08 停更）— 论文《FinMem》官方实现：分层记忆 LLM 交易员，曾是该方向引用最多的开源实现之一；已不再维护。[gh]
- **PRISM-Insight**（dragon1086，724★，活跃）— 13 个专业 agent 的 AI 个股分析 + 通过韩国 KIS API **真实自动交易**，覆盖韩股与美股；少见的研究+执行一体小项目。[gh][web]
- **mnemox-ai/tradememory-protocol** — 给 AI 交易 agent 加"决策审计链 + 结果加权记忆"的 MCP 工具集（SHA-256 链 + RFC3161 时间戳锚定）；体量小，思路对本类系统有参考价值。[web]
- **FinceptTerminal**（30,645★，活跃）— 现代化投研终端（数据分析/研报/宏观），非执行型，但常被误当交易系统。[gh]

**本章小结：** LLM agent 项目分两类——**研究决策型**（TradingAgents/Vibe-Trading/ai-hedge-fund，绝大多数）和**带执行层的少数派**（PRISM-Insight、QuantDinger、OpenAlice 宣称可接实盘）。赛道整体年轻（多为 2025 后创立），回测严谨性普遍不如传统框架，"前视偏差"这类问题直到 v0.3.x 才被头部项目认真处理 [primary]。

---

## 第 3 章 回测与研究框架

### 3.1 现役四大件

- **nautilus_trader**（nautechsystems，**27,764★** / LGPL-3.0 / Rust 内核 + Python API / 2026-08-25 当天仍在推送）— 高性能事件驱动平台：同一套策略代码跑回测与实盘，多资产多经纪商适配器；是当前工程标准最高、迭代最猛的开源交易内核。[gh][web]
- **QuantConnect Lean**（**21,349★** / Apache-2.0 / C# 内核 Python/C# 双语言 / 活跃)— 开源算法交易引擎，与 QuantConnect 云平台配套：数据、回测、实盘、参数优化全托管生态，本地也能裸跑。[gh][web]
- **vectorbt**（polakowo，8,828★ / 自定义 license / 活跃)— NumPy/Numba 全向量化：一次算几千个参数组合的回测，研究筛选利器；注意商业版 vectorbt PRO 承载了大部分新特性，开源版更新偏慢。[gh][web]
- **backtesting.py**（kernc，8,883★ / 活跃）— 轻量单标的向量化回测器，API 极简，图表友好；重获维护。[gh]

### 3.2 上一代与遗产

- **backtrader**（mementum，22,960★ / GPLv3 / **2024-08 后停更**）— 曾经的社区之王，中文资料最多的事件驱动回测框架。官方停更后有活跃接棒 fork `cloudQuant/backtrader`（加 MCP/AI 工具链）。存量用户巨大，新项目慎选。[gh]
- **zipline**（quantopian，20,064★）— Quantopian 2020 关站后的遗产；继任者 **zipline-reloaded**（stefan-jansen，1,927★，维护中）。pipeline API（横截面因子流水线）仍是它独有的能力；alpacahq 曾出 pylivetrader/pipeline-live 做其 zipline 兼容实盘层（已陈旧）。[gh][web]
- **qstrader**（mhallsmoore，3,448★ / 2024-06 停更）— QuantStart 的教学级事件驱动回测器，代码干净适合读。[gh]

### 3.3 特色选手

- **PyBroker**（edtechre，3,515★ / Apache-2.0 / 活跃) — 以 ML 为一等公民的回测框架：内嵌 walk-forward 训练/预测管线、光速缓存特征。[gh][web]
- **pysystemtrade**（已迁至 pst-group 组织，3,481★ / 2026-07 活跃）— Robert Carver《Systematic Trading》的完整开源实现：期货趋势跟踪为主的多资产系统化交易，从回测到实盘都有，是"规则型系统化交易"最完整的参考实现之一。[gh][web]
- **bt**（pmorissette，2,965★ / 活跃）— Algo 树组合回测，再平衡/权重类策略表达极简。[gh]
- **rqalpha**（ricequant，6,720★ / Apache-2.0 / 活跃）— Ricequant 开源的 A股/期货回测框架，mod 插件体系。[gh]
- **fastquant**（1,754★，2023 后停滞）—"3 行代码回测"教学定位。[gh]
- **Hikyuu**（fasiondog，3,468★ / C++ 内核 / 活跃）— 国产高性能系统组件化框架，交易系统可复用组合。[gh]
- **zvt**（zvtvz，4,284★ / MIT / 活跃）— 统一 schema 的模块化量化记录/选股/回测/实时框架（A股为主）。[gh]
- **WonderTrader**（6,297★ / C++ / 2025-09 减速）— 国内高频级一站式研发交易框架（CTP 系）。[gh]
- **aat**（timkpaine/AsyncAlgoTrading，829★）— 异步事件驱动引擎，可选 C++ 加速。[gh]
- **Rust 阵营**：barter-rs（2,243★，活跃，事件驱动 live+backtest）、rust_bt、LDEST…（lfest-rs 模拟永续合约交易所）；**Julia**：Fastback.jl、Strategems.jl。[gh][web]
- **StrateQueue**（211★）— 把各大回测引擎写好的策略零改动翻译到实盘/paper 经纪商的"桥"，思路有趣、体量尚小。[gh][web]

---

## 第 4 章 持续运行的自动交易机器人

> 用户问的"持续交易策略"主要对应这一章。关键事实：**这一类的头部项目几乎全是 crypto 向**。

### 4.1 加密货币向（体量最大）

- **freqtrade**（**53,628★** / GPLv3 / Python / 当天活跃）— 开源 crypto bot 的代名词：策略即 Python 类、超参优化（hyperopt）、Telegram 控制、dry-run 模拟盘；**FreqAI 扩展**把自适应机器学习（滚动训练预测）做成了内置能力。[gh][web]
- **hummingbot**（19,601★ / Apache-2.0 / 活跃）— 做市/套利方向：100+ 交易所连接器（CEX+DEX），V2 策略框架（控制器/执行器架构），是"开源做市"唯一成规模的选项。[gh][web]
- **Jesse**（jesse-ai，8,377★ / MIT / 活跃）— 研究→回测→实盘一体的 crypto 框架，API 设计干净，多时间框架策略支持好；实盘插件部分功能商业授权。[gh][web]
- **OctoBot**（6,462★ / GPLv3 / 活跃）— 带 Web 界面的 crypto bot：AI/网格/DCA/TradingView 信号策略，15+ 交易所。[gh][web]
- **Superalgos**（5,622★ / JS / 活跃）— 可视化节点图编程的交易平台，社区协作式策略市场，学习曲线陡。[gh][web]
- **gekko**（10,186★ / **archived 2020**）— 曾经最有名的 node.js crypto bot，已死，仅作历史注脚。[gh]

### 4.2 美股/多资产向（少数派）

- **lumibot**（Lumiwealth，1,980★ / 2026-08 活跃）— 同一套代码回测与实盘：股票/期权/crypto/期货/外汇，broker 支持 **Alpaca、Interactive Brokers、Tradier、Schwab**；带 AI agent 回测扩展。[primary][gh]
- **thetagang**（2,712★ / 活跃）— IBKR 期权"收租"bot（卖权吃 theta 的 wheel 类策略自动化）。[gh][web]
- **StockSharp**（10,637★ / C# / 活跃）— 俄罗斯系老牌交易平台：股票/期货/外汇/期权/crypto 全都接，GUI+SDK，连接器数量恐怖；文档对英文用户一般。[gh][web]
- **intelligent-trading-bot**（1,856★ / MIT / 活跃）— ML 特征工程生成信号 + Telegram 推送的轻量 bot，介于分析与执行之间。[gh][web]
- **blankly**（2,465★ / 2024-12 后减速）— 曾立志做"write once, run on any broker"（Alpaca/Binance/Coinbase...），势头已断。[gh]
- **Investing Algorithm Framework**、**QTradeX SDK**、**the0**（容器化 bot 执行引擎，多语言策略）等中小框架见附录。[web]

### 4.3 中文世界的"全自动交易"

- **myhhub/stock**（14,130★ / 2026-04 活跃）— A股全自动股票系统：数据采集→指标计算→形态识别→综合选股→验证回测→**自动交易**，PC/移动端支持，闭环完整度高。[gh][web]
- **easytrader**（shidenggui，10,089★ / MIT / 维护中）— 对接国内券商（同花顺客户端等）的下单自动化接口库，是无数国内 bot 的地基；本身不含策略。[gh][web]
- **UFund-Me/Qbot**（18,388★ / 2026-03 减速）— AI 自动量化机器人（完全本地部署）：策略回测+实盘对接+Web 界面一体。[gh][web]
- **charliedream1/ai_quant_trade**（6,368★ / 活跃）— "股票AI操盘手"一站式资源库：传统策略/ML/DL/RL/图网络/高频/C++部署/聚宽实例，学习地图价值大。[gh][web]

---

## 第 5 章 做市 / 高频 / 低延迟

开源世界做 HFT 是小众但真实存在的赛道：

- **hftbacktest**（nkaz001，4,478★ / MIT / Py+Numba/Rust / 2025-12 活跃）— 目前最重要的开源 HFT 回测器：用全量 tick 与订单簿数据建模**限价单排队位置、延迟**，专为做市/HFT 策略研究设计；crypto 与期货数据生态完善。[gh][web]
- **godzilla community**（C++/Python）— crypto 低延迟做市与资金费率套利开源框架。[web]
- **TradeFrame**（rburkholder，C++17）— 期权自动化交易测试框架：DTN IQ 实时数据 + IBKR TWS 执行，内置希腊字母/IV 库。[web]
- **PandoraTrader**（pegasusTrader，C++）— 基于 CTP 的国内高频交易平台。[web]
- **orderbook**（Go/WASM）— 整数精确定价的限价簿撮合引擎，含微观结构研究 harness（OFI/Kyle's lambda 对拍模拟器真值）。[web]
- **NexusFix**（C++23 FIX 引擎，零拷贝+SIMD）、**FlashFunk**（Rust 高性能运行时）、**openlimits**（Rust 多交易所 API，半停滞）——基建层项目。[web][gh]
- 观察结论：真正生产级 HFT 开源极少（延迟敏感的核心私域性太强），公开项目的价值主要在**回测真实性**（排队/延迟建模）与撮合/协议基建。

---

## 第 6 章 组合构建与风险管理

- **PyPortfolioOpt**（robertmartin8，5,982★ / MIT / 活跃）— 组合优化入门标准库：有效前沿、协方差收缩、HRP 层次风险平价、LASSO 稀疏化等，API 友好。[gh][web]
- **Riskfolio-Lib**（dcajasn，4,456★ / 2026-08 活跃) — 风险度量最全的优化库（VaR/CVaR/CDaR/最坏情形/MAD 等 30+ 种），支持风险预算与层次聚类约束；学术味浓但维护勤。[gh][web]
- **skfolio**（2,240★ / BSD / 当天活跃）— 后起之秀：完全 scikit-learn 化的组合优化（pipeline/cross-validation/GridSearch 直接用于组合模型），是这一类里工程现代化做得最好的。[gh][web]
- **pyfolio**（quantopian，6,406★ / 2023 停更）→ **pyfolio-reloaded**（stefan-jansen，609★，维护中）；同门 empyrical 同样以 -reloaded 接力。绩效归因/风险报表的标准工具（tearsheet 图）。[gh][web]
- **quantstats**（ranaroussi，7,586★ / MIT / 活跃）— 一行代码出全套绩效指标与 HTML 报告；轻量实用主义首选。[gh]
- **riskparity.py**（dppalomar）— TensorFlow 2 风险平价；**deepdow**（1,180★，2024 后停滞）— 深度学习端到端组合优化；**Eiten**（tradytics，3,289★，2022 死）— 特征组合/eigen 组合/遗传算法工具包。[gh][web]
- **fortitudo.tech** — CVaR 优化 + Entropy Pooling 观点/压力测试。[web]
- **universal-portfolios** — 在线组合选择（Cover 的 universal portfolio 等）算法集。[web]

---

## 第 7 章 因子挖掘与检验

- **alphalens-reloaded**（stefan-jansen，634★ / 维护中）— 因子 IC/分层收益/换手分析的标准工具（zipline 生态遗产的活跃接力）。[gh]
- **Spectre**（Heerozh，821★ / 2025-04 半停滞）— GPU 加速因子分析与回测（PyTorch/CUDA）。[gh][web]
- **Qlib 内置因子体系**：Alpha158/Alpha360 + factor 处理器链，配合上文 RD-Agent 可全自动挖因子。[primary]
- **alpha-skills**（VernonOY，97★ / 活跃）— "把因子研究做成 AI 编码助手的 skills"：发现/评估/回测/监控全流程提示词工程，支持 A股/港/美股——新形态尝试。[gh][web]
- **QuantGPT**（Miasyster）— agent 驱动的 A股因子引擎：假设设计→回测→打分→抗过拟合检测，8 个 MCP 工具。[web]
- **过拟合审计类新工具（2025–2026 出现的一批"反泄漏"项目，值得关注）**：
  - `eslazarev/purged-cross-validation`（27★，2026-08 活跃）— sklearn 兼容的 purged/CPCV 交叉验证 + Deflated Sharpe/PBO/最小回测长度统计。[gh][web]
  - `Perception-XAlpha Lite` — 回测过拟合审计（CSCV PBO、White's Reality Check、point-in-time 成分对齐）。[web]
  - `pit-release-gate` — 检测交错数据到达导致的截面泄漏并给信号分级。[web]
  - `backtest-bias` — 检测回测数据是否幸存者样本（死名单识别）。[web]
  - `rulelint` — 把交易规则在历史 bar 上重放，抓前视水平/永不触发的死分支。[web]
- **TrendFollowingSystems**（ArturSepp，20★ / 2026-08 活跃）— 趋势跟踪系统的闭式期望收益/Sharpe/偏度推导 + 数十年期货回测复现，学术严谨的小而美。[gh][web]

---

## 第 8 章 行情与数据层

- **OpenBB**（72,269★ / 自定义 license / 活跃)— 开源金融数据平台："开源 Bloomberg 终端"进化而来，现在是 platform + provider 架构，明确面向分析师/quant/**AI agent**（有 MCP 支持）。[gh][web]
- **ccxt**（43,743★ / JS/Python/PHP / 当天活跃）— 100+ 加密交易所统一 API，crypto 数据/交易的绝对地基。[gh]
- **yfinance**（25,071★ / Apache-2.0 / 活跃) — Yahoo Finance 非官方 Python 接口，全球散户量化的事实默认数据源（本仓库也在用）。[gh]
- **akshare**（22,232★ / MIT / 活跃）— 国内数据接口大全（股票/期货/期权/宏观/另类），A股生态标配。[gh]
- **tushare**（waditu，15,363★ / 2024-03 减速）— 老牌 A股数据接口，重心已转商业 Pro API。[gh][web]
- **alpaca-py**（alpacahq，1,473★ / Apache-2.0 / 活跃) — Alpaca 官方 Python SDK（行情+交易）。[gh]
- 指标层事实标准：**TA-Lib**（C 库 + ta-lib-python）、pandas-ta 等。[web]

---

## 第 9 章 中文 / 国内生态（专节）

除上文已列的 vn.py、WonderTrader、Hikyuu、zvt、rqalpha、czsc、easytrader、Qbot、myhhub/stock、ai_quant_trade、TradingAgents-CN/-astock、akshare 外：

- **vn.py / VeighNa**（44,762★ / MIT / 2026-08 活跃）— 国内量化开源的绝对龙头：事件驱动引擎 + 100 多个适配器（CTP 期货、股票柜台、加密所），从个人到私募都在用；社区以国内为主。[gh][web]
- **abu 阿布量化**（18,228★ / 2026-01 减速）— 《量化交易之路》配套系统：A股票/期货/crypto 全覆盖的规则策略库与回测。[gh][web]
- **QUANTAXIS**（11,043★ / 2026-02 减速）— 分布式部署的本地化全流程方案：数据/回测/模拟/实盘/可视化/多账户。[gh][web]
- **czsc 缠论**（waditu，5,917★ / 活跃）— 缠中说禅技术分析的工具化实现（笔/段/买卖点），A股特色技术流派里开源化最好的一个。[gh][web]
- **DeepTraderV2**（Lihw99，35★）— 把聚宽策略一键迁移到本地的工具（免费 Tushare 数据），反映"商业平台策略私有化"这个真实需求。[gh][web]
- 语境说明：聚宽(JoinQuant)/掘金/BigQuant/米筐(RiceQuant) 是国内主流**商业**量化平台（rqalpha 即米筐开源件），不在"开源项目"范围内，但与上述开源生态深度互操作。

---

## 第 10 章 其他语言生态速览

| 语言 | 代表项目 | 说明 |
|---|---|---|
| C++ | QuantLib（7,545★）、PandoraTrader、TradeFrame | 定价库之王 + 国内 CTP 高频框架 |
| C# | Lean（21,349★）、StockSharp（10,637★）、QLNet | 引擎级平台双雄 |
| Rust | nautilus_trader（27,764★）、barter-rs（2,243★）、openlimits | 性能派新贵聚集地 |
| Java | ta4j（2,480★，MIT，活跃） | JVM 世界的技术指标/回测标准库 |
| Julia | Fastback.jl、Strategems.jl、RiskPerf.jl | 学术性探索 |
| R | quantstrat、blotter、PerformanceAnalytics、PortfolioAnalytics、FactorAnalytics | 学术界/统计师传统阵地，blotter/quantstrat 仍在维护 |
| Go | Kelp（Stellar 基金会）、gobacktest、go-tart | 边缘存在 |
| Elixir | tai、workbench、prop（fremantle 系） | 分布式交易运营实验 |
| JS/TS | gekko（死）、Superalgos、PineTS（Pine Script 转译器） | 浏览器/Node 向 |
| MATLAB | QUANTAXIS(M版)、PROJ_Option_Pricing_Matlab | 教学遗留 |

---

## 附录 A：长尾项目清单（未展开，仅登记）

**回测/框架类**：pyalgotrade、basana（async crypto 事件驱动）、qtpylib、Quantdom（GUI）、pinkfish、pybacktest、finmarketpy、qf-lib、pyqstrat、NowTrade、YABTE、antback、fast-trade、catalyst（crypto zipline，死）、pylivetrader/pipeline-live（alpacahq，陈旧）、moonshot/zipline-extensions（QuantRocket 商业生态的开源件）、Investing Algorithm Framework、QTradeX-Algo-Trading-SDK、gunbot-quant、letianzj/quanttrader（765★）、sdoosa-algo-trade-python、PythonTradingFramework（K8s 部署向）。

**RL/ML 类**：TradingGym（Yvictor）、pskrunner14/trading-bot（DQN）、AlphaPy（AutoML）、bulbea、ib_nope（NOPE 策略 IBKR 自动交易）、aurumq-rl、huseinzol05 系列（archived）。

**Bot/执行类**：blackbird（BTC 跨所套利 C++）、r2（TypeScript 套利）、bitcoin-arbitrage、Kelp（Stellar DEX）、bTrader（三角套利 Rust）、crypto-crawler-rs、FAIG（IG Index spread betting）、income-desk（小资金期权）、ThetaGang 已入正文、the0（容器化多语言 bot 引擎）、mx-trader-bridge（东财妙想模拟盘 AI 桥）、TradeSight（Alpaca paper 自托管 AI 平台）、Orallexa（9 个 ML 模型排序 + Alpaca paper，277 测试）、Inalpha（LLM 写策略代码过沙箱审计的多 agent 框架）、midas-core（LLM agent 下单 + 独立 broker 进程强制 15 条安全护栏，git commit 戳记可复现）、demandai/ai-quant-agents（12 agent 实时辩论选股，美/A股）。

**经纪商接口类**：tda-api（TD Ameritrade，随 TD 并购失效中）、capitalcom-cli、mt5-httpapi / ibkr-httpapi（把 MT5/IBKR 包成 REST+MCP 服务，AI-agent 友好新物种）、binance-fix-connector-python、pyhood（Robinhood 无人值守客户端）、jiji2（Ruby FX/OANDA）。

**审计/合规类（2026 新物种）**：autonomous-audit（SHA-256 决策审计链）、NoEdge-Bench（"没有模型能打赢无记忆合成基准"的负结果基准）、backtester-mcp（带 PBO/Deflated Sharpe 的 MCP 回测服务器）、AlgoVault（crypto 信号 MCP + 链上 Merkle 战绩验证）、honest-signals（图形形态 vs 基线的提升度检验）、purgedcv 已入正文。

## 附录 B：学习资源与策略复现库（简列）

- `stefan-jansen/machine-learning-for-trading`（20,636★，活跃）— ML4Trading 第三版全套代码，数据→模型→实盘。
- 两份 awesome 清单本身就是最好的地图：`wilsonfreitas/awesome-quant`（29,163★）、`paperswithbacktest/awesome-systematic-trading`（13,952★，含 **40+ 篇学术论文按资产类别 × Sharpe 排序的策略复现索引**）、`wangzhe3224/awesome-systematic-trading`（5,015★，中文友好）。
- 书籍代码：Rob Carver systematictradingexamples / pysystemtrade_examples、AFML 习题解答（boyboi86/AFML）、Hilpisch 四件套（py4fi2nd/aiif/py4at/dawp）、ExpectedReturns（R）、101 Formulaic Alphas 复现（ram-ki）、shashankvemuri/Finance（150+ 个量化小程序）、LastAncientOne 系列。

---

## 数据说明与局限

1. 所有 stars/forks/pushedAt/license 来自 GitHub REST API（`gh repo view`），采集时刻为 2026-08-25；描述性论断凡未标 [primary] 者均为 [web] 级证据，使用前建议自行到仓库核实。
2. 覆盖偏差：本次以 GitHub 为中心 + Exa 补充，GitLab/Gitee 上的项目、闭源但免费的项目、以及纯论文无代码的工作未系统覆盖；awesome 清单收录本身偏向 Python 生态。
3. stars 是热度不是质量；"最近推送"是活性下限而非健康证明（部分项目推送频繁但 issue 积压）。
4. LLM Agent 类项目迭代极快（TradingAgents 三周一版），本文快照可能数月内过时；引用具体版本号时注意时效。
