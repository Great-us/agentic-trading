"""SQLite journal of every decision cycle — one row per symbol per run, plus
whatever order resulted. This is the audit trail for reviewing/tuning the
strategy later; nothing else in the pipeline reads it back."""
from __future__ import annotations

import logging

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = ROOT / "data" / "journal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    mode TEXT NOT NULL,
    equity REAL,
    cash REAL,
    regime_score REAL,
    regime_label TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
    symbol TEXT NOT NULL,
    quant_score REAL,
    llm_stance TEXT,
    llm_confidence REAL,
    llm_rationale TEXT,
    combined_score REAL,
    action TEXT NOT NULL,
    reasoning TEXT,
    order_status TEXT,
    order_qty REAL,
    -- Live fills happen at the broker after TradeIntent execution; this column
    -- stays NULL and actual fill prices come from Alpaca's activity feed
    -- (see agentic_trading.dashboard.broker_read).
    fill_price REAL,
    notional REAL,
    atr14 REAL,
    stop_price REAL,
    sizing_reason TEXT,
    grok_stance TEXT,
    grok_confidence REAL,
    grok_summary TEXT,
    llm_evidence_quality TEXT,
    llm_risk_flags TEXT
);

CREATE TABLE IF NOT EXISTS signal_outcomes (
    decision_id INTEGER PRIMARY KEY REFERENCES decisions(id),
    asof TEXT,
    ret_1d REAL,
    ret_5d REAL,
    ret_20d REAL,
    mfe_20d REAL,
    mae_20d REAL,
    evaluated_at TEXT NOT NULL
);

-- Highest price seen while a position has been open. The broker reports average
-- cost but not the peak, and the trailing stop needs the peak to know how much
-- of a gain has been given back.
CREATE TABLE IF NOT EXISTS position_peaks (
    symbol TEXT PRIMARY KEY,
    high_water_mark REAL NOT NULL,
    updated_at TEXT NOT NULL
);

-- Last observed share count and market value per open position. A stock split
-- doubles (say) the broker-reported qty while preserving value; comparing
-- against this snapshot is what lets the cycle detect a split and rescale its
-- absolute-price state (the peak) instead of mistaking it for a crash.
CREATE TABLE IF NOT EXISTS position_state (
    symbol TEXT PRIMARY KEY,
    last_qty REAL NOT NULL,
    last_value REAL NOT NULL,
    updated_at TEXT NOT NULL
);

-- Last time a symbol got a full (LLM) analysis, and the quant score it had
-- then. Fast-tier scans use this to skip re-analyzing a symbol whose
-- situation hasn't materially changed since a recent look — fundamentals and
-- news don't move within an hour, so re-deriving the same verdict every 20
-- minutes only spends LLM quota for no new information.
CREATE TABLE IF NOT EXISTS llm_escalations (
    symbol TEXT PRIMARY KEY,
    last_escalated_at TEXT NOT NULL,
    last_quant_score REAL NOT NULL
);

-- How many consecutive fast-tier scans have seen a symbol above the buy
-- threshold. Intraday scoring uses the still-forming daily bar, so indicators
-- move continuously and a symbol can cross the threshold, fall back, and cross
-- again within an hour — each crossing looking like a fresh signal to a
-- stateless scan. Requiring N consecutive confirmations turns that noise into
-- a filter. Reset when the symbol stops qualifying or a position is opened.
CREATE TABLE IF NOT EXISTS intraday_confirmations (
    symbol TEXT PRIMARY KEY,
    consecutive_scans INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

-- How many consecutive deep-cycle readings the market regime has sat in
-- sell-trigger territory (see trim_trigger_score / trim_confirm_cycles in
-- risk.yaml). Single row; any reading outside the trigger resets it to 0.
-- TRIM of the existing book fires only once this counter reaches the
-- configured confirm count.
CREATE TABLE IF NOT EXISTS regime_dwell (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    label TEXT,
    score REAL,
    consecutive INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

-- After-close (or pre-window) BUY decisions that must not be submitted until
-- the entry window opens, then revalidated against the overnight gap and the
-- chase guards. not_before (ISO UTC, nullable) is the earliest execution
-- moment: the 9:45 cycle queues for ~10:00 the same day, the 16:15 cycle for
-- the next day's window.
CREATE TABLE IF NOT EXISTS trade_intents (
    symbol TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    signal_price REAL NOT NULL,
    atr14 REAL NOT NULL,
    quant_score REAL,
    combined_score REAL,
    reasoning TEXT,
    not_before TEXT
);

-- What happened to a queued intent at flush time. These outcomes decide whether
-- a BUY the engine already committed to ever reaches the broker, and they used
-- to exist only in logs/*.log — so the single largest failure mode of the week
-- of 2026-08-24 (22 BUY decisions, zero fills) could not be counted from the
-- journal at all. `kind` is one of: gap, chase_signal, chase_open, sizing, ttl.
-- `deferred` records whether the intent survived for a later scan (chase,
-- sizing) or was discarded (gap, ttl).
CREATE TABLE IF NOT EXISTS intent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    deferred INTEGER NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_intent_events_ts ON intent_events(timestamp);
"""

# A-1 (c2c_a7e2 §14.2): nullable identity/linkage columns for intent_events.
# Kept out of SCHEMA on purpose — CREATE TABLE IF NOT EXISTS cannot add columns
# to an existing table, so these go through the incremental migration below and
# the unique index is created only after the columns exist on every journal.
_INTENT_EVENT_COLUMNS = (
    ("event_id", "TEXT"),
    ("run_id", "TEXT"),
    ("mode", "TEXT"),
    ("decision_key", "TEXT"),
    ("intent_id", "TEXT"),
    ("attempt_id", "TEXT"),
    ("order_id", "TEXT"),
    ("payload", "TEXT"),
)

# Whitelist of top-level payload keys stored on intent_events. Deliberately
# excludes anything credential-, prompt- or raw-broker-response-shaped
# (PLAN §A-2: no credentials, raw prompts, raw broker responses). Unknown keys
# are dropped, not stored.
INTENT_EVENT_PAYLOAD_KEYS = frozenset({
    "reason",
    "skip_reason",
    "wait_reason",
    "not_before",
    "client_order_id",
    "order_id",
    "order_status",
    "submit_status",     # order_submitted fact: accepted / rejected / dry_run (leader, 2026-09-20)
    "notional",          # submitted/reserved dollar amount — display value, not a secret
    "previous_version",
    "sizing",
    "cash_available",
    "available_notional",
    "min_notional",
    "limit",
    "limits",
    "filled_qty",
    "filled_avg_price",
    "filled_at",         # broker-reported fill timestamp (absent = never claimed)
    "observed_at",
    "error",
    "attempt",
    "note",
    "note_key",
    "unprocessed",       # order-cap run event: determinable not-yet-processed symbols (R6, iter 3)
})

# R7 (c2c_a7e2 ITERATION 2): secret-key blacklist applied RECURSIVELY inside
# nested payloads. Vocabulary mirrors live_events._SECRET_KEYS (the JSONL
# tail's field filter) on purpose — one secret vocabulary across both
# destinations; tests/test_payload_sanitizer.py pins the two sets equal so
# they cannot drift apart silently. The top-level whitelist above never
# contained any of these names; the extra check is defense in depth in case
# a future whitelist edit would let one through.
INTENT_EVENT_SECRET_KEYS = frozenset({
    "api_key", "secret", "secret_key", "password", "token", "prompt",
    "authorization", "alpaca_api_key", "alpaca_secret_key", "moonshot_api_key",
})


def sanitize_intent_payload(payload: Any) -> Any:
    """R7: the one safe-shape cleaning step for intent-event payloads, shared
    by both destinations — SQLite reads its JSON via _encode_payload, and the
    funnel hands the returned dict straight to the JSONL tail.

    - top level: whitelist INTENT_EVENT_PAYLOAD_KEYS — unknown keys are
      dropped, never stored (PLAN §A-2);
    - nested dicts / lists: recursed; any dict key in INTENT_EVENT_SECRET_KEYS
      is dropped together with its subtree, at every depth (extends
      live_events.emit's top-level skip below the first level);
    - non-finite floats -> None at every depth (never the non-standard
      NaN/Infinity literals);
    - anything non-JSON-native degrades to its str() rather than failing the
      business write around it;
    - non-dict payloads wrap as {"value": ...} (the historical _encode_payload
      contract); None passes through unchanged.
    """
    if payload is None:
        return None
    if not isinstance(payload, dict):
        return {"value": _sanitize_nested(payload)}
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        key = str(key)
        if key in INTENT_EVENT_SECRET_KEYS or key not in INTENT_EVENT_PAYLOAD_KEYS:
            continue
        cleaned[key] = _sanitize_nested(value)
    return cleaned


def _sanitize_nested(value: Any) -> Any:
    """Recursive body of sanitize_intent_payload: containers get the
    secret-key blacklist, non-finite floats and str() degradation at every
    depth; JSON natives pass through untouched."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key = str(key)
            if key in INTENT_EVENT_SECRET_KEYS:
                continue
            cleaned[key] = _sanitize_nested(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize_nested(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _encode_payload(payload: Any) -> str | None:
    """Whitelist + recursively sanitize a payload into JSON text (None
    passthrough). R7: the recursion is where nested secret keys die — a
    top-level whitelist alone let {"sizing": {"api_key": ...}} through."""
    if payload is None:
        return None
    return json.dumps(sanitize_intent_payload(payload),
                      ensure_ascii=False, allow_nan=False)



@dataclass
class DecisionRow:
    symbol: str
    quant_score: float
    llm_stance: str | None
    llm_confidence: float | None
    llm_rationale: str | None
    combined_score: float
    action: str
    reasoning: str
    order_status: str | None = None
    order_qty: float | None = None
    fill_price: float | None = None
    notional: float | None = None
    atr14: float | None = None
    stop_price: float | None = None
    sizing_reason: str | None = None
    grok_stance: str | None = None
    grok_confidence: float | None = None
    grok_summary: str | None = None
    llm_evidence_quality: str | None = None
    llm_risk_flags: str | None = None


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    if str(db_path) == ":memory:":
        conn = sqlite3.connect(":memory:")
    else:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # The deep cycle and the fast scan are separate scheduled tasks and can
        # overlap; both write this database. busy_timeout must be set before
        # the WAL switch — changing journal mode itself takes the write lock.
        conn = sqlite3.connect(db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # A concurrent writer held the lock past the timeout; plain
            # rollback journaling still works, just with less concurrency.
            pass
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS won't add columns to a table that already
    exists, so journals created by an earlier version need them backfilled."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(cycles)")}
    for column, ddl in (("regime_score", "REAL"), ("regime_label", "TEXT")):
        if column not in existing:
            conn.execute(f"ALTER TABLE cycles ADD COLUMN {column} {ddl}")
    decision_cols = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
    for column, ddl in (
        ("fill_price", "REAL"),
        ("notional", "REAL"),
        ("atr14", "REAL"),
        ("stop_price", "REAL"),
        ("sizing_reason", "TEXT"),
        ("grok_stance", "TEXT"),
        ("grok_confidence", "REAL"),
        ("grok_summary", "TEXT"),
        ("llm_evidence_quality", "TEXT"),
        ("llm_risk_flags", "TEXT"),
    ):
        if column not in decision_cols:
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {column} {ddl}")
    intent_cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_intents)")}
    if "not_before" not in intent_cols:
        conn.execute("ALTER TABLE trade_intents ADD COLUMN not_before TEXT")
    if "version" not in intent_cols:
        conn.execute("ALTER TABLE trade_intents ADD COLUMN version TEXT")
    # Old journals: add the nullable linkage columns, but never backfill —
    # historical events/intents keep NULL identities. Re-running is a no-op.
    event_cols = {row[1] for row in conn.execute("PRAGMA table_info(intent_events)")}
    for column, ddl in _INTENT_EVENT_COLUMNS:
        if column not in event_cols:
            conn.execute(f"ALTER TABLE intent_events ADD COLUMN {column} {ddl}")
    # Only after every journal has the column does this index become creatable;
    # SQLite treats NULLs as distinct so the historical NULL rows coexist.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_intent_events_event_id "
        "ON intent_events(event_id)"
    )
    conn.commit()


def load_position_peaks(conn: sqlite3.Connection) -> dict[str, float]:
    return {row[0]: row[1] for row in conn.execute("SELECT symbol, high_water_mark FROM position_peaks")}


def update_position_peak(conn: sqlite3.Connection, symbol: str, price: float, timestamp: str) -> None:
    """Raises the recorded peak, never lowers it — a trailing stop that drifted
    down with the price would stop trailing."""
    conn.execute(
        """INSERT INTO position_peaks (symbol, high_water_mark, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET
             high_water_mark = MAX(high_water_mark, excluded.high_water_mark),
             updated_at = excluded.updated_at""",
        (symbol, price, timestamp),
    )
    conn.commit()


def clear_position_peak(conn: sqlite3.Connection, symbol: str) -> None:
    """Called when a position closes, so a later re-entry starts a fresh peak."""
    conn.execute("DELETE FROM position_peaks WHERE symbol = ?", (symbol,))
    conn.commit()


def reset_position_peak(conn: sqlite3.Connection, symbol: str, price: float, timestamp: str) -> None:
    """Overwrites the peak outright — used after a stock split, when every
    pre-split price is on a different scale and MAX()-only ratcheting would
    keep the stale pre-split value forever."""
    conn.execute(
        """INSERT INTO position_peaks (symbol, high_water_mark, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET
             high_water_mark = excluded.high_water_mark,
             updated_at = excluded.updated_at""",
        (symbol, price, timestamp),
    )
    conn.commit()


def load_position_state(conn: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    """symbol -> (last_qty, last_market_value) recorded the previous cycle."""
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute("SELECT symbol, last_qty, last_value FROM position_state")
    }


def record_position_state(conn: sqlite3.Connection, symbol: str, qty: float,
                          market_value: float, timestamp: str) -> None:
    conn.execute(
        """INSERT INTO position_state (symbol, last_qty, last_value, updated_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET
             last_qty = excluded.last_qty,
             last_value = excluded.last_value,
             updated_at = excluded.updated_at""",
        (symbol, qty, market_value, timestamp),
    )
    conn.commit()


def clear_position_state(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute("DELETE FROM position_state WHERE symbol = ?", (symbol,))
    conn.commit()


def get_last_escalation(conn: sqlite3.Connection, symbol: str) -> tuple[str, float] | None:
    row = conn.execute(
        "SELECT last_escalated_at, last_quant_score FROM llm_escalations WHERE symbol = ?", (symbol,)
    ).fetchone()
    return (row[0], row[1]) if row else None


def record_escalation(conn: sqlite3.Connection, symbol: str, timestamp: str, quant_score: float) -> None:
    conn.execute(
        """INSERT INTO llm_escalations (symbol, last_escalated_at, last_quant_score) VALUES (?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET
             last_escalated_at = excluded.last_escalated_at,
             last_quant_score = excluded.last_quant_score""",
        (symbol, timestamp, quant_score),
    )
    conn.commit()


def bump_intraday_confirmation(conn: sqlite3.Connection, symbol: str, timestamp: str) -> int:
    """Records that this symbol qualified again, and returns the new consecutive
    count (1 on the first scan that sees it)."""
    row = conn.execute(
        "SELECT consecutive_scans FROM intraday_confirmations WHERE symbol = ?", (symbol,)
    ).fetchone()
    count = (row[0] + 1) if row else 1
    conn.execute(
        """INSERT INTO intraday_confirmations (symbol, consecutive_scans, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET
             consecutive_scans = excluded.consecutive_scans,
             last_seen_at = excluded.last_seen_at""",
        (symbol, count, timestamp, timestamp),
    )
    conn.commit()
    return count


def clear_intraday_confirmation(conn: sqlite3.Connection, symbol: str) -> None:
    """A symbol that stopped qualifying (or that we just bought) starts its
    streak over — otherwise an on-again/off-again name would accumulate
    confirmations across unrelated crossings."""
    conn.execute("DELETE FROM intraday_confirmations WHERE symbol = ?", (symbol,))
    conn.commit()


def record_regime_dwell(conn: sqlite3.Connection, triggered: bool, score: float,
                        label: str, timestamp: str) -> int:
    """Advances the single-row sell-trigger counter and returns the new
    consecutive count: +1 while the reading is in trigger territory, reset to
    0 on any reading outside it."""
    row = conn.execute("SELECT consecutive FROM regime_dwell WHERE id = 1").fetchone()
    count = (row[0] + 1) if (row and triggered) else (1 if triggered else 0)
    conn.execute(
        """INSERT INTO regime_dwell (id, label, score, consecutive, updated_at) VALUES (1, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             label = excluded.label,
             score = excluded.score,
             consecutive = excluded.consecutive,
             updated_at = excluded.updated_at""",
        (label, score, count, timestamp),
    )
    conn.commit()
    return count


@dataclass
class TradeIntent:
    symbol: str
    created_at: str
    signal_price: float
    atr14: float
    quant_score: float
    combined_score: float
    reasoning: str
    not_before: str | None = None  # ISO UTC; None = executable immediately
    # Immutable identity assigned by save_trade_intent (UUID4 string). Old
    # journals and old constructors leave it None until a real new decision
    # replaces the row — loads and flushes never invent one.
    version: str | None = None


@dataclass(frozen=True)
class IntentSaveReceipt:
    """What save_trade_intent actually wrote — the only trustworthy source for
    "which identity did this save get". Never re-query the table to guess it."""
    version: str
    action: Literal["created", "replaced"]
    previous_version: str | None


def save_trade_intent(conn: sqlite3.Connection, intent: TradeIntent) -> IntentSaveReceipt:
    """Creates or replaces the intent row and mints its immutable version.

    Every save is a new UUID4; loads and flushes never generate one, and a
    delete + recreate gets a fresh identity (never reused). Callers may ignore
    the receipt — legacy ``-> None`` usage keeps working."""
    previous = conn.execute(
        "SELECT version FROM trade_intents WHERE symbol = ?", (intent.symbol,)
    ).fetchone()
    action: Literal["created", "replaced"] = "replaced" if previous else "created"
    previous_version = previous[0] if previous else None
    # R1 (review round 2): identity minting must never block the business
    # save — a uuid failure degrades to a NULL version (the row is saved,
    # the decision is NOT flipped to save_failed/WAIT). A later real save
    # still mints a fresh identity; no identity is ever reused.
    try:
        version = str(uuid.uuid4())
    except Exception:
        logger_ = logging.getLogger(__name__)
        logger_.warning("version minting failed for %s — saving with NULL "
                        "identity.", intent.symbol, exc_info=True)
        version = None
    conn.execute(
        """INSERT INTO trade_intents
           (symbol, created_at, signal_price, atr14, quant_score, combined_score, reasoning, not_before, version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET
             created_at = excluded.created_at,
             signal_price = excluded.signal_price,
             atr14 = excluded.atr14,
             quant_score = excluded.quant_score,
             combined_score = excluded.combined_score,
             reasoning = excluded.reasoning,
             not_before = excluded.not_before,
             version = excluded.version""",
        (intent.symbol, intent.created_at, intent.signal_price, intent.atr14,
         intent.quant_score, intent.combined_score, intent.reasoning,
         intent.not_before, version),
    )
    conn.commit()
    # R1 (review round 2): the business write is already committed — a receipt
    # failure here must never let the caller report the save as failed. A
    # degraded receipt still tells the truth it can (action, previous), with
    # the minted version only if it is a real string.
    try:
        return IntentSaveReceipt(version=version if isinstance(version, str) else None,
                                 action=action, previous_version=previous_version)
    except Exception:
        logging.getLogger(__name__).warning(
            "save receipt construction failed for %s — business save is "
            "already committed.", intent.symbol, exc_info=True)
        return None


def load_trade_intents(conn: sqlite3.Connection) -> list[TradeIntent]:
    rows = conn.execute(
        "SELECT symbol, created_at, signal_price, atr14, quant_score, combined_score, reasoning, not_before, version "
        "FROM trade_intents"
    ).fetchall()
    return [TradeIntent(*row) for row in rows]


def clear_trade_intent(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute("DELETE FROM trade_intents WHERE symbol = ?", (symbol,))
    conn.commit()


def record_intent_event(
    conn: sqlite3.Connection,
    *args: Any,
    timestamp: str | None = None,
    deferred: bool | None = None,
    detail: str | None = None,
    event_id: str | None = None,
    run_id: str | None = None,
    mode: str | None = None,
    decision_key: str | None = None,
    intent_id: str | None = None,
    attempt_id: str | None = None,
    order_id: str | None = None,
    payload: Any = None,
) -> None:
    """Log what happened to a queued intent (or, with the new kinds, anywhere
    in the BUY decision -> fill funnel).

    Two positional styles, kept distinguishable by arity:
    - legacy: ``(timestamp, symbol, kind)`` with keyword ``deferred``/``detail``
      (run.py flush outcomes — unchanged semantics);
    - A-1:    ``(symbol, kind, deferred, detail)`` with the identity linkage
      passed as keywords. ``intent_id`` is the intent's ``version``.

    The same ``event_id`` delivered twice is not double-counted (INSERT OR
    IGNORE against the unique index). The insert runs inside a savepoint so an
    observability failure never leaves the caller's business transaction
    poisoned — but it still raises, so callers that care can catch it (the
    existing _journal_safe path does exactly that).

    R3 transaction ownership: on an autocommit connection this function owns
    the transaction and commits (persist-before-return, unchanged). When the
    caller already holds an open transaction, only our savepoint is released —
    the caller's work stays theirs to commit or roll back, and the event is
    durable exactly when the caller's transaction is.

    Append-only: an intent that is deferred four times and then discarded leaves
    five rows, which is the point — "how often does the chase guard hold a name
    back" is otherwise unanswerable from the journal.
    """
    if len(args) == 3:  # legacy (timestamp, symbol, kind)
        ts, sym, knd = args
    elif len(args) == 4:  # A-1 (symbol, kind, deferred, detail)
        sym, knd, pos_deferred, pos_detail = args
        if deferred is not None or detail is not None:
            raise TypeError("deferred/detail passed both positionally and by keyword")
        ts, deferred, detail = timestamp, pos_deferred, pos_detail
    else:
        raise TypeError(
            "record_intent_event expects (timestamp, symbol, kind) legacy or "
            "(symbol, kind, deferred, detail) positional arguments"
        )
    assert deferred is not None
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    payload_text = _encode_payload(payload)
    # R3 (c2c_a7e2 ITERATION 2): transaction ownership. ``in_transaction`` is
    # the C-level autocommit state (sqlite3_get_autocommit() == 0) — True
    # exactly when someone (the caller's DML, an explicit BEGIN, or a caller
    # savepoint) already opened a transaction. Checked BEFORE our SAVEPOINT,
    # because SAVEPOINT itself flips it. If the connection is already in a
    # transaction this function is a guest: the savepoint keeps our insert
    # individually undoable, we release only our own savepoint and never
    # commit or roll back the caller's business work (a top-level commit here
    # would publish their uncommitted writes beyond their control — the
    # review's leak). Only when we arrived on an autocommit connection do we
    # own the transaction and commit, preserving the historical
    # persist-before-return behavior every existing call site relies on.
    owns_transaction = not conn.in_transaction
    conn.execute("SAVEPOINT intent_event_write")
    try:
        conn.execute(
            """INSERT OR IGNORE INTO intent_events
               (timestamp, symbol, kind, deferred, detail,
                event_id, run_id, mode, decision_key, intent_id,
                attempt_id, order_id, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, sym, knd, 1 if deferred else 0, detail,
             event_id, run_id, mode, decision_key, intent_id,
             attempt_id, order_id, payload_text),
        )
    except BaseException:
        try:
            # Undo only our own statement; never the caller's transaction.
            # finally-RELEASE guarantees the savepoint cannot outlive this
            # handler even if the ROLLBACK TO itself fails (review: no
            # leftover savepoint, connection still usable).
            conn.execute("ROLLBACK TO intent_event_write")
        finally:
            conn.execute("RELEASE intent_event_write")
        raise
    conn.execute("RELEASE intent_event_write")
    if owns_transaction:
        conn.commit()


def record_cycle(
    conn: sqlite3.Connection,
    timestamp: str,
    mode: str,
    equity: float,
    cash: float,
    rows: list[DecisionRow],
    regime_score: float | None = None,
    regime_label: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO cycles (timestamp, mode, equity, cash, regime_score, regime_label) VALUES (?, ?, ?, ?, ?, ?)",
        (timestamp, mode, equity, cash, regime_score, regime_label),
    )
    cycle_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO decisions
           (cycle_id, symbol, quant_score, llm_stance, llm_confidence, llm_rationale,
            combined_score, action, reasoning, order_status, order_qty,
            fill_price, notional, atr14, stop_price, sizing_reason,
            grok_stance, grok_confidence, grok_summary,
            llm_evidence_quality, llm_risk_flags)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                cycle_id, r.symbol, r.quant_score, r.llm_stance, r.llm_confidence,
                r.llm_rationale, r.combined_score, r.action, r.reasoning,
                r.order_status, r.order_qty,
                r.fill_price, r.notional, r.atr14, r.stop_price, r.sizing_reason,
                r.grok_stance, r.grok_confidence, r.grok_summary,
                r.llm_evidence_quality, r.llm_risk_flags,
            )
            for r in rows
        ],
    )
    conn.commit()
    return cycle_id
