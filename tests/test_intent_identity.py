"""A-1 (c2c_a7e2): intent identity, versions and persistent linkage.

Contract source: PROGRESS.md §14.2 / research/c2c_a7e2-p1a-plan.md §二.
Storage side only — run.py wiring belongs to 员工甲.

Covers:
- old-DB migration runs twice idempotently; historical rows get NULL ids and
  keep their business fields byte-identical;
- a new intent deferred ten times still has exactly one intent_id (ten real
  attempts, one identity);
- replacement yields a fresh version and reports the previous one;
- delete + recreate never reuses an identity;
- duplicate event_id delivery is not double-counted;
- an event-write failure never rolls back an already-committed intent save and
  leaves the connection usable (savepoint boundary);
- legacy record_intent_event(conn, ts, symbol, kind, deferred=..., detail=...)
  call sites keep working unchanged.

R3 (ITERATION 2, review §R3): transaction ownership — when the caller already
holds an open (uncommitted) transaction, the event write releases only its own
savepoint and must NOT commit (or roll back) the caller's business work; the
function commits only when it arrived on an autocommit connection.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from agentic_trading.journal.logger import (
    IntentSaveReceipt,
    TradeIntent,
    clear_trade_intent,
    connect,
    load_trade_intents,
    record_intent_event,
    save_trade_intent,
)

# Schema exactly as it was before A-1 (old journals look like this).
OLD_SCHEMA = """
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
CREATE TABLE IF NOT EXISTS intent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    deferred INTEGER NOT NULL,
    detail TEXT
);
"""

NEW_EVENT_COLS = (
    "event_id", "run_id", "mode", "decision_key",
    "intent_id", "attempt_id", "order_id", "payload",
)


def _intent(symbol="VEEV", **kw):
    fields = dict(
        symbol=symbol,
        created_at="2026-09-20T14:00:00+00:00",
        signal_price=230.5,
        atr14=4.2,
        quant_score=0.71,
        combined_score=0.74,
        reasoning="test intent",
        not_before=None,
    )
    fields.update(kw)
    return TradeIntent(**fields)


def _make_old_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        """INSERT INTO trade_intents
           (symbol, created_at, signal_price, atr14, quant_score,
            combined_score, reasoning, not_before)
           VALUES ('ANET', '2026-08-01T13:00:00+00:00', 150.25, 3.1,
                   0.55, 0.6, 'legacy active intent', NULL)"""
    )
    conn.execute(
        """INSERT INTO intent_events (timestamp, symbol, kind, deferred, detail)
           VALUES ('2026-08-01T14:00:00+00:00', 'ANET', 'chase_signal', 1,
                   'price moved 1.4 ATR')"""
    )
    conn.commit()
    conn.close()


def test_migration_twice_idempotent_and_old_rows_stay_null(tmp_path):
    db = tmp_path / "old.db"
    _make_old_db(db)
    for _ in range(2):
        conn = connect(db)
        conn.close()

    conn = sqlite3.connect(db)
    intent_cols = {row[1] for row in conn.execute("PRAGMA table_info(trade_intents)")}
    assert "version" in intent_cols
    event_cols = {row[1] for row in conn.execute("PRAGMA table_info(intent_events)")}
    for col in NEW_EVENT_COLS:
        assert col in event_cols

    row = conn.execute(
        "SELECT symbol, created_at, signal_price, atr14, quant_score, "
        "combined_score, reasoning, not_before, version FROM trade_intents"
    ).fetchone()
    assert row[:8] == (
        "ANET", "2026-08-01T13:00:00+00:00", 150.25, 3.1, 0.55, 0.6,
        "legacy active intent", None,
    )
    assert row[8] is None  # old active intent: no invented identity

    ev = conn.execute(
        "SELECT event_id, run_id, mode, decision_key, intent_id, attempt_id, "
        "order_id, payload, detail, deferred FROM intent_events"
    ).fetchone()
    assert ev[:8] == (None, None, None, None, None, None, None, None)
    assert ev[8] == "price moved 1.4 ATR" and ev[9] == 1  # business fields intact
    conn.close()


def test_ten_deferred_flushes_one_intent_id(tmp_path):
    conn = connect(tmp_path / "n.db")
    receipt = save_trade_intent(conn, _intent())
    assert isinstance(receipt, IntentSaveReceipt)
    assert receipt.action == "created" and receipt.previous_version is None
    uuid.UUID(receipt.version)  # valid UUID4 string
    for _ in range(10):
        record_intent_event(
            conn, "VEEV", "sizing", True, "cash held for pending buy",
            intent_id=receipt.version,
        )
    ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT intent_id FROM intent_events WHERE intent_id IS NOT NULL")]
    assert ids == [receipt.version]
    assert conn.execute(
        "SELECT COUNT(*) FROM intent_events").fetchone()[0] == 10
    conn.close()


def test_replace_yields_new_version_and_reports_previous(tmp_path):
    conn = connect(tmp_path / "n.db")
    first = save_trade_intent(conn, _intent())
    second = save_trade_intent(conn, _intent(created_at="2026-09-20T15:00:00+00:00"))
    assert second.action == "replaced"
    assert second.version != first.version
    assert second.previous_version == first.version
    loaded = load_trade_intents(conn)
    assert len(loaded) == 1 and loaded[0].version == second.version
    conn.close()


def test_delete_then_recreate_does_not_reuse_identity(tmp_path):
    conn = connect(tmp_path / "n.db")
    first = save_trade_intent(conn, _intent())
    clear_trade_intent(conn, "VEEV")
    second = save_trade_intent(conn, _intent())
    assert second.action == "created"
    assert second.previous_version is None
    assert second.version != first.version
    conn.close()


def test_duplicate_event_id_not_double_counted(tmp_path):
    conn = connect(tmp_path / "n.db")
    for _ in range(3):
        record_intent_event(
            conn, "VEEV", "chase_signal", True, "gap 1.2 ATR",
            event_id="evt-1", run_id="p1:2026-09-20T14:00:00+00:00:abcd1234",
            mode="paper", decision_key="p1:2026-09-20T14:00:00+00:00:abcd1234:VEEV",
            order_id="ord-9",
        )
    rows = conn.execute(
        "SELECT event_id, run_id, mode, decision_key, order_id FROM intent_events"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("evt-1", "p1:2026-09-20T14:00:00+00:00:abcd1234",
                       "paper", "p1:2026-09-20T14:00:00+00:00:abcd1234:VEEV",
                       "ord-9")
    conn.close()


def test_event_write_failure_does_not_rollback_intent_save(tmp_path, monkeypatch):
    conn = connect(tmp_path / "n.db")
    receipt = save_trade_intent(conn, _intent())
    monkeypatch.setattr(
        "agentic_trading.journal.logger._encode_payload",
        lambda p: (_ for _ in ()).throw(ValueError("payload boom")),
    )
    with pytest.raises(ValueError):
        record_intent_event(conn, "VEEV", "sizing", True, "x", payload={"reason": "x"})
    monkeypatch.undo()
    # Business save is still intact and the connection is usable (savepoint
    # boundary cleaned up, no poisoned open transaction).
    saved = load_trade_intents(conn)
    assert len(saved) == 1 and saved[0].version == receipt.version
    record_intent_event(conn, "VEEV", "sizing", True, "after failure")  # works
    assert conn.execute("SELECT COUNT(*) FROM intent_events").fetchone()[0] == 1
    conn.close()


def test_legacy_call_signature_still_works(tmp_path):
    conn = connect(tmp_path / "n.db")
    record_intent_event(conn, "2026-09-20T14:05:00+00:00", "HPE", "gap",
                        deferred=False, detail="gapped 2%")
    row = conn.execute(
        "SELECT timestamp, symbol, kind, deferred, detail FROM intent_events"
    ).fetchone()
    assert row == ("2026-09-20T14:05:00+00:00", "HPE", "gap", 0, "gapped 2%")
    conn.close()


def test_payload_whitelist_nested_and_nonfinite(tmp_path):
    conn = connect(tmp_path / "n.db")
    record_intent_event(
        conn, "VEEV", "sizing", True, "notional 0",
        payload={
            "reason": "cash",
            "sizing": {"cash_available": 65.62, "min_notional": float("nan"),
                       "limits": ["exposure", "book_risk"]},
            "api_key": "SECRET",
            "prompt": "raw prompt",
        },
    )
    raw = conn.execute("SELECT payload FROM intent_events").fetchone()[0]
    data = json.loads(raw)
    assert "api_key" not in data and "prompt" not in data
    assert data["reason"] == "cash"
    assert data["sizing"]["cash_available"] == 65.62
    assert data["sizing"]["min_notional"] is None  # non-finite sanitized
    assert data["sizing"]["limits"] == ["exposure", "book_risk"]
    conn.close()


def test_tradeintent_version_default_keeps_old_construction(tmp_path):
    intent = _intent()  # no version kwarg — old constructors keep working
    assert intent.version is None
    conn = connect(tmp_path / "n.db")
    receipt = save_trade_intent(conn, intent)
    loaded = load_trade_intents(conn)[0]
    assert loaded.version == receipt.version
    assert (loaded.symbol, loaded.signal_price) == ("VEEV", 230.5)
    conn.close()


# --------------------------------------------------------------------------
# R3 (review ITERATION 1 §R3): transaction ownership of record_intent_event.
# --------------------------------------------------------------------------

def test_event_write_does_not_commit_caller_transaction(tmp_path):
    """R3 core red: with the caller holding an open (uncommitted) business
    transaction, the event write must not commit it. A second connection —
    which can only see committed data — must see neither the business change
    nor the event, and the caller's rollback must still undo the business
    write."""
    db = tmp_path / "n.db"
    conn = connect(db)
    receipt = save_trade_intent(conn, _intent())
    conn.execute("UPDATE trade_intents SET reasoning = 'uncommitted change' "
                 "WHERE symbol = 'VEEV'")
    assert conn.in_transaction  # caller owns an open business transaction
    record_intent_event(conn, "VEEV", "sizing", True,
                        "observed while caller tx open")

    # Second connection = committed-only view: nothing may have leaked out.
    other = sqlite3.connect(db)
    assert other.execute(
        "SELECT reasoning FROM trade_intents WHERE symbol = 'VEEV'"
    ).fetchone()[0] == "test intent"  # business change NOT committed
    assert other.execute(
        "SELECT COUNT(*) FROM intent_events").fetchone()[0] == 0
    other.close()

    # The caller can still undo its business write — and the event rides the
    # same transaction, so it disappears with it (never half-committed).
    conn.rollback()
    loaded = load_trade_intents(conn)
    assert len(loaded) == 1 and loaded[0].version == receipt.version
    assert loaded[0].reasoning == "test intent"
    assert conn.execute(
        "SELECT COUNT(*) FROM intent_events").fetchone()[0] == 0
    # connection stays fully usable afterwards
    record_intent_event(conn, "VEEV", "sizing", True, "after caller rollback")
    assert conn.execute(
        "SELECT COUNT(*) FROM intent_events").fetchone()[0] == 1
    conn.close()


def test_caller_transaction_can_still_commit_after_event_write(tmp_path):
    """R3 companion: when the caller decides to commit, the event and the
    business change land together — one transaction, the caller in charge."""
    db = tmp_path / "n.db"
    conn = connect(db)
    save_trade_intent(conn, _intent())
    conn.execute("UPDATE trade_intents SET reasoning = 'biz commit' "
                 "WHERE symbol = 'VEEV'")
    record_intent_event(conn, "VEEV", "sizing", True, "obs")
    conn.commit()
    other = sqlite3.connect(db)
    assert other.execute(
        "SELECT reasoning FROM trade_intents WHERE symbol = 'VEEV'"
    ).fetchone()[0] == "biz commit"
    assert other.execute(
        "SELECT COUNT(*) FROM intent_events").fetchone()[0] == 1
    other.close()
    conn.close()


def test_event_insert_failure_keeps_caller_transaction_intact(tmp_path):
    """R3: the event INSERT failing (forced here via a trigger) must leave the
    caller's open transaction exactly as it was, no leftover savepoint, and
    the caller still able to commit its own work."""
    db = tmp_path / "n.db"
    conn = connect(db)
    save_trade_intent(conn, _intent())
    conn.execute("""CREATE TRIGGER force_event_insert_failure
                    BEFORE INSERT ON intent_events
                    BEGIN SELECT RAISE(ABORT, 'boom'); END""")
    conn.commit()
    conn.execute("UPDATE trade_intents SET reasoning = 'biz change' "
                 "WHERE symbol = 'VEEV'")
    with pytest.raises((sqlite3.OperationalError, sqlite3.IntegrityError),
                       match="boom"):
        record_intent_event(conn, "VEEV", "sizing", True, "x")
    # savepoint boundary cleaned up — releasing a non-existent savepoint fails
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("RELEASE intent_event_write")
    # outer transaction is still exactly what the caller left open
    assert conn.execute(
        "SELECT reasoning FROM trade_intents WHERE symbol = 'VEEV'"
    ).fetchone()[0] == "biz change"
    conn.commit()  # the caller's choice, unaffected by the failed event
    other = sqlite3.connect(db)
    assert other.execute(
        "SELECT reasoning FROM trade_intents WHERE symbol = 'VEEV'"
    ).fetchone()[0] == "biz change"
    other.close()
    conn.close()


class _CommitOnceFailingConn(sqlite3.Connection):
    """Connection whose next commit() raises (commit-failure injection)."""
    fail_next_commit = False

    def commit(self):
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise sqlite3.OperationalError("commit boom")
        return super().commit()


def test_commit_failure_when_function_owns_transaction_leaves_conn_usable(tmp_path):
    """R3: a failing commit (function owns the transaction) must not strand
    the connection — no leftover savepoint, and the caller can keep writing
    (the durability of the failed commit's own row is already guaranteed by
    the savepoint RELEASE on an autocommit connection; this test pins that a
    commit failure changes nothing about connection usability)."""
    db = tmp_path / "n.db"
    base = connect(db)
    base.close()  # schema + migrations only
    conn = sqlite3.connect(db, factory=_CommitOnceFailingConn)
    conn.fail_next_commit = True
    with pytest.raises(sqlite3.OperationalError, match="commit boom"):
        record_intent_event(conn, "VEEV", "sizing", True, "before failure")
    # no leftover savepoint on the connection
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("RELEASE intent_event_write")
    # connection remains fully usable: next event write completes cleanly
    record_intent_event(conn, "WIX", "sizing", True, "after failure")
    conn2 = sqlite3.connect(db)
    assert conn2.execute(
        "SELECT COUNT(*) FROM intent_events WHERE symbol = 'WIX'"
    ).fetchone()[0] == 1
    conn2.close()
    conn.close()


class _ReleaseOnceFailingConn(sqlite3.Connection):
    """Connection whose next SAVEPOINT RELEASE raises (release-failure
    injection — review: '释放失败后连接必须仍可用')."""
    fail_next_release = False

    def execute(self, sql, *args, **kwargs):
        if self.fail_next_release and sql.lstrip().upper().startswith("RELEASE"):
            self.fail_next_release = False
            raise sqlite3.OperationalError("release boom")
        return super().execute(sql, *args, **kwargs)


def test_release_failure_leaves_caller_in_control(tmp_path):
    """R3: even if our own savepoint RELEASE fails mid-write, the caller
    retains full control (rollback/commit still works) and a later event
    write is clean."""
    db = tmp_path / "n.db"
    base = connect(db)
    base.close()
    conn = sqlite3.connect(db, factory=_ReleaseOnceFailingConn)
    conn.fail_next_release = True
    with pytest.raises(sqlite3.OperationalError, match="release boom"):
        record_intent_event(conn, "VEEV", "sizing", True, "x")
    conn.rollback()  # undoes the pending insert AND clears any savepoint state
    record_intent_event(conn, "VEEV", "sizing", True, "after recovery")
    conn2 = sqlite3.connect(db)
    assert conn2.execute(
        "SELECT COUNT(*) FROM intent_events").fetchone()[0] == 1
    conn2.close()
    conn.close()
