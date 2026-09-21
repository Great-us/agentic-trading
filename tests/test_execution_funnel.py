"""Funnel event tests for c2c_a7e2 A-2 (contract §14.2 / PLAN §三).

Three layers:

1. Unit — FunnelRecorder itself: dual destinations, fault isolation per
   destination, lazy payload construction, OrderObservation→kind mapping,
   once-per-order observation budget, event_id dedup, attempt threading.
2. Integration — the frozen-clock baseline scenario (borrowed from
   test_trade_trace_baseline, same fixture) now ALSO produces funnel events:
   decision_buy denominator, intent_created/replaced, gate reasons,
   order_submitted facts, and identity linkage from the cycle-1 intent
   version onto the cycle-2 flush events.
3. Invariance (the acceptance item, PLAN §七) — with observation healthy and
   with each observation stage broken (payload construction, SQLite write,
   JSONL emit), the recorded trade-call trace must equal the 97a65d6 fixture
   bit-for-bit, and intents/positions must be unchanged. Observation may
   never change what the trading path does.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))  # sibling scenario module import

from agentic_trading import execution_funnel
from agentic_trading.execution_funnel import (
    FunnelRecorder, attempt_id_of, decision_key_of, mint_run_id,
)
from agentic_trading.journal.logger import connect

from test_trade_trace_baseline import (
    FIXTURE_PATH, public_event, run_scenario,
)


def events(conn, *kinds) -> list[dict]:
    rows = conn.execute(
        "SELECT kind, symbol, deferred, detail, run_id, mode, decision_key, "
        "intent_id, attempt_id, order_id, payload FROM intent_events"
    ).fetchall()
    out = [dict(zip(
        ("kind", "symbol", "deferred", "detail", "run_id", "mode",
         "decision_key", "intent_id", "attempt_id", "order_id", "payload"),
        r)) for r in rows]
    return [e for e in out if not kinds or e["kind"] in kinds]


# --------------------------------------------------------------------------
# 1. Unit
# --------------------------------------------------------------------------

def test_record_reaches_sqlite_and_returns_event_id(tmp_path, monkeypatch):
    from agentic_trading.journal.logger import ROOT as BOOK_ROOT
    monkeypatch.setattr(execution_funnel.live_events, "LIVE_EVENTS_PATH",
                        tmp_path / "live.jsonl")
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, mint_run_id("2026-08-26T14:30:00+00:00"), "paper")
    event_id = rec.record("decision_buy", "BUYA", deferred=False)
    assert event_id
    rows = events(conn, "decision_buy")
    assert len(rows) == 1
    row = rows[0]
    # book prefix is each book root's own name (Trading / Trading-P2 / …)
    assert row["run_id"].startswith(f"{BOOK_ROOT.name}:2026-08-26T14:30:00+00:00:")
    assert row["mode"] == "paper"
    assert json.loads((tmp_path / "live.jsonl").read_text().splitlines()[0])["stage"] == "funnel"


def test_sqlite_failure_does_not_block_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(execution_funnel.live_events, "LIVE_EVENTS_PATH",
                        tmp_path / "live.jsonl")

    def boom(*a, **k):
        raise RuntimeError("sqlite down")

    monkeypatch.setattr(execution_funnel, "record_intent_event", boom)
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, mint_run_id("t0"), "paper")
    rec.record("decision_buy", "BUYA", deferred=False)  # must not raise
    assert events(conn) == []
    lines = (tmp_path / "live.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["kind"] == "decision_buy"


def test_jsonl_failure_does_not_block_sqlite(tmp_path, monkeypatch):
    conn = connect(":memory:")

    def boom(*a, **k):
        raise RuntimeError("jsonl down")

    monkeypatch.setattr(execution_funnel.live_events, "emit", boom)
    rec = FunnelRecorder(conn, mint_run_id("t0"), "paper")
    rec.record("decision_buy", "BUYA", deferred=False)  # must not raise
    assert len(events(conn, "decision_buy")) == 1


def test_payload_construction_failure_still_records_event(tmp_path, monkeypatch):
    monkeypatch.setattr(execution_funnel.live_events, "LIVE_EVENTS_PATH",
                        tmp_path / "live.jsonl")
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, mint_run_id("t0"), "paper")

    def boom():
        raise RuntimeError("cannot build payload")

    rec.record("order_submitted", "BUYA", deferred=False, payload=boom)
    rows = events(conn, "order_submitted")
    assert len(rows) == 1 and rows[0]["payload"] is None


def test_duplicate_event_id_not_double_counted(tmp_path):
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, mint_run_id("t0"), "paper")
    rec.record("order_filled", "BUYA", deferred=False, event_id="fixed-id")
    rec.record("order_filled", "BUYA", deferred=False, event_id="fixed-id")
    assert len(events(conn, "order_filled")) == 1


def test_attempt_id_threaded_from_intent_id(tmp_path):
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, mint_run_id("t0"), "paper")
    rec.record("flush_wait", "CHASE", deferred=True, intent_id="v-1",
               detail="not_before")
    (row,) = events(conn, "flush_wait")
    assert row["intent_id"] == "v-1"
    assert row["attempt_id"] == attempt_id_of("v-1", rec.run_id)


def test_run_level_skip_uses_star_symbol(tmp_path):
    conn = connect(":memory:")
    FunnelRecorder(conn, mint_run_id("t0"), "paper").flush_skipped("market_closed")
    (row,) = events(conn, "flush_skipped")
    assert row["symbol"] == "*"


class _ObsBroker:
    def __init__(self, observations):
        self.observations = observations
        self.calls: list[str] = []

    def observe_order(self, order_id):
        self.calls.append(order_id)
        return self.observations.get(order_id)


def _obs(status=None, filled_qty=None, error=None):
    from agentic_trading.execution.broker import OrderObservation
    return OrderObservation(order_id="o", status=status,
                            observed_at="2026-08-26T14:30:00+00:00",
                            filled_qty=filled_qty, filled_avg_price=None,
                            filled_at=None, error=error)


def test_observe_kind_mapping_and_budget(tmp_path):
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, mint_run_id("t0"), "paper")
    broker = _ObsBroker({
        "filled-1": _obs("filled", 10.0),
        "partial-1": _obs("partially_filled", 4.0),
        "dead-1": _obs("rejected"),
        "resting-1": _obs("accepted"),
        "unknown-1": _obs(None, error="boom"),
    })
    rec.observe("BUYA", "filled-1", broker, intent_id="v1")
    rec.observe("BUYB", "partial-1", broker, intent_id="v2")
    rec.observe("VETO", "dead-1", broker, intent_id="v3")
    rec.observe("CHASE", "resting-1", broker, intent_id="v4")
    rec.observe("SELLER", "unknown-1", broker, intent_id="v5")
    # budget: a second observation of the same order is a no-op
    rec.observe("BUYA", "filled-1", broker, intent_id="v1")
    assert broker.calls.count("filled-1") == 1  # once per order per cycle
    observed = [e for e in events(conn, "order_observed")]
    assert {o["order_id"] for o in observed} == {"dead-1", "resting-1"}
    assert len(events(conn, "order_filled")) == 1
    assert len(events(conn, "order_partial")) == 1
    (unknown,) = events(conn, "order_unknown")
    assert unknown["order_id"] == "unknown-1" and "boom" in (unknown["payload"] or "")


def test_observe_without_observe_order_degrades_to_unknown(tmp_path):
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, mint_run_id("t0"), "paper")

    class _OldFake:
        pass

    rec.observe("BUYA", "buy-1", _OldFake(), intent_id="v1")
    (row,) = events(conn, "order_unknown")
    assert row["order_id"] == "buy-1"
    assert "observe_unavailable" in (row["payload"] or "")


# --------------------------------------------------------------------------
# 2. Integration: the baseline scenario now emits funnel events
# --------------------------------------------------------------------------

def test_baseline_scenario_produces_the_full_funnel(monkeypatch, tmp_path):
    result = run_scenario(monkeypatch.setattr, tmp_path)
    runs = {}
    for e in events(result.conn):
        runs.setdefault(e["run_id"], []).append(e)
    assert len(runs) == 2, "two cycles, two run_ids"
    first = max(runs.values(), key=lambda ev: len([e for e in ev if e["kind"] == "intent_created"]))
    second = min(runs.values(), key=lambda ev: len([e for e in ev if e["kind"] == "intent_created"]))
    # cycle 1: four BUY decisions become four created intents
    assert len([e for e in first if e["kind"] == "decision_buy"]) == 4
    created = [e for e in first if e["kind"] == "intent_created"]
    assert sorted(e["symbol"] for e in created) == ["BUYA", "BUYB", "CHASE", "VETO"]
    assert all(e["intent_id"] for e in created)
    assert all(e["decision_key"] == decision_key_of(e["run_id"], e["symbol"]) for e in created)
    # cycle 2: two submits, one sizing veto, one chase wait, two replaced, two gated
    assert len([e for e in second if e["kind"] == "order_submitted"]) == 2
    veto = [e for e in second if e["kind"] == "sizing"]
    assert len(veto) == 1 and veto[0]["symbol"] == "VETO" and veto[0]["intent_id"]
    chase = [e for e in second if e["kind"] == "chase_signal"]
    assert len(chase) == 1 and chase[0]["symbol"] == "CHASE"
    replaced = [e for e in second if e["kind"] == "intent_replaced"]
    assert sorted(e["symbol"] for e in replaced) == ["CHASE", "VETO"]
    gated = [e for e in second if e["kind"] == "intent_not_created"]
    assert sorted((e["symbol"], e["detail"]) for e in gated) == \
        [("BUYA", "pending_buy"), ("BUYB", "pending_buy")]
    # identity threading: cycle-2 flush events carry the cycle-1 intent version
    buya_version = {e["intent_id"] for e in created if e["symbol"] == "BUYA"}
    submitted = [e for e in second if e["kind"] == "order_submitted" and e["symbol"] == "BUYA"]
    assert submitted[0]["intent_id"] in buya_version
    assert submitted[0]["order_id"] and json.loads(submitted[0]["payload"])["client_order_id"]
    # the trace fake self-observes submissions as accepted (R9): both orders
    # end the cycle as order_observed(accepted) — the REAL observation path
    submitted_ids = {e["order_id"] for e in second if e["kind"] == "order_submitted"}
    observed = [e for e in second if e["kind"] == "order_observed"]
    assert submitted_ids and {e["order_id"] for e in observed} == submitted_ids
    assert all('"accepted"' in (e["payload"] or "") or "accepted" in (e["payload"] or "")
               for e in observed)


def test_mode_is_paper_in_scenario(monkeypatch, tmp_path):
    result = run_scenario(monkeypatch.setattr, tmp_path)
    modes = {e["mode"] for e in events(result.conn)}
    assert modes == {"paper"}


# --------------------------------------------------------------------------
# 3. Invariance under observation faults (acceptance §14.5 / PLAN §七)
# --------------------------------------------------------------------------

def _assert_trace_matches_fixture(result):
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    observed = [public_event(e) for e in result.events]
    assert observed == fixture["expected_events"], \
        "observation changed the trading-call sequence — this is the one thing it may never do"
    assert sorted(i.symbol for i in result.intents) == ["CHASE", "VETO"]
    assert sorted(result.positions) == ["RATCHET"]


def test_trace_unchanged_with_healthy_observation(monkeypatch, tmp_path):
    _assert_trace_matches_fixture(run_scenario(monkeypatch.setattr, tmp_path))


def test_trace_unchanged_when_sqlite_event_writes_fail(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("sqlite event write down")

    monkeypatch.setattr(execution_funnel, "record_intent_event", boom)
    _assert_trace_matches_fixture(run_scenario(monkeypatch.setattr, tmp_path))


def test_trace_unchanged_when_jsonl_emit_fails(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("jsonl down")

    monkeypatch.setattr(execution_funnel.live_events, "emit", boom)
    _assert_trace_matches_fixture(run_scenario(monkeypatch.setattr, tmp_path))


def test_trace_unchanged_when_payload_construction_fails(monkeypatch, tmp_path):
    from agentic_trading.risk.manager import SizingDiagnostics
    monkeypatch.setattr(SizingDiagnostics, "to_dict",
                        lambda self: (_ for _ in ()).throw(RuntimeError("diag boom")))
    _assert_trace_matches_fixture(run_scenario(monkeypatch.setattr, tmp_path))


# --------------------------------------------------------------------------
# 4. ITERATION 2 / R1: the WHOLE observation call is inside the fault boundary
#    (review R1: identity minting, semantic field access, observation reads,
#    diagnostics construction — none of these may touch the trading path)
# --------------------------------------------------------------------------

def test_trace_unchanged_when_run_id_minting_fails(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("no entropy")

    # make_funnel resolves mint_run_id at module level — patching it here
    # must degrade the whole recorder to the null one (R1).
    monkeypatch.setattr(execution_funnel, "mint_run_id", boom)
    # The cycle must run to the same trace with a null recorder (R1: a
    # mint_run_id failure may not abort the cycle).
    _assert_trace_matches_fixture(run_scenario(monkeypatch.setattr, tmp_path))


def test_trace_unchanged_when_event_id_minting_fails(monkeypatch, tmp_path):
    from types import SimpleNamespace

    def boom():
        raise RuntimeError("uuid gone")

    # replace only the funnel module's uuid reference; logger's own uuid
    # (save_trade_intent) must keep working so intents still save
    monkeypatch.setattr(execution_funnel, "uuid", SimpleNamespace(uuid4=boom))
    result = run_scenario(monkeypatch.setattr, tmp_path)
    _assert_trace_matches_fixture(result)
    # funnel events are dropped silently, never fabricated, never raised
    assert events(result.conn) == []


def test_trace_unchanged_when_diagnostics_construction_fails(monkeypatch, tmp_path):
    import agentic_trading.risk.manager as rm

    class _Broken:
        def __init__(self, *a, **k):
            raise RuntimeError("diag ctor boom")

    monkeypatch.setattr(rm, "SizingDiagnostics", _Broken)
    # sizing must still approve the same notionals with diagnostics=None
    result = run_scenario(monkeypatch.setattr, tmp_path)
    _assert_trace_matches_fixture(result)


def test_observe_tolerates_raising_observation_object(tmp_path):
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, mint_run_id("t0"), "paper")

    class _PoisonObs:
        @property
        def status(self):
            raise RuntimeError("poisoned status")

    class _PoisonBroker:
        def observe_order(self, order_id):
            return _PoisonObs()

    rec.observe("BUYA", "o-1", _PoisonBroker(), intent_id="v1")  # must not raise
    rows = events(conn, "order_unknown")
    assert len(rows) == 1 and rows[0]["order_id"] == "o-1"
    assert "read_failed" in (rows[0]["payload"] or "")


def test_observe_tolerates_raising_broker_call(tmp_path):
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, mint_run_id("t0"), "paper")

    class _RaisingBroker:
        def observe_order(self, order_id):
            raise RuntimeError("observer exploded")

    rec.observe("BUYA", "o-2", _RaisingBroker(), intent_id="v1")  # must not raise
    rows = events(conn, "order_unknown")
    assert len(rows) == 1 and "observer exploded" in (rows[0]["payload"] or "")


def test_intent_saved_tolerates_broken_receipt(tmp_path):
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, mint_run_id("t0"), "paper")
    rec.intent_saved("BUYA", None)  # must not raise, must not fabricate
    rec.intent_saved("BUYA", object())  # missing attrs -> dropped, not raised
    assert events(conn, "intent_created") == [] and events(conn, "intent_replaced") == []


# --------------------------------------------------------------------------
# 5. ITERATION 2 / R2: a backtest cycle (asof set) must never touch the live
#    JSONL; its events only land in the caller-provided isolated journal
# --------------------------------------------------------------------------

def test_backtest_cycle_never_touches_live_jsonl(monkeypatch, tmp_path):
    from test_trade_trace_baseline import (
        FIXED_NOW, HarnessFeed, TraceBroker, frames, last_close, make_settings,
        neutral_regime,
    )
    from agentic_trading.journal.logger import connect as _connect, update_position_peak
    from agentic_trading.run import run_cycle

    sentinel = tmp_path / "live_events.jsonl"
    sentinel.write_bytes(b"SENTINEL\n")
    monkeypatch.setattr(execution_funnel.live_events, "LIVE_EVENTS_PATH", sentinel)

    frames_ = frames()
    live_prices = {s: last_close(frames_, s) for s in ("BUYA", "BUYB", "VETO", "CHASE")}
    monkeypatch.setattr("agentic_trading.run.datetime",
                        __import__("test_trade_trace_baseline").FrozenDateTime)
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda s: live_prices[s])
    monkeypatch.setattr("agentic_trading.run.time.sleep", lambda _s: None)
    monkeypatch.setattr("agentic_trading.heartbeat.HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr("agentic_trading.live_events.LIVE_EVENTS_PATH", sentinel)
    monkeypatch.setattr("agentic_trading.progress.PROGRESS_PATH", tmp_path / "progress.json")

    conn = _connect(":memory:")
    update_position_peak(conn, "SELLER", 100.0, FIXED_NOW.isoformat())
    update_position_peak(conn, "RATCHET", 125.0, FIXED_NOW.isoformat())
    broker = TraceBroker()
    settings = make_settings(sectors={"BUYA": "Technology", "BUYB": "Technology", "VETO": "Technology"})
    for _ in range(2):
        run_cycle(skip_llm=True, settings=settings, broker=broker, feed=HarnessFeed(frames_),
                  conn=conn, regime_model=neutral_regime, asof=FIXED_NOW)

    assert sentinel.read_bytes() == b"SENTINEL\n", "backtest wrote to the live JSONL (R2)"
    # isolated journal still receives the funnel ledger for the backtest run
    assert events(conn, "decision_buy"), "backtest events vanished from the isolated journal"


def test_flush_run_skip_respects_jsonl_flag(tmp_path, monkeypatch):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(execution_funnel.live_events, "emit",
                        lambda *a, **k: calls.append(a))
    execution_funnel.flush_run_skip("t0", "backtest", "market_closed", jsonl=False)
    assert calls == []
    execution_funnel.flush_run_skip("t0", "paper", "market_closed")
    assert len(calls) == 1


# --------------------------------------------------------------------------
# 6. ITERATION 2 / R4 execution side + R6: cross-cycle recovery, budget,
#    lifecycle ordering
# --------------------------------------------------------------------------

def test_cycle_end_observation_recovers_filled_order(monkeypatch, tmp_path):
    """R4: an order submitted in cycle 2 (intent already cleared) is observed
    at the END of cycle 3 with zero new submissions — the recovery reads the
    persistent ledger, the trace gains no new trading calls."""
    from test_trade_trace_baseline import (HarnessFeed, frames, make_settings,
                                            neutral_regime)
    from agentic_trading.run import run_cycle

    result = run_scenario(monkeypatch.setattr, tmp_path)
    submitted = [e for e in events(result.conn) if e["kind"] == "order_submitted"]
    assert len(submitted) == 2
    from agentic_trading.execution.broker import OrderObservation
    for row in submitted:
        result.broker.observations[row["order_id"]] = OrderObservation(
            order_id=row["order_id"], status="filled",
            observed_at="2026-08-26T14:31:00+00:00", filled_qty=1.0,
            filled_avg_price=100.0, filled_at="2026-08-26T14:30:30+00:00",
            error=None)
    from agentic_trading.journal.logger import clear_trade_intent as _clear
    # the kept VETO/CHASE intents would legitimately re-attempt in cycle 3 of
    # this fake world (buys never fill into positions); this test is about
    # RECOVERY, so retire them first.
    _clear(result.conn, "VETO")
    _clear(result.conn, "CHASE")
    buys_before = len([e for e in result.events if e["op"] == "buy"])
    run_cycle(skip_llm=True, settings=make_settings(), broker=result.broker,
              feed=HarnessFeed(frames()), conn=result.conn,
              regime_model=neutral_regime)
    # no new submissions in the third cycle
    assert len([e for e in result.events if e["op"] == "buy"]) == buys_before
    filled = [e for e in events(result.conn) if e["kind"] == "order_filled"]
    assert {e["order_id"] for e in filled} == {r["order_id"] for r in submitted}


def test_observation_budget_is_bounded(tmp_path):
    """R4: total observations per cycle are capped; this cycle's own
    submissions take priority over recovered ones."""
    from agentic_trading.execution.broker import OrderObservation
    conn = connect(":memory:")
    # seed the ledger with 10 resolved-less submitted orders
    for i in range(10):
        record = FunnelRecorder(conn, mint_run_id(f"r{i}"), "paper")
        record.order_submitted(f"S{i}", f"old-{i}", intent_id=None)
    rec = FunnelRecorder(conn, mint_run_id("now"), "paper")
    for i in range(3):
        rec.note_submission(f"P{i}", f"new-{i}")
    calls = []

    class _CountingBroker:
        def observe_order(self, order_id):
            calls.append(order_id)
            return OrderObservation(order_id=order_id, status="accepted",
                                    observed_at="t", filled_qty=None,
                                    filled_avg_price=None, filled_at=None,
                                    error=None)

    rec.observe_unresolved(conn, _CountingBroker())
    from agentic_trading.execution_funnel import MAX_ORDER_OBSERVATIONS_PER_CYCLE
    assert len(calls) == MAX_ORDER_OBSERVATIONS_PER_CYCLE
    assert calls[:3] == ["new-0", "new-1", "new-2"]  # own submissions first


def _direct_flush(monkeypatch, tmp_path, *, settings, positions, pending_buys, funnel, conn, orders_this_cycle=0, intent_symbols=()):
    """Call _flush_trade_intents directly with the baseline scenario's risk
    fixture and a frozen in-window clock. The funnel recorder must be built on
    the SAME conn so its SQLite events are inspectable."""
    import logging as _logging
    from test_trade_trace_baseline import FIXED_NOW, TraceBroker
    from agentic_trading.run import _flush_trade_intents
    from agentic_trading.journal.logger import save_trade_intent, TradeIntent
    for sym in sorted(pending_buys | set(positions) | set(intent_symbols)):
        save_trade_intent(conn, TradeIntent(
            symbol=sym, created_at=FIXED_NOW.isoformat(), signal_price=100.0,
            atr14=3.0, quant_score=0.5, combined_score=0.5, reasoning="t"))
    return _flush_trade_intents(
        broker=TraceBroker(), conn=conn, settings=settings,
        account=type("A", (), {"equity": 100_000.0, "cash": 100_000.0})(),
        positions=positions, pending_buys=pending_buys, peaks={},
        cash_remaining=100_000.0, invested_value=0.0, open_position_count=0,
        orders_this_cycle=orders_this_cycle, regime_multiplier=1.0,
        cycle_timestamp=FIXED_NOW.isoformat(), log=_logging.getLogger("t"),
        atrs={}, feed=None, asof=None, sizing_risk=settings.risk,
        sector_map={}, occupancy={}, now_utc=FIXED_NOW, funnel=funnel), conn


def test_intent_cleared_only_after_successful_clear(monkeypatch, tmp_path):
    """R6: a failed clear must leave NO intent_cleared terminal event."""
    from test_trade_trace_baseline import make_settings
    import agentic_trading.run as run_mod
    conn_holder = {}

    def failing_clear(conn, symbol):
        # _journal_safe only swallows sqlite3.Error by design — a programming
        # error MUST propagate; the realistic failure mode is a locked db.
        import sqlite3 as _sqlite3
        raise _sqlite3.OperationalError("db locked")

    monkeypatch.setattr(run_mod, "clear_trade_intent", failing_clear)
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, "test:0:x", "paper", live_emit=lambda *a, **k: None)
    _direct_flush(monkeypatch, tmp_path, settings=make_settings(),
                  positions={}, pending_buys={"CHASE"}, funnel=rec, conn=conn)
    assert events(conn, "intent_cleared") == []
    from agentic_trading.journal.logger import load_trade_intents
    assert [i.symbol for i in load_trade_intents(conn)] == ["CHASE"]


def test_max_orders_break_emits_run_level_event(monkeypatch, tmp_path):
    """R6: the break keeps its shape; one run-level event names the
    determinable remainder, no per-item fabrication."""
    from dataclasses import replace as _replace
    from test_trade_trace_baseline import RISK, make_settings
    settings = make_settings()
    settings.risk = _replace(RISK, max_new_orders_per_cycle=1)
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, "test:0:y", "paper", live_emit=lambda *a, **k: None)
    # intents exist but are neither held nor pending; the cycle has already
    # used its order budget -> the FIRST iteration hits the cap and breaks.
    _direct_flush(monkeypatch, tmp_path, settings=settings,
                  positions={}, pending_buys=set(), funnel=rec, conn=conn,
                  orders_this_cycle=1, intent_symbols=("CHASE", "VETO"))
    rows = [e for e in events(conn, "flush_skipped")]
    assert len(rows) == 1
    import json as _json
    payload = _json.loads(rows[0]["payload"] or "{}")
    assert payload["reason"] == "max_new_orders_reached"
    assert payload["limit"] == 1
    assert "unprocessed: CHASE, VETO" in rows[0]["detail"]


def test_both_destinations_receive_sanitized_payload(tmp_path, monkeypatch):
    """R7 closure: ONE sanitized copy reaches BOTH SQLite and the JSONL tail —
    the review's exact sentinel scenario must leak into neither."""
    monkeypatch.setattr(execution_funnel.live_events, "LIVE_EVENTS_PATH",
                        tmp_path / "live.jsonl")
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, mint_run_id("t0"), "paper")
    rec.record("sizing", "VETO", deferred=True,
               payload={"sizing": {"api_key": "SENTINEL", "note": "ok"},
                        "prompt": "SENTINEL", "reason": "cap"})
    rows = events(conn, "sizing")
    assert len(rows) == 1 and "SENTINEL" not in (rows[0]["payload"] or "")
    line = (tmp_path / "live.jsonl").read_text(encoding="utf-8").splitlines()[0]
    assert "SENTINEL" not in line and '"reason": "cap"' in line


# --------------------------------------------------------------------------
# 7. ITERATION 3 / R1 remainder: identity generation and early exits must not
#    change trading behavior (review round 2)
# --------------------------------------------------------------------------

def test_trace_unchanged_when_logger_uuid_minting_fails(monkeypatch, tmp_path):
    """R1-2: save_trade_intent mints its version BEFORE the business write;
    a uuid failure must degrade the identity, never the save itself."""
    from types import SimpleNamespace
    import agentic_trading.journal.logger as logger_mod

    def boom():
        raise RuntimeError("no entropy for intents")

    monkeypatch.setattr(logger_mod, "uuid", SimpleNamespace(uuid4=boom))
    result = run_scenario(monkeypatch.setattr, tmp_path)
    _assert_trace_matches_fixture(result)
    from agentic_trading.journal.logger import load_trade_intents
    assert sorted(i.symbol for i in load_trade_intents(result.conn)) == ["CHASE", "VETO"]
    created = [e for e in events(result.conn) if e["kind"] == "intent_created"]
    assert len(created) == 4 and all(e["intent_id"] is None for e in created)


def test_trace_unchanged_when_receipt_construction_fails(monkeypatch, tmp_path):
    """R1-2: the receipt is built AFTER the commit; its failure must not flip
    a committed save into WAIT + a false save_failed event."""
    import agentic_trading.journal.logger as logger_mod

    class _Broken:
        def __init__(self, *a, **k):
            raise RuntimeError("receipt ctor boom")

    monkeypatch.setattr(logger_mod, "IntentSaveReceipt", _Broken)
    result = run_scenario(monkeypatch.setattr, tmp_path)
    _assert_trace_matches_fixture(result)
    # no FALSE save_failed — pending_buy gates in later cycles are normal
    assert all(e["detail"] != "save_failed"
               for e in events(result.conn, "intent_not_created"))
    from agentic_trading.journal.logger import load_trade_intents
    # scenario end-state (BUYA/BUYB submitted+cleared); degraded receipts
    # must not lose the kept pair, and their identities stayed degraded
    kept = load_trade_intents(result.conn)
    assert sorted(i.symbol for i in kept) == ["CHASE", "VETO"]
    assert all(i.version for i in kept)


def test_sizing_early_exits_survive_diagnostics_failure():
    """R1-3: the three early exits (invalid price / max positions / invalid
    stop) must tolerate a diagnostics-construction failure and still return
    the original veto."""
    import agentic_trading.risk.manager as rm
    from agentic_trading.config import RiskConfig
    from test_trade_trace_baseline import RISK

    class _Broken:
        def __init__(self, *a, **k):
            raise RuntimeError("diag ctor boom")

    orig = rm.SizingDiagnostics
    rm.SizingDiagnostics = _Broken
    try:
        r1 = rm.size_position("X", last_price=-1.0, equity=100_000.0, cash=100_000.0,
                              invested_value=0.0, open_position_count=0, risk=RISK,
                              stop_pct=0.1, regime_multiplier=1.0,
                              existing_stop_risk=0.0, corr_multiplier=1.0, sector_room=None)
        assert r1.approved is False and r1.notional == 0.0 and r1.diagnostics is None
        r2 = rm.size_position("X", last_price=100.0, equity=100_000.0, cash=100_000.0,
                              invested_value=0.0, open_position_count=99, risk=RISK,
                              stop_pct=0.1, regime_multiplier=1.0,
                              existing_stop_risk=0.0, corr_multiplier=1.0, sector_room=None)
        assert r2.approved is False and r2.diagnostics is None
        r3 = rm.size_position("X", last_price=100.0, equity=100_000.0, cash=100_000.0,
                              invested_value=0.0, open_position_count=0, risk=RISK,
                              stop_pct=-0.1, regime_multiplier=1.0,
                              existing_stop_risk=0.0, corr_multiplier=1.0, sector_room=None)
        assert r3.approved is False and r3.diagnostics is None
    finally:
        rm.SizingDiagnostics = orig


# --------------------------------------------------------------------------
# 8. ITERATION 3 / R5 + R4 execution side: None is NOT a confirmed rejection;
#    a canceled order with a partial fill is TERMINAL; recovered observations
#    carry the original intent identity
# --------------------------------------------------------------------------

def test_none_submit_records_unknown_not_rejected(tmp_path, monkeypatch):
    """R5: a broker that swallows the exception and returns None is an UNKNOWN
    outcome (network failure included), never a confirmed rejection."""
    import logging as _logging
    from test_trade_trace_baseline import FIXED_NOW, TraceBroker
    from agentic_trading.run import _flush_trade_intents
    from agentic_trading.journal.logger import connect, save_trade_intent, TradeIntent

    class _NoneBroker(TraceBroker):
        def submit_notional_buy(self, *a, **k):
            return None

        def fetch_last_price(self, s):  # pragma: no cover - not reached
            return None

    conn = connect(":memory:")
    save_trade_intent(conn, TradeIntent(symbol="BUYA", created_at=FIXED_NOW.isoformat(),
                                        signal_price=100.0, atr14=3.0, quant_score=0.5,
                                        combined_score=0.5, reasoning="t"))
    import agentic_trading.run as run_mod
    from test_trade_trace_baseline import RISK, make_settings
    monkeypatch.setattr(run_mod, "fetch_last_price", lambda s: 100.0)
    settings = make_settings()
    _flush_trade_intents(
        broker=_NoneBroker(), conn=conn, settings=settings,
        account=type("A", (), {"equity": 100_000.0, "cash": 100_000.0})(),
        positions={}, pending_buys=set(), peaks={},
        cash_remaining=100_000.0, invested_value=0.0, open_position_count=0,
        orders_this_cycle=0, regime_multiplier=1.0,
        cycle_timestamp=FIXED_NOW.isoformat(), log=_logging.getLogger("t"),
        atrs={"BUYA": 3.0}, feed=None, asof=None, sizing_risk=RISK,
        sector_map={}, occupancy={}, now_utc=FIXED_NOW,
        funnel=FunnelRecorder(conn, "test:0:u", "paper",
                              live_emit=lambda *a, **k: None))
    # live price must be patchable: use the module-level fetch
    rows = [e for e in events(conn) if e["kind"] == "order_submitted"]
    assert rows, "no submit event"
    import json as _json
    assert _json.loads(rows[0]["payload"])["submit_status"] == "unknown"


def test_canceled_with_partial_fill_is_terminal(tmp_path):
    """R5: observation status=canceled + filled_qty>0 keeps BOTH facts — the
    partial fill AND the terminal status — and the recovery query must not
    re-poll this order in later cycles."""
    from agentic_trading.execution.broker import OrderObservation
    from agentic_trading.execution_funnel import FunnelRecorder, mint_run_id
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, mint_run_id("t0"), "paper")
    rec.note_submission("X", "ord-c")
    rec.observe("X", "ord-c", _ObsBroker({
        "ord-c": OrderObservation(order_id="ord-c", status="canceled",
                                  observed_at="t", filled_qty=2.0,
                                  filled_avg_price=10.0, filled_at=None, error=None),
    }))
    rows = [e for e in events(conn) if e["kind"] in ("order_partial", "order_observed")]
    assert len(rows) == 1
    import json as _json
    payload = _json.loads(rows[0]["payload"] or "{}")
    assert payload.get("order_status") == "canceled"   # terminal fact preserved
    assert payload.get("filled_qty") == 2.0            # partial fact preserved
    # recovery must treat it as terminal now
    unresolved = rec._unresolved_from_ledger(conn)
    assert ("X", "ord-c") not in [(s, o) for s, o, _i in unresolved]


def test_recovered_observations_carry_intent_id(tmp_path):
    """R4: a recovered unresolved order keeps the intent_id from its original
    submit event, so the observation lands on the original chain."""
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, mint_run_id("r0"), "paper")
    rec.order_submitted("BUYA", "ord-9", intent_id="v-1")
    unresolved = rec._unresolved_from_ledger(conn)
    assert unresolved == [("BUYA", "ord-9", "v-1")]


# --------------------------------------------------------------------------
# 9. ITERATION 3 / R6 remainder: leader-side structured unprocessed list and
#    the clear_failed attribution event
# --------------------------------------------------------------------------

def test_clear_failure_records_clear_failed_event(monkeypatch, tmp_path):
    """R6-3: a failed clear leaves no false terminal, but it must not be
    invisible either — a non-terminal clear_failed event names the symbol."""
    import sqlite3 as _sqlite3
    import logging as _logging
    import agentic_trading.run as run_mod
    from test_trade_trace_baseline import FIXED_NOW, RISK, make_settings
    from agentic_trading.run import _flush_trade_intents

    def failing_clear(conn, symbol):
        raise _sqlite3.OperationalError("db locked")

    monkeypatch.setattr(run_mod, "clear_trade_intent", failing_clear)
    conn = connect(":memory:")
    from agentic_trading.journal.logger import save_trade_intent, TradeIntent
    save_trade_intent(conn, TradeIntent(symbol="CHASE", created_at=FIXED_NOW.isoformat(),
                                        signal_price=100.0, atr14=3.0, quant_score=0.5,
                                        combined_score=0.5, reasoning="t"))
    rec = FunnelRecorder(conn, "test:0:cf", "paper", live_emit=lambda *a, **k: None)
    _flush_trade_intents(
        broker=type("B", (), {"get_open_orders": lambda self: []})(),
        conn=conn, settings=make_settings(),
        account=type("A", (), {"equity": 100_000.0, "cash": 100_000.0})(),
        positions={}, pending_buys={"CHASE"}, peaks={},
        cash_remaining=100_000.0, invested_value=0.0, open_position_count=0,
        orders_this_cycle=0, regime_multiplier=1.0,
        cycle_timestamp=FIXED_NOW.isoformat(), log=_logging.getLogger("t"),
        atrs={}, feed=None, asof=None, sizing_risk=RISK,
        sector_map={}, occupancy={}, now_utc=FIXED_NOW, funnel=rec)
    failed = [e for e in events(conn) if e["kind"] == "clear_failed"]
    assert len(failed) == 1 and failed[0]["symbol"] == "CHASE"
    assert failed[0]["deferred"] == 0
    # and still no false terminal
    assert events(conn, "intent_cleared") == []


def test_order_cap_event_carries_structured_unprocessed(monkeypatch, tmp_path):
    """R6-3: the capped flush event exposes the affected symbols as a
    structured payload list (whitelisted), not just prose in detail."""
    import json as _json
    import logging as _logging
    from dataclasses import replace as _replace
    from test_trade_trace_baseline import FIXED_NOW, RISK, make_settings
    from agentic_trading.run import _flush_trade_intents

    settings = make_settings()
    settings.risk = _replace(RISK, max_new_orders_per_cycle=1)
    conn = connect(":memory:")
    from agentic_trading.journal.logger import save_trade_intent, TradeIntent
    for sym in ("CHASE", "VETO"):
        save_trade_intent(conn, TradeIntent(symbol=sym, created_at=FIXED_NOW.isoformat(),
                                            signal_price=100.0, atr14=3.0, quant_score=0.5,
                                            combined_score=0.5, reasoning="t"))
    rec = FunnelRecorder(conn, "test:0:cap", "paper", live_emit=lambda *a, **k: None)
    _flush_trade_intents(
        broker=type("B", (), {"get_open_orders": lambda self: []})(),
        conn=conn, settings=settings,
        account=type("A", (), {"equity": 100_000.0, "cash": 100_000.0})(),
        positions={}, pending_buys=set(), peaks={},
        cash_remaining=100_000.0, invested_value=0.0, open_position_count=0,
        orders_this_cycle=1, regime_multiplier=1.0,
        cycle_timestamp=FIXED_NOW.isoformat(), log=_logging.getLogger("t2"),
        atrs={}, feed=None, asof=None, sizing_risk=RISK,
        sector_map={}, occupancy={}, now_utc=FIXED_NOW, funnel=rec)
    rows = [e for e in events(conn, "flush_skipped")]
    assert len(rows) == 1
    payload = _json.loads(rows[0]["payload"] or "{}")
    assert sorted(payload.get("unprocessed") or []) == ["CHASE", "VETO"]


# --------------------------------------------------------------------------
# 10. ITERATION 4 / R4 remainder: recovery respects mode + never picks a side
#     on conflicting order ownership; R6-A: TTL/gap clear failures are
#     attributed (review round 3)
# --------------------------------------------------------------------------

def test_recovery_respects_mode_filter(tmp_path):
    """R4: an order submitted under dry_run must not be recovered (or steal
    identity) by a paper-mode recorder on the same journal."""
    conn = connect(":memory:")
    dry = FunnelRecorder(conn, mint_run_id("dry:0"), "dry_run")
    dry.order_submitted("X", "ord-mode", intent_id="v-dry")
    paper = FunnelRecorder(conn, mint_run_id("p:0"), "paper")
    unresolved = paper._unresolved_from_ledger(conn)
    assert unresolved == []  # the dry_run order is not paper's business
    assert dry._unresolved_from_ledger(conn) == [("X", "ord-mode", "v-dry")]


def test_recovery_conflict_yields_no_identity(tmp_path):
    """R4: when the SAME order_id has submits from two different intents
    (same mode), the recovery must not pick the newest/first — the order is
    returned with intent_id=None so aggregation's conflict handling decides."""
    conn = connect(":memory:")
    a = FunnelRecorder(conn, mint_run_id("a:0"), "paper")
    a.order_submitted("X", "ord-x", intent_id="v-a")
    b = FunnelRecorder(conn, mint_run_id("b:0"), "paper")
    b.order_submitted("X", "ord-x", intent_id="v-b")
    rec = FunnelRecorder(conn, mint_run_id("c:0"), "paper")
    unresolved = rec._unresolved_from_ledger(conn)
    assert unresolved == [("X", "ord-x", None)]


def _ttl_gap_flush(monkeypatch, conn, rec, *, kind, settings):
    import logging as _logging
    from datetime import timedelta
    from test_trade_trace_baseline import FIXED_NOW, RISK, TraceBroker
    from agentic_trading.run import _flush_trade_intents, clear_trade_intent as _orig_clear
    import agentic_trading.run as run_mod
    from agentic_trading.journal.logger import save_trade_intent, TradeIntent

    if kind == "ttl":
        created = (FIXED_NOW - timedelta(hours=96)).isoformat()  # TTL is 72h
    else:
        created = FIXED_NOW.isoformat()  # fresh: only the price gap fires
    save_trade_intent(conn, TradeIntent(symbol="OLD", created_at=created,
                                        signal_price=100.0, atr14=3.0, quant_score=0.5,
                                        combined_score=0.5, reasoning="t"))
    if kind == "gap":
        monkeypatch.setattr(run_mod, "fetch_last_price", lambda s: 200.0)
    else:  # ttl: any live price works; gap must NOT trigger
        monkeypatch.setattr(run_mod, "fetch_last_price", lambda s: 100.0)

    def failing_clear(c, symbol):
        import sqlite3 as _sq
        raise _sq.OperationalError("db locked")

    monkeypatch.setattr(run_mod, "clear_trade_intent", failing_clear)
    _flush_trade_intents(
        broker=TraceBroker(), conn=conn, settings=settings,
        account=type("A", (), {"equity": 100_000.0, "cash": 100_000.0})(),
        positions={}, pending_buys=set(), peaks={},
        cash_remaining=100_000.0, invested_value=0.0, open_position_count=0,
        orders_this_cycle=0, regime_multiplier=1.0,
        cycle_timestamp=FIXED_NOW.isoformat(), log=_logging.getLogger("t"),
        atrs={"OLD": 3.0}, feed=None, asof=None, sizing_risk=RISK,
        sector_map={}, occupancy={}, now_utc=FIXED_NOW, funnel=rec)


def test_ttl_clear_failure_is_attributed(monkeypatch, tmp_path):
    """R6-A: a TTL discard whose DELETE fails records a non-terminal
    clear_failed(reason=ttl) — no false 'discarded' terminal, intent stays."""
    import sqlite3 as _sq
    import agentic_trading.run as run_mod
    from test_trade_trace_baseline import make_settings
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, "test:0:ttl", "paper", live_emit=lambda *a, **k: None)
    _ttl_gap_flush(monkeypatch, conn, rec, kind="ttl", settings=make_settings())
    failed = [e for e in events(conn, "clear_failed")]
    assert len(failed) == 1 and failed[0]["symbol"] == "OLD"
    import json as _json
    assert _json.loads(failed[0]["payload"] or "{}")["reason"] == "ttl"
    assert events(conn, "ttl") == []  # no false success terminal
    from agentic_trading.journal.logger import load_trade_intents
    assert [i.symbol for i in load_trade_intents(conn)] == ["OLD"]


def test_gap_clear_failure_is_attributed(monkeypatch, tmp_path):
    """R6-A: same attribution for the gap-discard branch."""
    from test_trade_trace_baseline import make_settings
    conn = connect(":memory:")
    rec = FunnelRecorder(conn, "test:0:gap", "paper", live_emit=lambda *a, **k: None)
    _ttl_gap_flush(monkeypatch, conn, rec, kind="gap", settings=make_settings())
    failed = [e for e in events(conn, "clear_failed")]
    assert len(failed) == 1
    import json as _json
    assert _json.loads(failed[0]["payload"] or "{}")["reason"] == "gap"
    assert events(conn, "gap") == []
