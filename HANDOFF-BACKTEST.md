# Handoff: Grok backtest session → Codex

**From:** Grok 4.6 in `C:\Users\helow\Documents\Trading`
**When:** 2026-08-21 (results through Yahoo bar 2026-08-21)
**For:** Codex continuing this work. Treat this file as the session memory. Do not replay the raw transcript (`回测` at repo root is a dump, incomplete — ignore it).

Live paper trading is a separate system. This session did **not** change live knobs based on the new numbers. `buy_threshold: 0.35` stays as a *risk choice*, not an optimized edge.

---

## 1. What the user asked

1. “这个模型回测 — 用什么数据、数据从哪来、指个方向，我去弄，或者你自己就能做。”
2. After seeing 2019–2026 results: “这几年真的是合适窗口吗？2019 之后股市涨得有点太多了。”
3. Chose next run: **2007-04 → today, frozen live knobs** (not the 2010–2018 extra slice).
4. Then: write this handoff so Codex sees the whole backtest process.

---

## 2. What already existed (Grok did not invent the engine)

The live cycle is replayed by `python -m agentic_trading.backtest`.

| Piece | Path | Role |
|---|---|---|
| CLI | `src/agentic_trading/backtest/__main__.py` | `--start/--end/--cash/--sweep`; later (not by Grok) `--surface/--walk-forward/--ablate` |
| Loop | `src/agentic_trading/backtest/engine.py` | `run_backtest`: each session `broker.process_bar` then `run_cycle(skip_llm=True, asof=session)` |
| Prices | `src/agentic_trading/backtest/data.py` | Yahoo `history(period="max", auto_adjust=True)` → `data/cache/max/*.parquet` |
| Fills | `src/agentic_trading/execution/sim_broker.py` | Decision at T close, fill T+1 open; stop uses day's low; gap-through stop fills at open |
| Metrics | `src/agentic_trading/backtest/metrics.py` | CAGR, vol, Sharpe, maxDD, Calmar, win rate, profit factor, exposure, yearly |
| Cycle | `src/agentic_trading/run.py` | Same decide/size/exit as live. `asof is not None` ⇒ no live quotes, no TradeIntent flush, no Grok |
| Score | `src/agentic_trading/signals/technical.py` | trend 0.30, cross 0.20, momentum 0.20, macd 0.20, rsi 0.10 |
| Regime | `src/agentic_trading/signals/macro.py` | RSP/SPY, HYG/LQD, IWM/SPY, SPY/TLT, XLY/XLP + VIX damper |

**Quant-only.** LLM / news / fundamentals / Grok are not historically reproducible without look-ahead. Quant-only is a lower bound on selectivity, not a Kimi simulation. Do not backtest headlines.

Slippage in the sim broker: 5 bps entry/exit, 10 bps on stops. Starting cash $100,000.

Look-ahead guard: `HistoricalFeed` + `slice_asof`. Covered by `tests/test_backtest.py`.

---

## 3. Data (already on disk, no vendor to fetch)

Source: **Yahoo Finance via yfinance**, `auto_adjust=True` (splits + dividends). No API key. Alpaca historical is worse here (free IEX, short history, no `^VIX`).

Grok force-refreshed `data/cache/max/` on 2026-08-21. Last bar **2026-08-21**.

**Tradeable universe (core watchlist, 12 names):**
AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA, JPM, XOM, JNJ, WMT, UNH

**Regime only (never bought in these runs):**
SPY, RSP, IWM, HYG, LQD, TLT, XLY, XLP, ^VIX

QQQ is `context_symbols` but not used by the five-ratio macro.

**Do not** put research-vault names (BOTZ, BTSG, IQV, NBIS, VEEV, …) in a historical replay. Those were picked because they look interesting *now*.

**Listing dates that shrink the 2007 book:** TSLA 2010-06-29, META 2012-05-18. HYG lists **2007-04-11** — that is the earliest date the *live* regime can run. Do not start earlier without silently dropping credit.

`data/cache/` and `data/backtest/` are gitignored.

---

## 4. Config frozen for every Grok run

From uncommitted `config/risk.yaml` / `config/watchlist.yaml` at run time (working tree, not origin/master):

- `buy_threshold: 0.35` (live; picked in-sample on an older 2019–2026 14-name book that could buy SPY/QQQ)
- `sell_threshold: -0.25`
- `min_quant_score_to_consider: 0.15`
- `atr_stop_multiple: 2.5`, `min_stop_pct: 0.06`, `max_stop_pct: 0.20`
- `trailing_stop_pct: 0.12`, `take_profit_pct: null`
- `risk_per_trade_pct: 0.015`, `max_position_pct: 0.18`, `max_open_positions: 8`
- `risk_off_size_multiplier: 0.5`, `risk_off_score_penalty: 0.15`
- SPY/QQQ moved to `context_symbols` — **not tradeable**. Old README 21.3% CAGR was a 14-name book that could buy the indices. Not comparable.

Grok **did not write** `risk.yaml`. Did not re-sweep ATR / trail / max_pos.

---

## 5. Chronological process (what Grok actually did)

### 5.1 Read the engine, refuse exotic data

User offered to download data. Grok did not: daily OHLCV is already wired. Tick/minute bars would only refine stop-fill realism. News archives would leak look-ahead.

Noted prior artifacts in `data/backtest/thresh_{baseline,looser,tighter}.json` — those are the **old 14-name (incl. SPY/QQQ)** in-sample pick of 0.35. `equity.csv` on disk at session start ended at $325,227, matching `thresh_baseline.json` (threshold 0.25), not the live 0.35.

### 5.2 Re-run current 12-name universe 2019-01-01 → 2026-08-21

Script: `data/backtest/_eval.py` (gitignored one-shot; not part of the package).
Loads bars once, then:

1. Full window, live knobs → `full_current.json`, overwrites `equity.csv`, `journal.db`
2. Train 2019-01-01 → 2022-12-31 at buy 0.20 / 0.25 / 0.35
3. Holdout 2023-01-01 → 2026-08-21 at train-winner and 0.25 / 0.35

**Full 2019-01-02 → 2026-08-21, $100k, threshold 0.35**

| | Strategy | SPY | Eq-weight 12 |
|---|---:|---:|---:|
| CAGR | +22.4% | +17.5% | +35.9% |
| Vol | +15.5% | +19.3% | +31.1% |
| Sharpe | 1.39 | 0.93 | 1.15 |
| Max DD | −16.7% | −33.7% | −46.9% |
| Calmar | 1.34 | 0.52 | 0.77 |
| End $ | 468,777 | 342,158 | 1,039,695 |
| Trades | 201 buys / 196 sells | | |
| Win / PF / exposure | 43% / 2.31 / 69% | | |

Years (strategy vs SPY):
2019 +26.8 / +31.1; **2020 +47.0 / +17.2**; 2021 +26.5 / +30.5; 2022 −14.2 / −18.6; 2023 +32.9 / +26.7; 2024 +29.0 / +25.6; 2025 +23.4 / +18.0; **2026 YTD +7.6 / +12.7**.

Reading: volatility-damped overlay on *today’s* mega-caps. Beats SPY on risk-adjusted terms; **loses badly to buy-and-hold of the same 12**. 22% CAGR is partly universe selection.

### 5.3 Walk-forward the buy bar on that same biased window

**Train 2019–2022** (pick by Sharpe, freeze):

| buy_threshold | CAGR | Sharpe | Max DD | End $ | buys |
|---|---:|---:|---:|---:|---:|
| 0.20 | +13.3% | 0.90 | −23.0% | 165,072 | 131 |
| 0.25 | +11.1% | 0.78 | −24.0% | 152,292 | 125 |
| **0.35** | **+20.0%** | **1.25** | **−16.0%** | **207,159** | 112 |

**Holdout 2023-01-03 → 2026-08-21** (SPY CAGR +22.7%, Sharpe 1.44):

| buy_threshold | CAGR | Sharpe | Max DD | End $ | vs SPY Sharpe |
|---|---:|---:|---:|---:|---|
| **0.25** | **+26.9%** | **1.54** | −16.0% | 237,186 | beats |
| 0.35 (live) | +22.9% | 1.35 | −15.0% | 211,123 | **loses** (CAGR ≈ SPY +22.7%) |

“Being pickier won on every axis” is true on 2019–2022 and on the full window used to pick 0.35, **false after**. Keep 0.35 as conservative risk, not OOS proof.

JSON: `train_th_0p20.json`, `train_th_0p25.json`, `train_th_0p35.json`, `holdout_th_0p25.json`, `holdout_th_0p35.json`.

### 5.4 User: is 2019–now a normal window?

Grok computed SPY from `data/cache/max/SPY.parquet` (adjusted). **No.**

| Window | SPY CAGR | Max DD | Role |
|---|---:|---:|---|
| 1993–2026 | +10.9% | −55% | long-run index; cannot run full model (no HYG) |
| 2000–2009 | −0.9% | −55% | lost decade |
| 2000–2018 | +4.8% | −55% | hard 19 years |
| **2007-04-11 → 2026-08-21** | **+11.0%** | **−55%** | **honesty window** — first day HYG exists |
| 2010–2018 | +11.4% | −19% | boring bull |
| 2019–2026-08-21 | **+17.5%** | −34% | current tape only |
| 2023–2026-08-21 | **+22.7%** | −19% | AI bull |

2019–2026 SPY is ~60% faster than long-run. User then chose the 2007-start run.

Do not start before 2007-04-11 unless you *explicitly* drop HYG/LQD. Do not do that silently.

### 5.5 Frozen knobs, 2007-04-11 → 2026-08-21

~19.4 years, 12 names, threshold 0.35, no retune. Inline python calling `run_backtest` (not the CLI). Wrote `full_2007.json`, `equity_2007.csv`, `journal_2007.db`.

| | Strategy | SPY | Eq-weight 12 |
|---|---:|---:|---:|
| CAGR | **+15.1%** | +11.0% | +25.5% |
| Vol | +12.9% | +19.7% | +26.0% |
| Sharpe | **1.16** | 0.63 | 1.00 |
| Max DD | **−26.5%** | −55.2% | −51.9% |
| Calmar | **0.57** | 0.20 | 0.49 |
| End $ | **1,525,140** | 757,271 | 8,128,257 |
| Trades | 454 buys / 449 sells | | |
| Win / PF / exposure | 41% / 2.36 / **61%** | | |

**Max DD timing (from `equity_2007.csv`):** peak 2007-10-23 ($136,265) → trough 2009-03-30 ($100,198). That is the GFC. Honest max DD of this system is **~27%, not the 17% from 2019-start**.

Calendar 2008: **strategy −11.6% vs SPY −36.2%**.

Yearly strategy vs SPY (2007-run):

| Year | Strategy | SPY | Note |
|---|---:|---:|---|
| 2007 | +20.8% | +3.0% | |
| **2008** | **−11.6%** | **−36.2%** | crash test |
| 2009 | +20.7% | +22.7% | missed some bounce |
| 2010 | +8.3% | +13.1% | quiet-bull lag |
| 2011 | −0.5% | +0.9% | |
| **2012** | **+0.3%** | **+14.2%** | worst lag |
| 2013 | +30.8% | +29.0% | |
| 2014 | +6.6% | +14.6% | lag |
| 2015 | +16.7% | +1.3% | trend harvest |
| 2016 | +41.0% | +13.6% | |
| 2017 | +22.8% | +20.8% | |
| 2018 | −1.7% | −5.2% | |
| 2019 | +24.8% | +31.1% | |
| 2020 | +32.6% | +17.2% | |
| 2021 | +20.9% | +30.5% | lag |
| 2022 | −11.6% | −18.6% | |
| 2023 | +32.1% | +26.7% | |
| 2024 | +28.9% | +25.6% | |
| 2025 | +15.4% | +18.0% | |
| 2026 | +9.7% | +12.7% | YTD through 8/21 |

Personality: crash damper, sits ~39% in cash, needs a trend, lags grind-ups. **Quote +15% CAGR / −27% max DD / 2008 −12%.** Not +22% / −17%.

Eq-weight $8.1M from 2007 is “picked MAG7 in 2007 and never sold.” Unfair. Benchmark is SPY.

Intra-year drawdowns on the 2007 equity curve stay in a ~5–16% band after GFC (2018 −15.5%, 2012 −14.7%, 2020 −14.3%, 2022 −13.5%).

---

## 6. Headline conclusions (do not water these down)

1. **Do not treat 22.4% as the model.** That is a 2019–2026 mega-cap bull (SPY itself +17.5%).
2. **On a normal-return window (SPY +11%), the overlay still beats SPY** (+15.1% CAGR, Sharpe 1.16 vs 0.63) and cuts GFC damage from −36% calendar / −55% peak-trough to −12% / −27%.
3. **It is not an alpha engine vs holding the same names.** Eq-weight of the 12 crushes it on CAGR; the pitch is Calmar / drawdown.
4. **0.35 is in-sample on the bull half.** Holdout 2023–2026 prefers 0.25. Do not re-pick 0.35 on 2007–2026 either — freeze it, then look.
5. **Survivorship is still in the 12 names.** 2007-start helps (META/TSLA absent in GFC; NVDA not “AI”). Remaining test: 2019-era peers that did not become MAG7.

---

## 7. Artifacts (local, gitignored except as noted)

| Path | What |
|---|---|
| `data/backtest/full_current.json` | 2019–2026 live knobs |
| `data/backtest/equity.csv` | that run’s equity (overwrote the old 0.25 curve) |
| `data/backtest/journal.db` | that run’s cycle journal |
| `data/backtest/train_th_0p20.json` etc. | 2019–2022 threshold sweep |
| `data/backtest/holdout_th_0p25.json` / `0p35` | 2023–2026 holdout |
| `data/backtest/full_2007.json` | **headline run** |
| `data/backtest/equity_2007.csv` | 2007–2026 equity |
| `data/backtest/journal_2007.db` | 2007–2026 journal |
| `data/backtest/_eval.py` | one-shot script for §5.2–5.3 |
| `data/backtest/thresh_*.json` | **stale** pre-session 14-name results — do not mix |
| `data/backtest/ablations.txt` | **do not trust** — see §8 |
| `data/cache/max/*.parquet` | Yahoo max history, refreshed 2026-08-21 |
| `回测` (repo root, untracked) | raw Grok transcript dump, incomplete |

Reproduce (venv already has the package):

```powershell
# headline 2007 window, live knobs
.\.venv\Scripts\python.exe -m agentic_trading.backtest --start 2007-04-11 --end 2026-08-21

# 2019 diagnostic (biased)
.\.venv\Scripts\python.exe -m agentic_trading.backtest --start 2019-01-01 --end 2026-08-21
```

A 19-year daily replay is ~15–20 minutes. Progress logs every 252 sessions.

---

## 8. Repo state Codex will see (this is not all Grok)

`origin/master` last commits: `43b7381` system, `f576b58` raise bar to 0.35 / no intraday entries.

**Uncommitted / untracked as of 2026-08-22** includes Grok-era watchlist/risk comments **and a lot of work Grok did not do in this session**: `experiments.py`, `journal/evaluate.py`, `config/sectors.yaml`, tests for those, plus edits to `sim_broker.py`, `risk/manager.py`, `technical.py`, `engine.py`, `__main__.py` (`--surface/--walk-forward/--ablate`).

Grok in *this* conversation: read engine, ran backtests, wrote gitignored `data/backtest/_eval.py` and result json/csv, wrote session `plan.md`. **Did not add `experiments.py`.**

If you keep those experiment modules: fine, but **do not treat `ablations.txt` as evidence**. It shows baseline CAGR +125.7%, Sharpe 4.34, MaxDD −3.3%, 8 buys, end $122,218 — that is a **sub-year window** (or a broken run), not 2007–2026 and not 2019–2026. Re-run ablations with `--start 2007-04-11 --end 2026-08-21` if you want them.

Default CLI `--start` was `2019-01-01` when this was written; it has since been
changed to **`2007-04-11`** (the first day HYG trades), so the biased window
can no longer be entered by accident.

---

## 9. Do not

- Do not retune `buy_threshold` / ATR / trail on 2019–2026 or on 2007–2026 and call it research.
- Do not backtest LLM/news.
- Do not include the research vault in the historical universe.
- Do not quote eq-weight of these 12 as the hurdle; quote SPY.
- Do not start before HYG (2007-04-11) without documenting a degraded regime.
- Do not “fix” 2012’s +0.3% vs SPY +14% by loosening the bar on that window.
- Do not overwrite `experiments.py` / `evaluate.py` without reading them; they post-date Grok’s runs.

---

## 10. Safest next actions (priority)

1. **Survivorship book** — same engine, frozen 0.35, 2007-04-11 → today *or* 2019 → today, universe = current 12 plus 2019-era large caps that did not all become MAG7: `INTC DIS PYPL BA PFE IBM GE CSCO CVX MRK` (Yahoo, no new vendor). If Calmar vs SPY dies, the 15% is still name selection.
2. **Per-symbol attribution** on `journal_2007.db` / sim fills — how much of +15% is NVDA. No new data.
3. Optional: 2010–2018 isolated slice. Low value; the lag is already in 2010/2012/2014 yearly rows.
4. If you run `--ablate` / `--walk-forward` from the new CLI, **pass `--start 2007-04-11`**. Expanding walk-forward on 2007–2026 is a reasonable upgrade over Grok’s single 2019/2023 split; do not use it to pick a new live threshold in the same sitting.

Judge the live paper account against **drawdown in 2022-like / 2008-like tapes**, not against 22% CAGR.
