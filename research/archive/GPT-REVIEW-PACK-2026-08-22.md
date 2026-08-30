# GPT Review Pack — Agentic Trading Universe Selection

**Date:** 2026-08-22  
**System:** US-equities long-only paper swing agent (`C:\Users\helow\Documents\Trading`)  
**Purpose of this file:** one self-contained packet so an independent model can audit the research process, the selection rules, and the proposed Active Watchlist.  
**Repo yaml status:** `config/watchlist.yaml` has NOT been updated to the proposed 16. Current live list is still the Round-1 growth pool (14 names including NVDA Hold).

This is **not** a chat transcript. There is no single original of the whole conversation. Working scraps live under `tmp/`. This pack is the thing to copy.

---

## 0. What the reviewer should attack

Please check:

1. Did SA Overall Quant actually get used as the quality gate, or did the author override it (especially NVDA Hold, gold miners Hold, CCJ Sell)?
2. Is dropping CRDO/ALAB/FLEX/CIEN (all still SA Strong Buy) justified as concentration, or is it an undeclared extra rule?
3. Is keeping both MU and SNDK (60d corr 0.87) inconsistent with dropping CVX vs XOM (corr 0.84)?
4. Did incomplete Quant coverage (PerimeterX from RKLB onward) silently bias Active toward names that happened to be scored first (energy, materials, healthcare) and against industrials, utilities, defense, REITs, financials, education?
5. Is the proposed 16 actually usable by a trend system that only buys when **own** technical score ≥ 0.35, given several names are below SMA50 (POOR ENTRY)?
6. Any Eligible SA Buy/Strong Buy that should have made Active and did not?

Do **not** re-optimize `buy_threshold`, ATR, or `risk.yaml`. This round is universe construction only.

---

## 1. The trading system (constraints that bound selection)

Long-only US stocks, Alpaca **paper**, cycle-based.

| Knob | Value | Effect on universe |
|---|---|---|
| `max_open_positions` | 8 | Active list of 16–20 is a scan universe, not a book |
| `buy_threshold` | 0.35 | **Own** technical score must clear this to BUY. LLM cannot originate BUY |
| Own technical score | trend 0.30, SMA cross 0.20, 20d mom 0.20, MACD 0.20, RSI 0.10 | Timing only after this reconstruction |
| `min_adv_usd` | $20M | Hard liquidity floor |
| `max_sector_pct.Technology` | 0.35 | A tech-only universe cannot be fully invested |
| `max_sector_pct.Energy` | 0.25 | |
| `max_sector_pct.Healthcare` | 0.30 | |
| Basic Materials / Industrials / Utilities | fall to `default_max_sector_pct` 0.35 | Not retuned this round |
| Correlation haircut | 60d corr > 0.75 → size × 0.5 | Already in risk manager; do not invent a new multiplier |

**Division of labor (agreed spec):**

```
Seeking Alpha Overall Quant  →  is this name worth watching?   (quality gate)
Tradability / ADV / mcap     →  can we actually trade it?
Own technical Quant          →  is this a good trend / entry now?  (timing)
Risk manager                 →  how large?
```

SA Strong Buy ≠ instant BUY.  
SA Sell + own technical bullish → **must not rescue** into Active / new entry.  
Hold → Research Candidate Pool, not Active, unless the user writes EXCEPTION REVIEW.  
Factor grades (Value/Growth/Profitability/Momentum/Revisions) are **explanatory only**. Forbidden: “Value D so veto Strong Buy.”  
Growth A+ is **not** a hard gate (that rule systematically kills miners, oil, banks, utilities).

Eligible Universe mechanical bar: US-listed, Alpaca-tradable common stock, market cap ≥ $5B, 20-day ADV ≥ $20M, not OTC, not leveraged ETF, not micro-cap.

Active Watchlist target: **16–20** names. No sector quota. A sector may contribute zero names.

---

## 2. Timeline of how the list was built

### Starting book (before this work)

```
AAPL MSFT NVDA GOOGL AMZN META TSLA JPM XOM JNJ WMT UNH
```

User complaint: too many traditional/low-growth names.

### Round 1 — “growth + AI” (later judged biased)

Method: logged-in Seeking Alpha Chrome (OpenCLI Browser Bridge). Screeners: Top Rated (Quant+Author+Wall St), Top Growth (mcap ≥ $1B, Growth A+, Quant Buy/SB), Top Semiconductor, Trending AI. Then symbol Summary pages for Quant + factor grades. X used only as confirmation/veto context, **not** allowed to upgrade Hold → Strong Buy.

Round-1 Mag7 / staples Quant (Summary page, ~2026-08-21 close):

| Ticker | SA Quant | Author | Wall St | Val | Growth | Profit | Mom | Rev | Round-1 fate |
|---|---|---|---|---|---|---|---|---|---|
| NVDA | Hold 3.49 | Buy 3.91 | SB 4.70 | D- | A+ | A+ | C+ | B- | **Kept** (override: growth A+, trend system) |
| MSFT | Hold 3.47 | Buy 4.21 | SB 4.63 | F | C- | A+ | B- | C | dropped |
| GOOGL | Hold 3.49 | Buy 4.14 | SB 4.64 | F | C+ | A+ | B- | A | dropped |
| AMZN | **SB 4.96** | Buy 4.18 | SB 4.68 | D+ | A- | A+ | B | B+ | kept |
| META | Hold 3.32 | Buy 3.97 | SB 4.64 | F | B | A+ | C- | C | dropped |
| AAPL | Hold 3.47 | Hold 3.19 | Buy 3.81 | F | **D-** | A+ | C+ | C+ | dropped |
| TSLA | Hold 3.21 | Hold 2.51 | Buy 3.65 | D- | B | A+ | **D** | C- | dropped |
| JPM | Hold 3.43 | Buy 3.77 | Buy 3.87 | D- | C- | **F** | B | A+ | dropped |
| XOM | **SB 4.87** | Buy 3.66 | Buy 3.68 | D | C- | A+ | B | B | dropped in Round 1 for “not growth”; **restored in Round 2** |
| JNJ | Hold 3.28 | Buy 3.50 | Buy 4.08 | F | D | A+ | B- | D- | dropped |
| WMT | Hold 3.28 | Hold 2.55 | Buy 4.41 | F | C- | A+ | D+ | C+ | dropped |
| UNH | Hold 3.48 | Buy 3.72 | Buy 4.44 | C- | D- | A+ | B- | A- | dropped |

Round-1 **applied** watchlist (this is what yaml contains **right now**):

```
MU SNDK AMD NVDA AMZN ANET CRDO ALAB NBIS HPE FLEX CIEN ARGX VEEV
```

User then objected: almost all Technology; Analysis sidebar (Energy, Materials, Gold, Healthcare, Education, etc.) was not used as the discovery surface.

### Round 2 — full Analysis sweep + SA Quant as sole quality gate

Agreed in `Downloads/读取文件并连接GitHub.pdf`. Key corrections vs Round 1:

1. SA Overall Quant is the **only** quality gate. Round-1 “keep NVDA despite Hold” is now an exception, not a default.
2. No “keep 7–8 tech” quota. Every current name **re-competes**.
3. Three layers: Eligible Universe → Research Candidate Pool (can be 30–50+) → Active Watchlist (16–20).
4. Do **not** edit `risk.yaml` / `buy_threshold` this round.
5. Analysis Latest = last 30 days **or** first ~30 articles, whichever first. Top tab is gap-fill only.
6. Article counts are **attention**, not quality.
7. After Analysis, do a market-based backfill: mcap ≥ $5B, ADV ≥ $20M, SA Quant Buy/SB, to catch names with little editorial coverage.
8. X/Web cannot upgrade SA Hold to Strong Buy.

**What was actually executed in Round 2**

- Phase A: froze `watchlist.yaml` / `sectors.yaml` / `risk.yaml` to `tmp/universe-recon-20260822/*.freeze`.
- Phase B: Chrome, logged-in Seeking Alpha. Clicked left-nav **Analysis** (`/latest-articles`). Then opened **23 category URLs** and saved Latest article lists + ticker mentions. No captcha during the category sweep.
- Education: SA site search `education edtech`. Tickers TAL, EDU (plus junk). DUOL added from mechanical list, not from that search.
- Alpha Picks / Energy Investing Authority: visible in the left PRO rail; **not** opened article-by-article (later PX).
- Phase D: opened symbol Summary pages and parsed Ratings Summary + Factor Grades for 30 names (ET through ISRG). **PerimeterX “Press & Hold” at RKLB**. Stopped. Did not click the widget.
- Eligibility: yfinance `fast_info` market cap + 20-day dollar ADV for ~130 candidates.
- Own timing: price vs SMA50, 20d momentum, RSI14, extended if px > 1.08×SMA20 and RSI>70.
- 60d return correlation among the shortlist. Recorded; no new haircut invented. Used only to avoid listing **both** XOM and CVX (0.84).
- Full GICS Quant screener backfill on SA’s website was **not** completed (PX). Backfill used Round-1 Top Growth/Top Rated plus the 30 Summary pages.

Prices/ADV/Quant as-of **2026-08-21 US regular-session close** unless noted.

---

## 3. Analysis coverage (Round 2)

Each category: ~20–21 Latest articles.

| Category | Notable tickers in Latest | How it affected Active |
|---|---|---|
| Energy | WES, HESM, ET, EPD, FRO, SHEL, UUUU, UEC, TRGP, EPD | ET, FRO entered after Quant SB. UUUU/UEC observation (small/dilution). |
| Basic Materials | PAAS, MP, AGI, ICL, TMCR, royalties | Latest skewed small. NUE entered via Quant SB (market backfill), not because it dominated Latest. |
| Gold & Precious Metals | GLD, FNV, PAAS, AGI | **No Active.** Miners Hold. GLD ETF not auto-added. |
| Commodities | CL1, XLE, SLV | Macro. Producers handled under Energy/Materials. |
| Consumer Staples | CLX, EL, WMT, CPB, STZ | No new large-cap SA Buy/SB taken. WMT previously Hold. |
| Consumer Goods | BABA, TSLA, NKE, AMZN | AMZN KEEP. TSLA previously Hold. |
| Healthcare | LLY, TEM, CNC, NBIX, MDT, NVO | CNC SB, NBIX Buy → Active. LLY/NVO Hold. |
| Biotech | TGTX, NBIX, RARE, MRNA | Mostly small. NBIX only large-enough Buy. |
| Weight Loss | NVO, LLY, HIMS | Hold / small. No Active. |
| Financials | JPM, MA, PSEC | JPM previously Hold. MA/V/BLK Quant **not retrieved**. |
| Industrials | RKLB, CDRE, FLR | Quant **not retrieved** (PX at RKLB). |
| Aerospace & Defense | RKLB, KTOS, GD, LMT | Quant not retrieved. |
| Utilities | NEE, AES, PEG, CMS | NEE page denied. No Active. |
| Real Estate / REITs | O, IRM | Quant not retrieved. EQIX/DLR eligible but no Quant. |
| Communication | META, GOOG, ASTS, VZ | META/GOOGL previously Hold. |
| Electric Vehicles | TSLA, RIVN, F | TSLA Hold. |
| Technology / AI / Mag7 | NVDA, FLEX, CRWV, MSFT | Check only. NVDA Hold → DEMOTE. |
| Undercovered / Long Ideas / Trending | TGTX, FLEX, CDRE, JPM, DOCU | Attention only. |
| Education search | TAL, EDU | DUOL/EDU/TAL pass mcap+ADV; Quant not retrieved → no Active. |

---

## 4. SA Quant table (retrieved)

### Round 2 Summary-page pulls (energy → healthcare, then PX)

| Ticker | Quant | Score | Val | Growth | Profit | Mom | Rev | Gate |
|---|---|---|---|---|---|---|---|---|
| ET | Strong Buy | 4.75 | A- | C | A | B- | B+ | Core |
| EPD | Hold | 3.37 | A- | D+ | A | C | B+ | Candidate |
| WES | Hold | 3.43 | A- | D | B+ | B- | A- | Candidate |
| TRGP | Hold | 3.40 | F | B+ | B+ | A- | A- | Candidate |
| SHEL | Hold | 3.43 | B- | D | A+ | B | B- | Candidate |
| FRO | Strong Buy | 4.82 | A | A | A- | A- | A+ | Core |
| CCJ | **Sell** | 2.30 | F | A | C | D | D+ | **Reject new entry** |
| CVX | Strong Buy | 4.89 | C | C+ | A+ | B | B+ | Core, **not** Active (0.84 corr vs XOM) |
| VLO | Strong Buy | 4.95 | D | A | A+ | A+ | A- | Core → Active |
| CNQ | Buy | 3.79 | C | C | A+ | B+ | C- | Core, not Active (corr vs XOM 0.72) |
| LNG | Hold | 3.14 | F | C- | A- | B+ | B- | Candidate |
| WMB | Hold | 3.39 | D+ | B+ | B- | D+ | A | Candidate |
| COP | Buy | 4.46 | D+ | C+ | A+ | B+ | B- | Core, not selected (energy already 4) |
| FCX | Hold | 3.46 | D- | B+ | A | A | A- | Candidate |
| NEM | Hold | 3.14 | D- | C | A+ | A- | C | Candidate |
| AEM | Hold | 2.95 | F | D+ | A+ | B+ | C- | Candidate |
| FNV | Hold | 2.91 | F | B- | A | B+ | C | Candidate |
| AGI | Hold | 2.64 | D | A- | A | D+ | D | Candidate |
| PAAS | Hold | 2.68 | D- | B+ | A+ | C+ | C- | Candidate |
| MP | Hold | 2.65 | D+ | A+ | D- | C | D+ | Candidate |
| SCCO | Hold | 3.43 | D- | C | A+ | A- | A- | Candidate |
| VALE | Hold | 3.15 | A+ | B | A+ | D+ | C- | Candidate |
| NUE | Strong Buy | 4.75 | D+ | B | A- | A | A | Core → Active |
| NVO | Hold | 3.21 | B | F | A+ | C | B- | Candidate |
| CNC | Strong Buy | 4.94 | A+ | A+ | A+ | A- | A- | Core → Active |
| MDT | Hold | 3.01 | C+ | F | A+ | C+ | D- | Candidate |
| NBIX | Buy | 3.59 | A- | A | A | C+ | C- | Core → Active |
| AMGN | Hold | 3.47 | D- | F | A+ | B | A- | Candidate |
| BSX | Hold | 2.63 | C+ | D- | A | D | D- | Candidate |
| ISRG | Hold | 3.25 | D | C+ | A+ | D | A+ | Candidate |
| RKLB | — | — | — | — | — | — | — | PX, not scored |

### Round 1 tech / Mag7 still used in Round 2 (not re-pulled)

| Ticker | Quant | Score | Notes |
|---|---|---|---|
| MU | SB | 4.99 | Rank 1 / 4271. Val A-, all other A/A+ |
| SNDK | SB | 4.99 | Rank 2. Val A+ |
| HPE | SB | 4.99 | Rank ~6 |
| AMD | SB | 4.97 | Authors Hold 3.42; Overall still SB |
| AMZN | SB | 4.96 | Only Mag7 SB |
| NBIS | SB | 4.94 | Convertible raise discussed on X; Quant still SB |
| ALAB | SB | 4.91 | Authors Hold; Overall SB |
| ANET | SB | 4.90 | Growth only B- |
| CRDO | SB | 4.88 | Top Rated (all three Buy/SB) |
| VEEV | SB | 4.81 | Growth C |
| FLEX | SB | 4.64 | Top Rated |
| ARGX | SB | 4.70 | |
| CIEN | SB | 4.52 | |
| NVDA | **Hold** | 3.49 | Growth A+, Val D- |
| INTC | SB | 4.98 | Authors/WS Hold — Candidate, not Active |
| STX | SB | 4.98 | Storage overlap with MU/SNDK |
| WDC | SB | 4.97 | Same |
| XOM | SB | 4.87 | Restored in Round 2 |

---

## 5. Eligibility (yfinance, 20d ADV, mcap ≥ $5B)

All 16 proposed Active names passed. Examples (close 2026-08-21):

| Ticker | px | mcap $B | ADV $M |
|---|---|---|---|
| MU | 966.78 | 1092 | 34551 |
| SNDK | 1596.08 | 234 | 23482 |
| AMD | 473.25 | 773 | 12803 |
| AMZN | 258.63 | 2790 | 12559 |
| ANET | 188.65 | 238 | 1540 |
| HPE | 53.45 | 71 | 871 |
| NBIS | 219.13 | 60 | 6243 |
| ARGX | 1039.78 | 65 | 283 |
| VEEV | 247.90 | 40 | 397 |
| VLO | 348.86 | 100 | 817 |
| XOM | 165.11 | 679 | 2251 |
| ET | 21.19 | 73 | 209 |
| FRO | 43.67 | 10 | 77 |
| NUE | 243.63 | 55 | 434 |
| CNC | 65.02 | 32 | 319 |
| NBIX | 152.57 | 16 | 240 |

Failed mechanical bar (examples): CHGG (mcap $0.09B), COUR ($1.7B), LOPE ($3.8B), GOLD-as-ticker ($1.3B). UUUU/UEC never treated as Active even though Energy Latest featured them.

Education mechanical pass without Quant: DUOL $6.8B ADV $196M; EDU $9.3B ADV $53M; TAL $6.3B ADV $66M.

---

## 6. Own timing (does not change membership)

Rule used: POOR ENTRY if price < SMA50; WATCH if extended (px > 1.08×SMA20 and RSI>70); else ACTIVE NOW.

| Ticker | vs SMA50 | 20d mom | RSI14 | Timing |
|---|---|---|---|---|
| MU | +0.2% | +5.0% | 68.8 | ACTIVE NOW |
| SNDK | −3.5% | +11.1% | 63.0 | POOR ENTRY |
| AMD | −7.2% | −9.3% | 47.1 | POOR ENTRY |
| AMZN | +3.6% | +11.4% | 24.5 | ACTIVE NOW |
| ANET | +6.4% | +8.4% | 52.3 | ACTIVE NOW |
| HPE | +9.0% | +12.1% | 58.3 | ACTIVE NOW |
| NBIS | −1.8% | +16.7% | 51.5 | POOR ENTRY |
| ARGX | +16.2% | +13.2% | 87.1 | WATCH |
| VEEV | +25.4% | +33.1% | 77.2 | WATCH |
| VLO | +19.3% | +15.8% | 74.9 | WATCH |
| XOM | +11.5% | +5.9% | 70.1 | ACTIVE NOW |
| ET | +7.5% | +5.8% | 72.5 | ACTIVE NOW |
| FRO | +12.2% | +11.1% | 64.9 | ACTIVE NOW |
| NUE | −1.8% | −1.6% | 36.3 | POOR ENTRY |
| CNC | +0.1% | +2.5% | 53.1 | ACTIVE NOW |
| NBIX | −8.6% | −13.2% | 29.8 | POOR ENTRY |

SB + POOR ENTRY still belongs on Active; the live cycle would WAIT, not BUY.

---

## 7. 60-day return correlation (record only)

Pairs ≥ 0.60 among the shortlist:

- MU–SNDK **0.87**
- CVX–XOM **0.84** → Active keeps XOM, CVX stays Candidate
- SNDK–AMD 0.80, MU–AMD 0.79
- CVX–CNQ 0.78, XOM–CNQ 0.72 → CNQ not Active
- XOM–ET 0.65, CVX–ET 0.65
- MU–ANET 0.65 and several ~0.61–0.62 AI-infra links

**Known tension for the reviewer:** MU+SNDK were both kept at 0.87, while CVX was excluded vs XOM at 0.84. Stated rationale: HBM vs NAND are different products; CVX vs XOM are two integrated oils. That is a judgment, not a numeric rule.

---

## 8. Final proposed Active (NOT yet in yaml)

```
MU SNDK AMD AMZN ANET HPE NBIS ARGX VEEV VLO XOM ET FRO NUE CNC NBIX
```

Count by GICS: Technology 7, Energy 4, Healthcare 4, Basic Materials 1.

### KEEP / ADD / DEMOTE / REMOVE vs current yaml

Current yaml: `MU SNDK AMD NVDA AMZN ANET CRDO ALAB NBIS HPE FLEX CIEN ARGX VEEV`

| Action | Names | Rule used |
|---|---|---|
| KEEP | MU SNDK AMD AMZN ANET HPE NBIS ARGX VEEV | SA SB + eligible + selected |
| ADD | VLO XOM ET FRO NUE CNC NBIX | SA SB or Buy + eligible |
| DEMOTE | NVDA | SA Hold (quality gate) |
| DEMOTE | CRDO ALAB FLEX CIEN | Still SA SB; dropped from Active for AI-hardware concentration |
| REMOVE | (none) | No Active name was SA Sell |
| Reject new | CCJ | SA Sell |
| Candidate (Hold) | NEM AEM FNV AGI PAAS FCX SCCO MP VALE LLY NVO JPM MSFT GOOGL META AAPL TSLA WMT UNH EPD WES TRGP SHEL … | Quality gate |
| Unscored, not Active | NEE CEG CAT GE GEV VRT GD LMT MA V EQIX DLR DUOL EDU TAL RKLB | PX |

---

## 9. Explicit non-actions

Did not change: `buy_threshold`, RSI/MACD weights, ATR, trailing stop, LLM architecture, sizing, TRIM, shorting, options, 2007 backtest universe, `risk.yaml` sector caps.

Did not add GLD/SLV/futures.  
Did not put a name on Active because “gold/oil/education should have a slot.”  
Did not let X upgrade Hold → Strong Buy (NBIS $4.5B convert was noted, Quant remained SB, still KEEP).

---

## 10. Gaps the reviewer must treat as coverage holes

1. **PerimeterX** cut Quant retrieval at RKLB. Industrials, defense, utilities, most financials, REITs, education have Analysis coverage but **no Overall Quant** in this packet.
2. No full SA screener dump of every Quant Buy/SB with mcap ≥ $5B (the market-based backfill is partial).
3. Alpha Picks / Energy Investing Authority not read in Round 2.
4. Own timing is a simplified SMA50/RSI proxy, not the live `signals/technical.py` composite.
5. Alpaca tradability was assumed for liquid NYSE/Nasdaq names; not re-queried against the live asset list in this packet.

---

## 11. File map (if you want raw artifacts)

| What | Path |
|---|---|
| **This pack (copy this to GPT)** | `C:\Users\helow\Documents\Trading\research\GPT-REVIEW-PACK-2026-08-22.md` |
| Round-2 decision note | `research\universe-2026-08-22.md` |
| Round-1 growth pool | `research\growth-pool-2026-08-22.md` |
| Agreed spec PDF | `C:\Users\helow\Downloads\读取文件并连接GitHub.pdf` |
| Current live watchlist | `config\watchlist.yaml` |
| Frozen yaml before Round 2 | `tmp\universe-recon-20260822\*.freeze` |
| Per-category article JSON | `tmp\universe-recon-20260822\cats\*.json` |
| Quant page extracts | `tmp\universe-recon-20260822\quant.jsonl` |
| ADV/mcap | `tmp\universe-recon-20260822\elig.json` |
| Timing | `tmp\universe-recon-20260822\timing.json` |
| Session plan | `C:\Users\helow\.grok\sessions\C%3A%5CUsers%5Chelow%5CDocuments%5CTrading\01a02a6b-e0b0-7fc0-8efb-99701a38d380\plan.md` |

There is **no** exported full chat log in the Trading folder. Grok session files are under `C:\Users\helow\.grok\sessions\...` and are not a clean human transcript.

---

## 12. One-sentence claim to audit

Seeking Alpha Overall Quant decides what is allowed on the swing universe; Analysis discovers names across all sectors; own technicals only time entries; the proposed Active 16 is the Buy/Strong Buy subset that cleared $5B / $20M ADV after a 23-category scan, minus NVDA (Hold) and four extra AI-hardware Strong Buys, plus energy/steel/managed-care/biotech Strong Buys, with gold/uranium/utilities/education/defense left off because Quant was Hold, Sell, or missing.
