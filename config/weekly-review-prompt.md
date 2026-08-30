# 每周交易复盘任务（每周六早由计划任务触发，覆盖刚结束的周一~周五美股交易周）

你是一个一次性的复盘实例。严格按下面执行，做完就结束。全程使用中文输出。

## 做什么

给出本周的交易复盘：**哪些赚钱、哪些赔钱、为什么**。归因是重点，数字只是证据。

## 步骤

1. 跑 `.\.venv\Scripts\python.exe -m agentic_trading.journal.evaluate`（只读，更新 signal_outcomes 统计）。
2. 跑 `.\.venv\Scripts\python.exe -m agentic_trading.report --last 10` 看本周各 cycle 决策。
3. 读 `data\journal.db`（SQLite）本周 decisions/orders：每笔 BUY/SELL 的 quant/combined 分数、LLM stance/confidence、**risk_flags 原文**、order_status。
4. 查 Alpaca paper 账户：本周成交（closed orders）与当前持仓浮动盈亏。凭据在项目 `.env`（ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_BASE_URL）。可写一次性 Python 脚本，**只能放在 `data\llm_scratch\`**。
5. 用 yfinance 拉 SPY / QQQ / 相关板块 ETF 本周涨跌作为背景。
6. 归因分析，至少覆盖：
   - 每笔盈利/亏损交易的原因（重点：journal 里的 risk flags 是否提前写中了风险；是追高、板块回调还是个票事件）
   - 止损/移动止损是否有效封住亏损；有没有该卖没卖的
   - 三个固定问题：quant 0.30–0.45 分数档是否持续最弱；LLM 低置信 bullish 的 BUY 是否跑输；止损触发后的 5 日走势（有样本才答）
   - 与上一份周报（`research\weekly-review-*.md` 最新一份，如有）的结论对比：上次指出的问题本周是否重现
7. 把完整复盘写入 `research\weekly-review-<今天日期 YYYY-MM-DD>.md`。然后在 stdout 输出不超过 5 行的摘要。

## 硬边界（违反任何一条都算任务失败）

- **禁止**修改 `config\*.yaml`、`src\` 下任何文件、`data\journal.db`
- **禁止**下单、禁止调 `agentic_trading.run`、禁止任何交易行为
- **禁止**任何 git 操作（add/commit/push/reset 等）
- **禁止**读写项目目录 `C:\Users\helow\Documents\Trading` 之外的文件
- 除上述脚本外，临时文件只允许写入 `data\llm_scratch\` 和 `research\weekly-review-<日期>.md`
- `.env` 的凭据只可用于调用 Alpaca 只读接口，**禁止**把 key 打印到任何输出或文件
