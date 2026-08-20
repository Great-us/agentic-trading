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
    fill_price REAL,
    notional REAL,
    atr14 REAL,
    stop_price REAL,
    sizing_reason TEXT,
    grok_stance TEXT,
    grok_confidence REAL,
    grok_summary TEXT
);

-- Highest price seen while a position has been open. The broker reports average
-- cost but not the peak, and the trailing stop needs the peak to know how much
-- of a gain has been given back.
CREATE TABLE IF NOT EXISTS position_peaks (
    symbol TEXT PRIMARY KEY,
    high_water_mark REAL NOT NULL,
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


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    if str(db_path) == ":memory:":
        conn = sqlite3.connect(":memory:")
    else:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
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
    ):
        if column not in decision_cols:
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {column} {ddl}")
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
            grok_stance, grok_confidence, grok_summary)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                cycle_id, r.symbol, r.quant_score, r.llm_stance, r.llm_confidence,
                r.llm_rationale, r.combined_score, r.action, r.reasoning,
                r.order_status, r.order_qty,
                r.fill_price, r.notional, r.atr14, r.stop_price, r.sizing_reason,
                r.grok_stance, r.grok_confidence, r.grok_summary,
            )
            for r in rows
        ],
    )
    conn.commit()
    return cycle_id
