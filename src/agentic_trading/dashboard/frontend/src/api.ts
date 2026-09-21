/** Typed client for the read-only dashboard API. */

export interface BookSummary {
  id: string;
  display_name: string;
  kind: string;
  root: string;
  last_cycle_at?: string | null;
  equity?: number | null;
  cash?: number | null;
  regime_label?: string | null;
  regime_score?: number | null;
  open_positions?: number | null;
  broker_error?: string;
}

export interface EquityPoint {
  date: string;
  equity: number | null;
  cash: number | null;
  regime_label: string | null;
  drawdown?: number;
}

export interface CycleRow {
  id: number;
  timestamp: string;
  mode: string;
  equity: number | null;
  regime_label: string | null;
  n_decisions: number;
}

export interface DecisionRow {
  id: number;
  cycle_id: number;
  cycle_time: string;
  mode: string;
  symbol: string;
  quant_score: number | null;
  combined_score: number | null;
  action: string;
  reasoning: string | null;
  sizing_reason: string | null;
  llm_stance: string | null;
  llm_confidence: number | null;
  llm_rationale: string | null;
  llm_risk_flags: string | null;
  llm_evidence_quality: string | null;
  grok_stance: string | null;
  grok_confidence: number | null;
  order_status: string | null;
  order_qty: number | null;
  fill_price: number | null;
  notional: number | null;
  stop_price: number | null;
}

export interface PositionRow {
  symbol: string;
  qty: number;
  avg_entry_price?: number;
  current_price?: number;
  market_value?: number;
  unrealized_pl?: number;
  unrealized_plpc?: number;
  stop_price?: number | null;
  high_water_mark?: number | null;
  stop_distance_pct?: number | null;
  giveback_pct?: number | null;
  source: string;
  last_qty?: number;
  last_value?: number;
  implied_last_price?: number;
}

export interface TradeTrip {
  symbol: string;
  opened_at: string | null;
  closed_at: string | null;
  qty: number;
  avg_entry: number | null;
  avg_exit: number | null;
  // Null for an incomplete/orphaned trip (round_trips.py: a sell with no
  // matching buy in the fills window, or the oversold remainder of one) —
  // there is no real P&L to report, not a $0 result.
  realized_pnl: number | null;
  unrealized_pnl?: number | null;
  open: boolean;
  incomplete?: boolean;
  ambiguous?: boolean;
}

export interface RosterPayload {
  available: boolean;
  reason?: string;
  evening?: RosterData | null;
  midday?: RosterData | null;
}

export interface RosterData {
  round: string | null;
  generated_at: string | null;
  pool_size: number | null;
  buyable: string[];
  held: string[];
  added: string[];
  removed: string[];
  rows: {
    rank: number; symbol: string; rs: number; ex_20d: string; ex_60d: string;
    last: number; streak: number; buyable: boolean;
  }[];
}

export interface LogicPayload {
  risk: Record<string, unknown>;
  watchlist: string[];
  context_symbols: string[];
  pipeline: [string, string][];
}

export interface HealthPayload {
  status: string;
  message: string;
  note?: string | null;
  // The most recent cycle that actually reconciled stops (whichever mode),
  // per heartbeat.latest_stop_check — this is what `overall`/`status` bases
  // its stops_unknown alert on, so it's the one to display; the per-mode
  // stops_covered/stops_unknown below are each mode's own last check and no
  // longer drive `overall` (P0-B-2/R2 follow-up).
  stop_check?: StopCheck | null;
  deep: HealthMode;
  fast: HealthMode;
  limits: { deep_hours: number; fast_minutes: number };
}

export interface StopCheck {
  checked_at: string | null;
  mode: string | null;
  cycle_id: number | null;
  stops_covered: number | null;
  positions: number | null;
  unknown: boolean;
  reason: string | null;
  naked: boolean;
}

export interface HealthMode {
  timestamp: string | null;
  cycle_id: number | null;
  age_seconds: number | null;
  missed_sessions: number | null;
  stops_covered: number | null;
  positions: number | null;
  naked?: boolean;
  stops_unknown?: boolean;
  stops_unknown_reason?: string | null;
  late_minutes?: number | null;
  missed_slots?: number | null;
  late?: boolean;
  note?: string | null;
  state: string;
}

export interface IntentRow {
  symbol: string;
  created_at: string;
  not_before: string | null;
  signal_price: number;
  quant_score: number | null;
  combined_score: number | null;
  reasoning: string | null;
  ttl_hours: number | null;
  status: string;
}

export interface VetoRow {
  label: string;
  recent: number;
  total: number;
  source: string;
  kind?: string;
}

export interface ProgressCard {
  schema_version?: number;
  book_id?: string;
  written_at?: string;
  round?: { name: string; round_id: string; asof: string; status: string };
  did?: string[];
  did_not?: string[];
  broker?: { equity: number | null; cash: number | null; n_positions: number; n_open_orders: number; degraded: boolean; reason?: string | null };
  risk?: { exposure_pct: number | null; stops_covered: number | null; stops_total: number | null; locks: string[] };
  unresolved?: { kind: string; detail: string | null }[];
  next_job?: { slot: string; when_et: string | null; when: string | null; instruction: string };
}

export interface TodayPayload {
  session_date: string;
  calendar_today: string;
  weekend: boolean;
  health: HealthPayload;
  fills_today: { symbol: string; side: string; qty: number; price: number; transaction_time: string }[];
  fills_degraded: boolean;
  fills_reason: string | null;
  intents: IntentRow[];
  intent_events: { timestamp: string; symbol: string; kind: string; deferred: number; detail: string | null }[];
  vetoes: VetoRow[];
  holdings: (PositionRow & { in_pool: boolean; bucket: string; quant_score: number | null; combined_score: number | null; last_action?: string })[];
  fail_closed_today: number;
  outcomes: { action: string; n: number; n_1d?: number; n_5d?: number; n_20d?: number; ret_1d: number | null; ret_5d: number | null; small_sample: boolean }[];
  notes: Record<string, string>;
  book_kind?: string;
  progress?: { present: boolean; card: ProgressCard | null };
}

// ---- A-4 (c2c_a7e2 §五): execution funnel --------------------------------------
// Mirrors dashboard/views.py funnel_summary(). EVERY field is nullable: an
// old journal returns schema_degraded with only legacy_event_counts, failed or
// truncated fills surface as degraded flags instead of "no fills", and old
// identity-less rows appear only as degraded counters. Rendering must never
// assume a non-null value and never read absence of evidence as zero.

export interface FunnelSizingDiagnostics {
  cash_available?: number | null;
  exposure_room?: number | null;
  position_cap?: number | null;
  stop_risk_room_pct?: number | null;
  stop_risk_room_notional?: number | null;
  // Upstream already took min(sector, theme) — this is the effective room,
  // not a pure sector limit.
  sector_theme_room?: number | null;
  pre_haircut_notional?: number | null;
  post_haircut_notional?: number | null;
  available_notional?: number | null;
  min_position_notional?: number | null;
  binding_constraints?: string[] | null;
  reject_code?: string | null;
}

// Echoed from the event payload that produced it — never recomputed at page
// load (PLAN §五: the number comes from the attempt's own sizing snapshot).
// R8 gap (honest): _funnel_sizing_snapshot() exposes only the EVENT KIND — no
// snapshot timestamp and no attempt/run id — so the page names the event and
// must not invent a time or attempt number. See the review handoff: adding
// event ts/attempt_id to the snapshot is a backend contract change.
export interface FunnelSizingSnapshot {
  event_kind?: string | null;
  sizing?: FunnelSizingDiagnostics | null;
  reason?: string | null;
  cash_available?: number | null;
  available_notional?: number | null;
  min_notional?: number | null;
  event_ts?: string | null;
  attempt_id?: string | null;
}

export interface FunnelOrder {
  order_id?: string | null;
  client_order_id?: string | null;
  submitted_at?: string | null;
  submit_status?: string | null;
  // R5/R8: the backend's own state fields — the page must never re-derive
  // meaning from a status string. phase ∈ complete | partial |
  // terminal_unfilled | submit_rejected | submitted | submit_unknown | unknown;
  // submit_phase ∈ submitted_accepted | submit_rejected | submit_unknown.
  phase?: string | null;
  submit_phase?: string | null;
  last_observation_kind?: string | null;
  observed_at?: string | null;
  last_observation_failed?: boolean | null;
  // Broker status; accepted is NOT filled (PLAN §一/§五).
  broker_status?: string | null;
  terminal_status?: string | null;
  filled_evidence?: boolean | null;
  partial_evidence?: boolean | null;
  filled_qty?: number | null;
  filled_avg_price?: number | null;
  // null = the fills feed itself was unreadable; false = readable, no match
  // (absence of evidence, not evidence of zero fills).
  fills_matched?: boolean | null;
  fills_qty?: number | null;
  fills_notional?: number | null;
  fills_unavailable?: boolean | null;
}

interface FunnelChainBase {
  stage?: string | null;
  reason?: string | null;
  // Attempts (R6) = distinct runs doing flush-stage work; creation and
  // status-observation runs are counted separately and listed separately.
  attempts?: number | null;
  created_runs?: number | null;
  observed_runs?: number | null;
  wait_events?: number | null;
  orders?: FunnelOrder[] | null;
  sizing?: FunnelSizingSnapshot | null;
  // R8: the backend's intent-lifecycle classification — views.py
  // _funnel_classify (waiting / discarded / cleared / created_pending /
  // submitted / submit_rejected / submit_unknown / other) plus the
  // chain-level overrides superseded / not_created / evidence_missing.
  // The page may only say 意图保留 for waiting/created_pending; a partial
  // order phase is NOT terminal.
  classification?: string | null;
  // R5: the backend's explicit chain/order-level truth and replacement mark.
  phase?: string | null;
  superseded_by?: string | null;
}

export interface FunnelChain extends FunnelChainBase {
  decision_key?: string | null;
  run_id?: string | null;
  symbol?: string | null;
  first_event_at?: string | null;
  last_event_at?: string | null;
  intent_id?: string | null;
  replaced_previous_version?: string | null;
}

// Intents created on an earlier ET day that keep flushing today — displayed
// separately and never counted in the session denominator.
export interface FunnelCarryover extends FunnelChainBase {
  intent_id?: string | null;
  symbol?: string | null;
  created_on?: string | null;
  created_by_decision?: string | null;
}

// R4/R5: today's fills whose order_id matches no ledger event — listed as
// positive evidence, never fabricated into a chain and never in the cohort.
export interface FunnelUnlinkedFill {
  activity_id?: string | null;
  order_id?: string | null;
  symbol?: string | null;
  side?: string | null;
  qty?: number | null;
  notional?: number | null;
  transaction_time?: string | null;
  fill_status?: string | null;
  note?: string | null;
}

export interface FunnelSummaryCounts {
  decisions?: number | null;
  with_intent?: number | null;
  not_created?: number | null;
  not_created_reasons?: Record<string, number> | null;
  evidence_missing?: number | null;
  submitted?: number | null;
  submitted_frac?: string | null;
  submit_rejected?: number | null;
  submit_unknown?: number | null;
  superseded?: number | null;
  filled_verified?: number | null;
  partial?: number | null;
  waiting?: number | null;
  discarded?: number | null;
  cleared?: number | null;
  created_pending?: number | null;
  carryover_intents?: number | null;
}

export interface FunnelDegraded {
  fills?: { degraded?: boolean | null; reason?: string | null; truncated?: boolean | null } | null;
  unattributed_decision_events?: number | null;
  unparseable_timestamps?: number | null;
  unparseable_payloads?: number | null;
  legacy_flush_events?: number | null;
  duplicate_fill_activities?: number | null;
  post_cutoff_fills?: number | null;
  identityless_observed_today?: {
    kind?: string | null; symbol?: string | null; timestamp?: string | null;
    mode?: string | null; note?: string | null;
  }[] | null;
  // R6-B: the same identity-less rows counted PER MODE — "unsplit" is the
  // backend's reference bucket for mode-NULL history and must never be
  // folded into the page's own mode.
  identityless_by_mode?: Record<string, number> | null;
  // R6-B: order observations that could not be attributed to any intent
  // (ownership conflict, identity-less submission, no submission on file).
  // Each row names its own reason — displayed as data, never re-derived.
  unattributed_order_events?: {
    kind?: string | null;
    order_id?: string | null;
    symbol?: string | null;
    mode?: string | null;
    timestamp?: string | null;
    note?: string | null;
  }[] | null;
  orphan_intents?: { intent_id?: string | null; symbol?: string | null; created_by_decision?: string | null; note?: string | null }[] | null;
  unknown_origin_intents?: { intent_id?: string | null; symbol?: string | null; note?: string | null }[] | null;
  notes?: string[] | null;
}

export interface FunnelPayload {
  session_date?: string | null;
  mode?: string | null;
  generated_at?: string | null;
  // Old journal: only legacy_event_counts kind counts, no identity chains.
  schema_degraded?: boolean | null;
  schema_reason?: string | null;
  summary?: FunnelSummaryCounts | null;
  chains?: FunnelChain[] | null;
  carryover?: FunnelCarryover[] | null;
  unlinked_fills?: FunnelUnlinkedFill[] | null;
  run_skips?: {
    run_id?: string | null;
    reason?: string | null;
    at?: string | null;
    // R6-B: run.py writes the prose remainder VERBATIM in detail and the
    // structured unprocessed intent symbols as a list — null when the payload
    // carried none. Both render as data; the page never parses the prose.
    detail?: string | null;
    unprocessed?: string[] | null;
  }[] | null;
  degraded?: FunnelDegraded | null;
  legacy_event_counts?: Record<string, number> | null;
}

export interface LiveSignalRow {
  symbol: string;
  score: number | null;
  combined: number | null;
  action: string | null;
  gap_to_buy: number | null;
  rsi14: number | null;
  extended: boolean | null;
  last_price: number | null;
  live_price: number | null;
  components: { trend: number; cross: number; momentum: number; macd: number; rsi: number } | null;
  held: boolean;
  skip: string | null;
}

export interface LiveSignalsPayload {
  regime: { label: string; score: number; vix: number | null; components: Record<string, number>; notes: string[] };
  buy_threshold: number | null;
  min_quant_score_to_consider: number | null;
  weights: Record<string, number>;
  signals: LiveSignalRow[];
  holdings: PositionRow[];
  degraded: boolean;
  degraded_reasons: string[];
  cache_asof: string | null;
  live_prices_ok: boolean;
  note: string;
}

export interface LiveMeta {
  schedule: { next: string | null; kind: string | null; seconds: number | null; next_deep: string | null; next_fast: string | null };
  clock: { is_open: boolean; timestamp?: string; next_open?: string; next_close?: string } | null;
  clock_reason: string | null;
  progress: { running: boolean; analyzed: number; total: number; current_symbol: string | null; stage: string | null; cycle: number | null };
  pipeline: [string, string][];
}

export interface LiveEvent {
  t: string;
  stage: string;
  cycle?: number;
  symbol?: string;
  [key: string]: unknown;
}

export interface ComparePayload {
  normalized_equity: { p1: { date: string; value: number | null }[]; p2: { date: string; value: number | null }[] };
  overlap: { date: string; overlap: number; p1_count: number; p2_count: number }[];
  rolling_correlation: { date: string; correlation: number | null }[];
  note: string;
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json() as Promise<T>;
}

export const api = {
  books: () => get<{ books: BookSummary[] }>("/api/books"),
  equity: (book: string, days = 180) =>
    get<{ series: EquityPoint[] }>(`/api/books/${book}/equity?days=${days}`),
  cycles: (book: string, limit = 40) =>
    get<{ cycles: CycleRow[] }>(`/api/books/${book}/cycles?limit=${limit}`),
  decisions: (book: string, params: { cycle_id?: number; symbol?: string; limit?: number } = {}) => {
    const q = new URLSearchParams();
    if (params.cycle_id) q.set("cycle_id", String(params.cycle_id));
    if (params.symbol) q.set("symbol", params.symbol);
    q.set("limit", String(params.limit ?? 200));
    return get<{ decisions: DecisionRow[] }>(`/api/books/${book}/decisions?${q}`);
  },
  positions: (book: string) => get<{ positions: PositionRow[]; degraded?: boolean; reason?: string }>(`/api/books/${book}/positions`),
  trades: (book: string) =>
    get<{ trades: TradeTrip[]; timeline: { closed_at: string; cum_realized_pnl: number; symbol: string }[]; degraded?: boolean; reason?: string }>(`/api/books/${book}/trades`),
  logic: (book: string) => get<LogicPayload>(`/api/books/${book}/logic`),
  outcomes: (book: string) => get<{ buckets: Record<string, unknown>[] }>(`/api/books/${book}/signal-outcomes`),
  roster: (book: string) => get<RosterPayload>(`/api/books/${book}/roster`),
  compare: () => get<ComparePayload>("/api/compare"),
  health: (book: string) => get<HealthPayload>(`/api/books/${book}/health`),
  today: (book: string) => get<TodayPayload>(`/api/books/${book}/today`),
  funnel: (book: string, mode = "paper") =>
    get<FunnelPayload>(`/api/books/${book}/funnel?mode=${mode}`),
  liveSignals: (book: string) => get<LiveSignalsPayload>(`/api/books/${book}/live/signals`),
  liveStreamUrl: (book: string) => `/api/books/${book}/live/stream`,
};

export const usd = (v: number | null | undefined, digits = 0): string =>
  v === null || v === undefined ? "—" : `$${v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;

export const pct = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`;

export const shortTime = (iso: string | null | undefined): string =>
  iso ? iso.slice(0, 16).replace("T", " ") : "—";
