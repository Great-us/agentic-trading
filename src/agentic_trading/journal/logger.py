"""SQLite journal of every decision cycle — one row per symbol per run, plus
whatever order resulted. This is the audit trail for reviewing/tuning the
strategy later; nothing else in the pipeline reads it back."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

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


def save_trade_intent(conn: sqlite3.Connection, intent: TradeIntent) -> None:
    conn.execute(
        """INSERT INTO trade_intents
           (symbol, created_at, signal_price, atr14, quant_score, combined_score, reasoning, not_before)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(symbol) DO UPDATE SET
             created_at = excluded.created_at,
             signal_price = excluded.signal_price,
             atr14 = excluded.atr14,
             quant_score = excluded.quant_score,
             combined_score = excluded.combined_score,
             reasoning = excluded.reasoning,
             not_before = excluded.not_before""",
        (intent.symbol, intent.created_at, intent.signal_price, intent.atr14,
         intent.quant_score, intent.combined_score, intent.reasoning, intent.not_before),
    )
    conn.commit()


def load_trade_intents(conn: sqlite3.Connection) -> list[TradeIntent]:
    rows = conn.execute(
        "SELECT symbol, created_at, signal_price, atr14, quant_score, combined_score, reasoning, not_before "
        "FROM trade_intents"
    ).fetchall()
    return [TradeIntent(*row) for row in rows]


def clear_trade_intent(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute("DELETE FROM trade_intents WHERE symbol = ?", (symbol,))
    conn.commit()


def record_intent_event(conn: sqlite3.Connection, timestamp: str, symbol: str,
                        kind: str, *, deferred: bool, detail: str | None = None) -> None:
    """Log why a queued intent did not become an order this scan.

    Append-only: an intent that is deferred four times and then discarded leaves
    five rows, which is the point — "how often does the chase guard hold a name
    back" is otherwise unanswerable from the journal."""
    conn.execute(
        "INSERT INTO intent_events (timestamp, symbol, kind, deferred, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        (timestamp, symbol, kind, 1 if deferred else 0, detail),
    )
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
