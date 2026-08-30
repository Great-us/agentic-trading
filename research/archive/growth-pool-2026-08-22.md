---
title: 增长股池 2026-08-22
date: 2026-08-22
type: analysis
tags: [投资研究, seeking-alpha, 增长股池, AI基建]
tradeable: false
---

# 增长股池研究（Seeking Alpha + X）— 2026-08-22

> 给 paper trading agent 的**候选宇宙**，不是对你个人的买入建议。
> 数据截止 **2026-08-21 美股收盘**（SA 页面为 08/21 16:00 ET）。登录态：已登录 Premium。
> 中途两次撞上 PerimeterX Press & Hold，**没有点验证按钮**；第二次发生在 `VRT` 页，后面几个名字没打开。

## 1. 结论（先看这张表）

**建议核心池 14 只**（替换现在的 12 只 mega-cap + 传统仓）：

| # | Ticker | 角色 | SA Quant | Growth | 20日 ADV | X |
|---|---|---|---|---|---|---|
| 1 | MU | HBM / DRAM | Strong Buy **4.99**（全市场第 1） | A+ | $34.6B | 确认 |
| 2 | SNDK | NAND 存储 | Strong Buy **4.99**（第 2） | A+ | $23.5B | 确认 |
| 3 | HPE | AI 服务器 | Strong Buy **4.99** | B+ | $0.87B | 中性 |
| 4 | AMD | GPU 挑战者 | Strong Buy **4.97** | A | $12.8B | 确认 |
| 5 | AMZN | 云 + 零售（唯一 Quant 强买的 Mag7） | Strong Buy **4.96** | A- | $12.6B | 确认 |
| 6 | NBIS | AI 云 | Strong Buy **4.94** | A+ | $6.2B | 确认（稀释风险已定价） |
| 7 | ALAB | AI 连接 / PCIe | Strong Buy **4.91** | A+ | $1.5B | 确认 |
| 8 | ANET | 数据中心网络 | Strong Buy **4.90** | B- | $1.5B | 确认 |
| 9 | CRDO | SerDes / AEC | Strong Buy **4.88** | A+ | $1.1B | 确认 |
| 10 | FLEX | AI 制造 / EMS | Strong Buy **4.64** | A- | $0.53B | 中性 |
| 11 | CIEN | 光网络 | Strong Buy **4.52** | A | $0.88B | 确认 |
| 12 | NVDA | AI 计算龙头 | **Hold 3.49**（估值 D-） | **A+** | $24.7B | 确认 |
| 13 | ARGX | 生物药（非科技） | Strong Buy **4.70** | A+ | $0.28B | 中性 |
| 14 | VEEV | 生命科学软件（非科技） | Strong Buy **4.81** | C | $0.40B | 中性 |

**从核心移出：** AAPL、MSFT、GOOGL、META、TSLA、JPM、XOM、JNJ、WMT、UNH。

理由一句话：这 10 只里，除 XOM 外 Quant 全是 Hold；增长因子 AAPL D-、JNJ D、UNH D-、JPM C-、WMT C-、MSFT C-。XOM 虽然 Quant 4.87，增长仍是 C-，不符合「增长池」。NVDA 是唯一建议**留在核心**的 Quant Hold——增长仍是 A+，趋势系统需要这个名字，但估值已经很贵。

未改 `watchlist.yaml`。文末有建议 diff，你点头后再贴进去。

---

## 2. 方法

1. 用你已登录的 Chrome（OpenCLI Browser Bridge，profile `y7x3b5mw`）打开 SA。
2. 预置筛：Top Rated（Quant+作者+华尔街三方买入）、Top Growth（市值≥$1B、Growth A+、Quant Buy/SB）、Top Semiconductor、Trending AI。
3. 对 Mag7、传统五仓、筛子头部名字打开 **Summary** 页读 Ratings + Factor Grades（不去 `/ratings/quant-ratings`，那个 URL 会触发人机验证）。
4. X：agent-reach / OpenCLI twitter + 关键词检索。X 只打脸或加强，不单独入池。
5. 本地 yfinance 20 日美元成交额，门槛 `$20M`（`config/risk.yaml` 的 `min_adv_usd`）。候选全部过线。
6. 强制留 2 只非科技（ARGX、VEEV），避免一开仓就撞 Technology 35% 上限。

**没做的：** 没改风险阈值、没把新名字塞进 2007 回测宇宙、没点 Press & Hold。

---

## 3. 当前核心仓的 SA 体检

| Ticker | Quant | SA 作者 | 华尔街 | Valuation | Growth | Profit | Momentum | Revisions | 处置 |
|---|---|---|---|---|---|---|---|---|---|
| NVDA | Hold 3.49 | Buy 3.91 | SB 4.70 | D- | **A+** | A+ | C+ | B- | **留**（增长还在，估值贵） |
| MSFT | Hold 3.47 | Buy 4.21 | SB 4.63 | F | C- | A+ | B- | C | 移出核心 |
| GOOGL | Hold 3.49 | Buy 4.14 | SB 4.64 | F | C+ | A+ | B- | A | 观察池 |
| AMZN | **SB 4.96** | Buy 4.18 | SB 4.68 | D+ | **A-** | A+ | B | B+ | **留**（Mag7 里唯一 Quant 强买，全市场第 22） |
| META | Hold 3.32 | Buy 3.97 | SB 4.64 | F | B | A+ | C- | C | 移出核心 |
| AAPL | Hold 3.47 | Hold 3.19 | Buy 3.81 | F | **D-** | A+ | C+ | C+ | 移出 |
| TSLA | Hold 3.21 | Hold 2.51 | Buy 3.65 | D- | B | A+ | **D** | C- | 移出（动量已经死） |
| JPM | Hold 3.43 | Buy 3.77 | Buy 3.87 | D- | C- | **F** | B | A+ | 移出 |
| XOM | **SB 4.87** | Buy 3.66 | Buy 3.68 | D | **C-** | A+ | B | B | 移出增长池（Quant 喜欢，增长不喜欢） |
| JNJ | Hold 3.28 | Buy 3.50 | Buy 4.08 | F | **D** | A+ | B- | D- | 移出 |
| WMT | Hold 3.28 | Hold 2.55 | Buy 4.41 | F | C- | A+ | D+ | C+ | 移出 |
| UNH | Hold 3.48 | Buy 3.72 | Buy 4.44 | C- | **D-** | A+ | B- | A- | 移出 |

你的直觉和 Quant 增长因子对得上：传统仓 + 苹果不是「增长乏力」的错觉，是 SA 因子已经打到 D/C 档。

---

## 4. 筛子里真正排在最前面的增长名

**Top Growth / Stocks by Quant 头部（Quant + Growth 同时高）：**

MU 4.99 A+ → SNDK 4.99 A+ → INTC 4.98 A+ → STX 4.98 A+ → WDC 4.97 A+ → AMD 4.97 A → NBIS 4.94 A+ → ALAB 4.91 A+ → CRDO 4.88 A+。

**Top Rated（Quant + SA 作者 + 华尔街同时 Buy/SB）** 里和增长相关、且够大的：MU、SNDK、CRDO、ONTO、FLEX。其余大量是船运、小盘生物、能源 MLP，ADV 或质量不够。

**Trending AI：** NBIS、AMD、VEEV、ANET、HPE（Quant 4.99）、AMZN、ALAB。HPE 是这次最大的「非共识」：不是 Mag7，但 Quant 全市场第 6。

INTC Quant 4.98、增长 A+，但 **SA 作者 Hold 3.28、华尔街 Hold 3.47**。三方打架，放观察池，不进 A。

---

## 5. 核心 14 只卡片

每只：一句话逻辑 / 最大风险 / SA / X。价格为 08/21 收盘。

### MU — Micron $966.78
HBM 卖光到 2026，存储从周期品变成 AI 瓶颈。Quant 全市场第 1，五因子几乎全 A/A+，Valuation 还有 A-（Mag7 是 F）。
风险：存储周期历史上会自己造下一轮过剩；X 上已有「$500M/日利润会变成新产能」的提醒。
X：**确认**。长协/HBM 叙事主导，同时有人把 MU 涨幅叫「涨价而不是需求」。

### SNDK — Sandisk $1,596.08
从 WDC 分出来的纯 NAND。Quant 第 2，Valuation **A+**（增长池里极少见）。
风险：分拆后交易历史短；NAND 价格如果反转，弹性双向。
X：**确认**，常和 MU/WDC 一起作为「存储短缺」篮子。

### HPE — Hewlett Packard Enterprise $53.45
AI 服务器 + 存储，Quant 第 6、Momentum A+、Revisions A+。Growth 只有 B+，不是最猛的，但是「能买到的算力机箱」。
风险：估值从 3 个月前的 A 掉到现在的 D，说明价格已经跑在基本面前面。
X：中性，讨论量远小于 NVDA/MU。

### AMD — $473.25
Quant 4.97、Growth A、Momentum A+。作者只有 Hold 3.42，华尔街仍 SB。
风险：挑战 NVDA 的叙事反复定价；作者端并不热。
X：**确认**，作为 GPU 第二名留在 AI 链里。

### AMZN — $258.63
Mag7 里唯一 Quant Strong Buy（全市场第 22，行业第 1）。云 capex 本身就是 AI 需求。
风险：估值仍是 D+；capex 数字大了市场会打。
X：**确认**（hyperscaler capex 篮子）。

### NBIS — Nebius $219.13
你 7 月已经写过笔记。Quant 4.94、Growth A+、Momentum A+，华尔街 **5.00**。盈利能力只有 B+，Revisions C+。
风险：X 上刚过 **$4.5B 可转债**；这是稀释，不是秘密。SA 没把它从 Strong Buy 拿下来，但 A 层必须带着这个风险。
X：**确认，带否决预警**（融资/neocloud 出清）。不降出 A：Quant + 增长 + 你已有研究都在，但仓位应比 MU 小。

### ALAB — Astera Labs $284.97
PCIe/CXL 连接，Morgan Stanley 年初 AI Top Picks 里就有。Quant 4.91、Growth A+。作者 Hold 3.42。
风险：产品周期 + 客户集中；作者端冷。
X：**确认**（AI connectivity 篮子，常和 CRDO/MRVL 并列）。

### ANET — Arista $188.65
数据中心网络。Quant 4.90、Profitability A+、Revisions A+。Growth 只有 B-，所以它不是「增速冠军」，是「质量冠军」。
风险：估值 D；Q2 营收 +38% 已经在价格里。
X：**确认**。

### CRDO — Credo $230.57
Top Rated 第 10（三方都买）。Growth A+、Profit A、Mom A。
风险：市值 $43B，赛道窄，一次产品切换就会重定价。
X：**确认**（光/铜连接、CPO）。

### FLEX — Flex $110.45
Top Rated + 首页有「AI Infrastructure Growth」研报。Quant 4.64，Valuation **A**（核心池里最不贵的科技名之一）。
风险：EMS 毛利率普通（Profit B-）；执行和客户集中。
X：中性。

### CIEN — Ciena $395.79
光传输，Quant 4.52、Growth A。X 上和 LITE/COHR 一起被当成光子学/互联。
风险：LITE 自己 Quant 是 Hold 且 Profit D-，说明这条赛道良莠不齐；CIEN 质量好于 LITE。
X：**确认**。

### NVDA — $214.72
Quant Hold 的原因是 **Valuation D-、Momentum C+**，不是增长没了（Growth 仍 A+）。全市场排名 766/4271。
风险：估值；下周前后有财报（X 上 08-22 已在预热）。对趋势系统：丢掉它等于丢掉最能走出趋势的名字。
X：**确认**，但仍是「买回调、不是买高位」口径。

### ARGX — argenx $1,039.78
非科技。Quant 4.70、Growth A+、Profit A。生物药，和半导体相关极低，专门用来满足 Healthcare 仓位上限。
风险：单品管线；ADV $283M 够用但比 MU 薄。Revisions 从 6 个月前 D- 修到现在 B+，方向对，仍要看财报。
X：中性（讨论少）。

### VEEV — Veeva $247.90
你仓库里已有深度笔记。Quant 4.81、Profit A+、Revisions A，**Growth 只有 C**。它进 A 层是因为：非科技、质量高、已研究、Quant 强买；不是因为增速最猛。
风险：增长因子弱，趋势系统可能长期给不出 BUY。如果三个月还是 C，降到 B。
X：中性。

---

## 6. 观察池（B）— 不进 yaml

| Ticker | 为什么观察、不进 A |
|---|---|
| INTC | Quant 4.98 但作者/华尔街都是 Hold，政治/晶圆代工故事 |
| STX / WDC | Quant 极强，和 MU/SNDK 存储相关过高，先放一边 |
| GOOGL / MSFT | 质量好、Quant Hold、增长 C 档；伯克希尔加仓 GOOGL 记在观察 |
| AVGO / TSM / MRVL | 增长仍 A 档，估值 F/D，Quant Hold |
| LITE / COHR | 光子热，LITE Profit D-，COHR Profit D+ |
| ORCL / APP / PLTR | Momentum D 档或 Quant < 3.3 |
| LLY | Growth A 但 Valuation F、Quant Hold |
| BTSG | Top Growth 4.81 A+，你有笔记；没来得及打开个股页（验证码） |
| CNC | Top Rated 4.94，Managed care，和你刚删的 UNH 同类 |
| CLS / TTMI / FN | AI 制造链，Quant 多为 Buy 不是 SB |
| VRT | X 上电源/散热瓶颈；打开时撞 Press & Hold，未取 Quant |

---

## 7. 硬过滤

- 20 日 ADV：A 层全部 ≫ $20M（最瘦的 ARGX 也有 $283M）。
- 上市地：全是美股主要交易所。SpaceX / LONN / BANB 继续排除。
- 科技仓：A 层 11/14 是 Technology。**开仓时** `max_sector_pct.Technology = 0.35` 会挡住堆满半导体；相关 >0.75 还会再砍一半。宇宙可以偏科技，持仓不行。
- 存储不要四只一起开：MU + SNDK 已经够；STX/WDC 故意留在 B。

---

## 8. X 交叉验证（辅助）

使用 agent-reach / OpenCLI twitter 和 X 关键词检索。不是选股引擎。

**同向：**
- 存储短缺（HBM/NAND/HDD）是 8 月 22 日讨论主线；有人把 SPX 基本面十强写成 MU/WDC/STX + 能源 + CIEN/LITE。
- 互联（CRDO/ALAB/ANET/CIEN）和光模块被当成 GPU 之后的瓶颈。
- Hyperscaler capex（MSFT/GOOGL/META/AMZN）仍被当作 MU/SNDK/NBIS 的上游。

**反向（已写进卡片）：**
- NBIS $4.5B 可转债。
- 「neocloud 36 个月内一半消失」「CRWV/NBIS 不会便宜」。
- MU 利润高峰会资助下一轮产能过剩。
- 有人把整条 AI 基建说成涨价叙事。这些没有把 MU/NBIS 拉出 A，但禁止把仓位当成「不会回撤」。

---

## 9. 建议 diff（未写入仓库）

`config/watchlist.yaml` 建议改成：

```yaml
symbols:
  - MU     # HBM
  - SNDK   # NAND
  - AMD    # GPU
  - NVDA   # AI compute (Quant Hold, Growth A+)
  - AMZN   # cloud (only Mag7 Quant SB)
  - ANET   # networking
  - CRDO   # connectivity
  - ALAB   # PCIe/CXL
  - NBIS   # AI cloud
  - HPE    # servers
  - FLEX   # EMS
  - CIEN   # optical
  - ARGX   # biotech (non-tech)
  - VEEV   # life-science software (non-tech)

context_symbols:
  - SPY
  - QQQ
```

`config/sectors.yaml` 建议补：

```yaml
MU: Technology
SNDK: Technology
AMD: Technology
NVDA: Technology
AMZN: Consumer Cyclical
ANET: Technology
CRDO: Technology
ALAB: Technology
NBIS: Technology
HPE: Technology
FLEX: Technology
CIEN: Technology
ARGX: Healthcare
VEEV: Healthcare
```

旧的 AAPL/MSFT/GOOGL/META/TSLA/JPM/XOM/JNJ/WMT/UNH 映射可以留着，不影响。

**不要**把这份名单丢进 2007 年起的回测：MU 今天的因子状态是 2026 的，不是 2008 的。

---

## 10. 你拍板时可以改的三件事

1. NVDA 要不要留：Quant Hold + 估值 D-。我建议留，因为策略吃趋势不是吃 Quant。
2. 存储只要 MU，还是 MU+SNDK。我建议两只（HBM vs NAND），不要再加 STX/WDC。
3. 非科技只要 ARGX，还是 ARGX+VEEV。VEEV 增长只有 C，偏质量。

回复「按建议改 yaml」我就改 `watchlist.yaml` 和 `sectors.yaml`，其它不动。
