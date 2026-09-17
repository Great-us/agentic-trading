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
  realized_pnl: number;
  unrealized_pnl?: number | null;
  open: boolean;
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
  deep: HealthMode;
  fast: HealthMode;
  limits: { deep_hours: number; fast_minutes: number };
}

export interface HealthMode {
  timestamp: string | null;
  cycle_id: number | null;
  age_seconds: number | null;
  missed_sessions: number | null;
  stops_covered: number | null;
  positions: number | null;
  naked?: boolean;
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
  liveSignals: (book: string) => get<LiveSignalsPayload>(`/api/books/${book}/live/signals`),
  liveStreamUrl: (book: string) => `/api/books/${book}/live/stream`,
};

export const usd = (v: number | null | undefined, digits = 0): string =>
  v === null || v === undefined ? "—" : `$${v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;

export const pct = (v: number | null | undefined, digits = 2): string =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`;

export const shortTime = (iso: string | null | undefined): string =>
  iso ? iso.slice(0, 16).replace("T", " ") : "—";
