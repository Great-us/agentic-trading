# GPT Review Pack — KIMI COMPLETE (Round 2 finished)

**Date:** 2026-08-22
**System:** US-equities long-only paper swing agent (`C:\Users\helow\Documents\Trading`)
**Supersedes:** `GPT-REVIEW-PACK-2026-08-22.md` (Grok, incomplete — PX cut at RKLB)
**Repo yaml status:** `config/watchlist.yaml` NOT yet updated. Live list is still the Round-1 growth pool (14 names incl. NVDA Hold). This pack only **proposes** the final Active 20; yaml diff at the end awaits user approval.

---

## 0. Change log vs the Grok pack

1. **PX-gap Quant backfilled.** All 26 previously unscored names were pulled from Summary pages via the logged-in browser (serial, paced): RKLB GD LMT CAT GE GEV VRT NEE CEG MA V BLK EQIX DLR DUOL EDU TAL KTOS CDRE FLR O IRM AES PEG CMS. No Press & Hold encountered this run. One transient PX interstitial on GD (first attempt) was detected, discarded, and re-pulled clean.
2. **Real market-wide backfill done.** 13 SA per-sector screeners ranked by Quant Rating scraped in full: top-rated-all, energy, materials, industrials, consumer-disc, healthcare, financials, communication, utilities, staples, real-estate, REITs, technology → **1180 rows**. Method note: sector screeners list the top ~100 per sector by Quant score; sectors with >100 Buy/SB names are truncated at the Buy tail — recorded as the only known coverage limit, not silent.
3. **Eligibility actually verified.** For all 700 Quant Buy/SB candidates: 20d dollar ADV (yfinance batch, close 2026-08-21) and market cap (fast_info). 253 passed mcap ≥ $5B + ADV ≥ $20M.
4. **Alpaca tradability actually verified** against the live `/v2/assets/{symbol}` API for all 253. 252 tradable; only **SAFRY** (OTC ADR) not tradable → excluded.
5. **Active re-selected from zero** out of the full 252-name Core Candidate Pool. Previous provisional 16 was **not** used as a base.
6. **Status taxonomy applied:** KEEP / ADD / QUALITY DEMOTE / QUALITY REJECT / CAPACITY HOLDOUT / ELIGIBILITY FAIL. No more UNSCORED names among the plan's list.
7. **New sells found:** CDRE Quant **Sell 1.82** → QUALITY REJECT (with CCJ Sell 2.30).

## 1. Rules applied (unchanged, restated for the reviewer)

```
SA Overall Quant  → quality gate (SB/Buy = Core; Hold = research only; Sell/SS = reject new entry)
mcap ≥ $5B + 20d ADV ≥ $20M + Alpaca tradable → eligibility
Own technicals    → entry timing only; does NOT decide Active membership
Risk manager      → sizing (60d corr > 0.75 → size × 0.5; unchanged)
```

No Hold was upgraded by authors/Wall Street/X. No Sell was rescued. Factor grades recorded as explanatory only. No sector quota. Correlation recorded, **not** used as a watchlist hard cutoff (the MU–SNDK 0.87 vs XOM–CVX 0.84 tension from the Grok pack is resolved by arguing **business duplication**, never a numeric corr rule — see exclusions).

## 2. What the PX-gap sectors actually scored

The missing sectors competed and mostly lost — this is now verified fact, not coverage bias:

| Ticker | Quant | Score | Gate result |
|---|---|---|---|
| LMT | **Strong Buy** | 4.56 | Core → **Active** |
| TAL | **Strong Buy** | 4.59 | Core → **Active** |
| FLR | Buy | 3.97 | Core (CAPACITY HOLDOUT) |
| GD | Hold | 3.40 | Research only |
| CAT | Hold | 3.42 | Research only |
| GE | Hold | 3.45 | Research only |
| GEV | Hold | 3.23 | Research only |
| VRT | Hold | 3.27 | Research only |
| NEE | Hold | 3.12 | Research only |
| CEG | Hold | 3.01 | Research only |
| MA | Hold | 3.46 | Research only |
| V | Hold | 3.44 | Research only |
| BLK | Hold | 3.43 | Research only |
| EQIX | Hold | 2.87 | Research only |
| DLR | Hold | 3.17 | Research only |
| DUOL | Hold | 2.99 | Research only |
| EDU | Hold | 2.91 | Research only |
| RKLB | Hold | 2.73 | Research only |
| KTOS | Hold | 2.82 | Research only |
| O | Hold | 2.83 | Research only |
| IRM | Hold | 3.06 | Research only |
| AES | Hold | 2.94 | Research only |
| PEG | Hold | 2.98 | Research only |
| CMS | Hold | 2.76 | Research only |
| CDRE | **Sell** | 1.82 | QUALITY REJECT |

## 3. Market-wide backfill result

Core Candidate Pool = Analysis-discovered ∪ screener backfill, Quant Buy/SB + eligible: **252 names** (`research/core-candidate-pool-2026-08-22.csv`, full table with score, factors, mcap, ADV, Alpaca, sources).

Eligible Buy/SB by GICS sector (top by score):

| Sector | n | Strongest names |
|---|---|---|
| Information Technology | 52 | SNDK/MU/HPE 4.99, INTC/STX 4.98, AMD/WDC 4.97, NBIS 4.94, ALAB 4.91, ANET 4.90 |
| Financials | 39 | OSCR 4.97, BAC 4.95, WFC 4.87, PNC 4.84, ALL 4.83, STT 4.78 |
| Industrials | 36 | ATI 4.82, SWK 4.76, DAL 4.73, UBER 4.72, CLH 4.60, LMT 4.56, FLR 3.97 |
| Consumer Discretionary | 29 | AMZN 4.96, F 4.89, KMX 4.84, GM 4.80, TAL 4.59 |
| Energy | 21 | PBF 4.98, MPC 4.96, VLO 4.95, PSX 4.94, CVX 4.89, XOM 4.87, FRO 4.82, ET 4.75 |
| Consumer Staples | 16 | DAR 4.89, ADM 4.73, USFD 4.70, DLTR 4.61 |
| Materials | 15 | NUE 4.75, GEF 4.71, TX 4.71, CLF 4.66 |
| Health Care | 14 | CNC 4.94, CORT 4.94, TXG 4.94, HALO 4.83, VEEV 4.81, ARGX 4.70, NBIX 3.59 |
| Communication Services | 12 | NIQ 4.91, SKM 4.74, MTCH 4.41, TMUS 4.41, RDDT 4.05 |
| Real Estate | 10 | JLL 4.82, CTRE 4.75, HST 4.71 |
| Utilities | 9 | SO 3.94 (best; all Buy < 4.0, zero Strong Buy) |

New Buy/SB names the backfill found that Analysis had **not** surfaced (examples): BAC, WFC, PNC, OSCR, UBER, DAL, SWK, ATI, CORT, TXG, HALO, DAR, ADM, NIQ, JLL, HST, F, GM, PBF, MPC, PSX, DINO. Utilities/defense/big payments (SO, GD, MA, V) confirmed Hold — no hidden SB missed in those lanes.

## 4. Final Active Watchlist proposal (20, re-selected from zero)

Selection rules applied in order: (1) SA Quant strength (not strict-linear), (2) no two names with the same business exposure, (3) marginal new exposure preferred, (4) capacity 20.

```
MU  SNDK  HPE  AMD  AMZN  NBIS  ANET        # AI/compute & cloud (distinct sub-industries)
VLO XOM   FRO  ET                           # energy: refiner / integrated / tanker / midstream
CNC CORT  VEEV ARGX                         # health: managed care / endocrine biotech / life-sci SW / biotech
BAC                                        # financials: money-center bank
LMT UBER                                   # industrials: defense / mobility platform
NUE                                        # materials: steel
TAL                                        # education (China) — user's requested theme, SB 4.59
```

GICS distribution: Information Technology 6, Consumer Discretionary 2 (AMZN, TAL), Energy 4, Health Care 4, Industrials 2, Financials 1, Materials 1. Utilities / Staples / Comm Svcs / Real Estate = 0 — their candidates competed and lost on capacity + score-vs-duplication (see exclusions). No quota was applied in either direction.

### Actions vs current live yaml (14 names)

| Action | Names | Reason |
|---|---|---|
| KEEP | MU SNDK AMD AMZN ANET HPE NBIS ARGX VEEV | SB ≥ 4.70, eligible, distinct exposure |
| ADD | VLO XOM ET FRO NUE CNC | SB, eligible (unchanged from Grok provisional) |
| ADD | CORT | SB 4.94, new backfill find; replaces NBIX as 4th health slot |
| ADD | BAC | SB 4.95; Financials exposure the provisional 16 lacked |
| ADD | LMT | SB 4.56; defense exposure |
| ADD | UBER | SB 4.72 + Authors SB 4.52; mobility platform |
| ADD | TAL | SB 4.59; education exposure |
| QUALITY DEMOTE | NVDA | SA Hold 3.49. Growth A+ does not rescue (Round-1 override废止) |
| CAPACITY HOLDOUT | CRDO ALAB FLEX CIEN | Still SA SB; excluded on AI-hardware concentration + capacity. **Not** a quality verdict |
| QUALITY REJECT | CCJ (Sell 2.30), CDRE (Sell 1.82) | No new entry |

### Key excluded Buy/SB and why (all remain Core Candidates)

| Name | Score | Exclusion reason |
|---|---|---|
| INTC | SB 4.98 | Capacity + large-cap-semis duplication with AMD; Authors/WS Hold noted (not the veto reason — Overall Quant is the gate, duplication is). Core Candidate. |
| STX / WDC | SB 4.98 / 4.97 | Storage duplication with SNDK/MU. |
| PBF / MPC / PSX / DINO | SB 4.98–4.86 | Refiner duplication; VLO kept (largest/most liquid of the group; scores not treated as strict-linear). |
| CVX | SB 4.89 | Integrated-oil duplication with XOM (business reason; 60d corr 0.84 recorded, not the criterion). |
| COP / CNQ / PBR / YPF | Buy/SB | E&P/integrated duplication; energy capacity already 4 distinct sub-industries. |
| TXG / HALO | SB 4.94 / 4.83 | Idiosyncratic-biotech/tools duplication; health capacity 4 (CNC/CORT/VEEV/ARGX). |
| NBIX | Buy 3.59 | Weakest health Buy; CORT (SB 4.94) carries the same idiosyncratic-biotech factor at much higher Quant. CAPACITY HOLDOUT, not a quality demote. |
| WFC / PNC / STT | SB | Bank duplication with BAC. |
| OSCR | SB 4.97 | Managed-care duplication with CNC, smaller/riskier. |
| DAL / UAL / AAL / LUV | SB | Airline duplication; UBER chosen (Authors SB 4.52 vs DAL Authors Hold). |
| ATI / SWK / CLH / FLR | SB/Buy | Industrial holdouts; LMT (defense) + UBER (platform) give more distinct exposure. |
| F / GM | SB 4.89 / 4.80 | Autos — cyclical duplication; ConsDisc already has AMZN + TAL. |
| DAR / ADM | SB | Staples holdouts — capacity; recorded as the sector's best (DAR 4.89). |
| NIQ / SKM | SB | Comm-services holdouts — capacity. |
| JLL / CTRE / HST | SB | Real-estate holdouts — capacity. |
| SO | Buy 3.94 | Best utility; no utility SB exists. Capacity. |

## 5. Own timing (record only — does not affect membership)

Close 2026-08-21. Rule: POOR ENTRY if px < SMA50; WATCH if extended (px > 1.08×SMA20 & RSI > 70).

| Ticker | vs SMA50 | 20d mom | RSI14 | Timing |
|---|---|---|---|---|
| MU | +0.2% | +5.0% | 68.8 | ACTIVE NOW |
| SNDK | −3.5% | +11.1% | 63.0 | POOR ENTRY |
| HPE | +9.0% | +12.1% | 58.3 | ACTIVE NOW |
| AMD | −7.2% | −9.3% | 47.1 | POOR ENTRY |
| AMZN | +3.6% | +11.4% | 24.5 | ACTIVE NOW |
| NBIS | −1.8% | +16.7% | 51.5 | POOR ENTRY |
| ANET | +6.4% | +8.4% | 52.3 | ACTIVE NOW |
| VLO | +19.3% | +15.8% | 74.9 | WATCH |
| XOM | +11.5% | +5.9% | 70.1 | ACTIVE NOW |
| FRO | +12.2% | +11.1% | 64.9 | ACTIVE NOW |
| ET | +7.5% | +5.8% | 72.5 | ACTIVE NOW |
| CNC | +0.1% | +2.5% | 53.1 | ACTIVE NOW |
| CORT | +25.6% | +28.2% | 66.3 | ACTIVE NOW |
| VEEV | +25.4% | +33.1% | 77.2 | WATCH |
| ARGX | +16.2% | +13.2% | 87.1 | WATCH |
| BAC | +1.9% | −0.6% | 44.7 | ACTIVE NOW |
| LMT | +2.7% | −3.3% | 42.2 | ACTIVE NOW |
| UBER | +8.1% | +19.5% | 65.1 | ACTIVE NOW |
| NUE | −1.8% | −1.6% | 36.3 | POOR ENTRY |
| TAL | +6.6% | +9.9% | 37.2 | ACTIVE NOW |

SB + POOR ENTRY stays Active; the live cycle WAITs. Eligibility of new names (close 2026-08-21): CORT mcap $13.2B ADV $129M; BAC $431B / $1,751M; LMT $130B / $649M; UBER $161B / $1,483M; TAL $6.3B / $66M. All Alpaca-verified tradable.

## 6. 60d correlation (record only)

Pairs ≥ 0.60 within the proposed Active: MU–SNDK **0.87**; SNDK–AMD 0.80; MU–AMD 0.79; MU–ANET 0.65; XOM–ET 0.65; AMD–ANET 0.62; NBIS–ANET 0.62; SNDK–NBIS 0.61; SNDK–ANET 0.61.

No new cutoff invented. If MU and SNDK both fire BUY, the existing risk-layer haircut (corr > 0.75 → ×0.5) handles sizing. MU–SNDK kept together: HBM/DRAM vs NAND are different products (business rationale, stated explicitly).

## 7. Explicit non-actions (unchanged from Grok pack)

Not touched: `buy_threshold` 0.35, RSI/MACD/momentum weights, ATR, trailing stop, LLM architecture, sizing, correlation haircut, sector caps, `risk.yaml`, shorting, options, TRIM, 2007 backtest universe. No GLD/SLV/futures added. No name put on Active to fill a sector slot.

## 8. Remaining known limits (honest list)

1. Sector screeners truncate at ~100 names/sector — the Buy tail (score ≲ 3.6) in big sectors is partially unseen. Impact on a 20-name Active list: nil; impact on the completeness of the 252-name Core CSV: minor.
2. Alpha Picks / Energy Investing Authority (PRO) still not read article-by-article — deprioritized per handoff §18.
3. Screener-row Quant scores are same-day table values; Summary-page factor grades exist for the 55 summary-pulled names, not for screener-only holdouts (Quant score itself is present for all).
4. Own timing here is the SMA50/RSI proxy, not the live `signals/technical.py` composite — same caveat as Grok pack.
5. TAL: China-education regulatory risk is real; SA Authors rate it Hold 3.00 while Overall Quant is SB 4.59. Included on the Quant gate; flagged for the user's discretionary veto.

## 9. Proposed yaml diff (NOT yet applied)

`config/watchlist.yaml` — replace `symbols:` with the 20 above; `context_symbols` unchanged (SPY, QQQ).

`config/sectors.yaml` — add (Yahoo-style names, matching file convention):
```
VLO: Energy
FRO: Energy
ET: Energy
CNC: Healthcare
CORT: Healthcare
BAC: Financial Services
LMT: Industrials
UBER: Industrials
NUE: Basic Materials
TAL: Consumer Defensive
```
Keep existing entries (incl. NVDA and the demoted/holdout names) so leftover positions keep sector caps.

`config/risk.yaml` — **no change**. Note for the user, not a rule change: Financials/Industrials/Materials currently fall to `default_max_sector_pct` 0.35 — fine for 1–2 names each.

## 10. File map (Kimi additions)

| What | Path |
|---|---|
| This pack | `research/GPT-REVIEW-PACK-2026-08-22-KIMI-COMPLETE.md` |
| Full 252-name Core Candidate Pool | `research/core-candidate-pool-2026-08-22.csv` |
| Kimi summary-page pulls (raw) | `tmp/universe-recon-20260822/quant_kimi.jsonl` |
| 13-screener dump (raw, 1180 rows) | `tmp/universe-recon-20260822/screener_dump.json` |
| Merged candidates (1293) | `tmp/universe-recon-20260822/merged_candidates.json` |
| Eligibility (700 Buy/SB) | `tmp/universe-recon-20260822/elig2.json` |
| Alpaca verification (253) | `tmp/universe-recon-20260822/alpaca_tradable.json` |
| Timing + corr for proposed 20 | `tmp/universe-recon-20260822/timing_corr_kimi.json` |

---

## One-sentence claim to audit

Every sector competed with its real Quant score this time; the Active 20 is the highest-quality, non-duplicated, eligible, Alpaca-tradable subset of a verified 252-name pool — with NVDA demoted on Hold, CCJ/CDRE rejected on Sell, and every excluded Strong Buy logged as a capacity/duplication holdout rather than a pretend quality failure.
