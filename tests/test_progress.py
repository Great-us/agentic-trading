"""Operator handoff card: schema, next-job calendar, isolation from live data/."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentic_trading import progress as progress_mod
from agentic_trading.data.market_data import ET
from agentic_trading.progress import (
    emit_progress,
    infer_book_id,
    next_job_p1,
    next_job_p3,
    read_progress,
    write_progress,
)


WED = datetime(2026, 9, 9, 14, 0, tzinfo=ET)          # Wednesday 14:00 ET
AFTER_CLOSE = datetime(2026, 9, 9, 16, 20, tzinfo=ET)  # after 16:15 deep
FRIDAY_EVE = datetime(2026, 9, 11, 16, 35, tzinfo=ET)
SATURDAY = datetime(2026, 9, 12, 12, 0, tzinfo=ET)
MIDDAY_DONE = datetime(2026, 9, 9, 10, 45, tzinfo=ET)


def test_conftest_redirects_progress_path(tmp_path):
    assert progress_mod.PROGRESS_PATH == tmp_path / "progress.json"


def test_write_and_read_roundtrip(tmp_path):
    path = tmp_path / "card.json"
    emit_progress(
        book_id="p1", round_name="fast", status="ok", round_id="12",
        asof="2026-09-09", did=["1 order"], did_not=["no LLM"],
        broker={"equity": 10000, "cash": 2000, "n_positions": 3},
        risk={"exposure_pct": 0.80, "stops_covered": 3, "stops_total": 3},
        now=WED, path=path,
    )
    card = read_progress(path)
    assert card is not None
    assert card["schema_version"] == 1
    assert card["round"]["status"] == "ok"
    assert card["did"] == ["1 order"]
    assert card["next_job"]["slot"] in {"fast", "deep"}
    assert "instruction" in card["next_job"]
    assert "api_key" not in card


def test_missing_file_is_none(tmp_path):
    assert read_progress(tmp_path / "nope.json") is None


def test_corrupt_file_is_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_progress(path) is None


def test_write_strips_secrets(tmp_path):
    path = tmp_path / "card.json"
    write_progress({"book_id": "p1", "alpaca_api_key": "AKIA", "did": ["x"]}, path=path)
    text = path.read_text(encoding="utf-8")
    assert "AKIA" not in text
    assert "alpaca_api_key" not in text


def test_write_never_raises(tmp_path, monkeypatch):
    import agentic_trading.progress as mod

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(mod.Path, "mkdir", boom)
    write_progress({"book_id": "p1"}, path=tmp_path / "blocked" / "progress.json")


def test_next_job_p1_after_afternoon_is_next_session_open():
    job = next_job_p1(AFTER_CLOSE.astimezone(timezone.utc), last_round="deep")
    # Fast scan starts 09:35, before the 09:45 deep — that is the real next task.
    assert job["slot"] == "fast"
    assert job["when_et"] == "09:35"
    assert "flush" in job["instruction"]


def test_next_job_p1_midday_is_fast_scan():
    job = next_job_p1(WED.astimezone(timezone.utc), last_round="fast")
    assert job["slot"] == "fast"
    assert "flush" in job["instruction"]


def test_next_job_p1_weekend_skips_to_monday():
    job = next_job_p1(SATURDAY.astimezone(timezone.utc), last_round="deep")
    assert job["slot"] == "fast"
    assert job["when_et"] == "09:35"
    when = datetime.fromisoformat(job["when"])
    assert when.astimezone(ET).weekday() == 0


def test_next_job_p3_after_midday_is_evening():
    job = next_job_p3(MIDDAY_DONE.astimezone(timezone.utc), last_round="midday")
    assert job["slot"] == "evening"
    assert job["when_et"] == "16:30"
    assert "禁止新开仓" in job["instruction"]


def test_next_job_p3_after_evening_is_next_midday():
    job = next_job_p3(FRIDAY_EVE.astimezone(timezone.utc), last_round="evening")
    assert job["slot"] == "midday"
    assert job["when_et"] == "10:30"
    when = datetime.fromisoformat(job["when"])
    assert when.astimezone(ET).weekday() == 0  # Monday


def test_next_job_p3_flatten_incomplete_forbids_entries():
    job = next_job_p3(FRIDAY_EVE.astimezone(timezone.utc),
                      last_round="flatten", status="incomplete")
    assert job["slot"] == "flatten"
    assert "禁止新开仓" in job["instruction"]


def test_next_job_p3_flatten_failed_forbids_entries():
    job = next_job_p3(FRIDAY_EVE.astimezone(timezone.utc),
                      last_round="flatten", status="failed")
    assert job["slot"] == "flatten"
    assert "禁止新开仓" in job["instruction"]


def test_next_job_p3_healthy_flatten_points_at_next_midday_not_entries_now():
    job = next_job_p3(FRIDAY_EVE.astimezone(timezone.utc),
                      last_round="flatten", status="ok")
    assert job["slot"] == "midday"
    assert "禁止新开仓" in job["instruction"]
    when = datetime.fromisoformat(job["when"])
    assert when.astimezone(ET).weekday() == 0


def test_infer_book_id_from_folder_name(tmp_path):
    assert infer_book_id(tmp_path / "Trading") == "p1"
    assert infer_book_id(tmp_path / "Trading-P2") == "p2"
    assert infer_book_id(tmp_path / "Trading-P3") == "p3"
    assert infer_book_id(tmp_path / "Trading-P4") == "p4"


class _FakeReader:
    """Stands in for BookBrokerReader so _stamp_cycle_progress never makes a
    real network call in tests. fills/positions are set per test."""

    def __init__(self, fills=None, fills_reason=None, truncated=False):
        self._fills = fills
        self._fills_reason = fills_reason
        self.last_fills_truncated = truncated

    def __call__(self, _book_root):  # used as a monkeypatched class replacement
        return self

    def fills(self):
        return self._fills, self._fills_reason


def test_stamp_cycle_progress_live_writes(monkeypatch):
    from types import SimpleNamespace

    import agentic_trading.run as run_mod

    monkeypatch.setattr(run_mod, "BookBrokerReader", _FakeReader(fills=None, fills_reason="no creds"))

    run_mod._stamp_cycle_progress(
        asof=None, fast_mode=True, cycle_no=7,
        account=SimpleNamespace(equity=10000.0, cash=2000.0),
        positions={"AAA": object()}, orders_this_cycle=0, status="ok",
    )
    card = read_progress()
    assert card is not None
    assert card["round"]["name"] == "fast"
    assert card["round"]["round_id"] == "7"
    assert card["broker"]["n_positions"] == 1
    assert card["broker"]["equity"] == 10000.0
    assert card["next_job"]["instruction"]


def test_stamp_cycle_progress_position_mismatch_reaches_unresolved(monkeypatch):
    """The real VEEV case (AGENTS.md F1): fills say a symbol is still open,
    but the broker positions handed to _stamp_cycle_progress don't have it."""
    from types import SimpleNamespace

    import agentic_trading.run as run_mod

    fills = [
        {"symbol": "VEEV", "side": "buy", "qty": 5.83, "price": 263.84,
         "notional": 1538.55, "transaction_time": "2026-09-16T15:15:00Z"},
    ]
    monkeypatch.setattr(run_mod, "BookBrokerReader", _FakeReader(fills=fills, fills_reason=None))

    run_mod._stamp_cycle_progress(
        asof=None, fast_mode=False, cycle_no=1,
        account=SimpleNamespace(equity=8263.29, cash=3634.75),
        positions={}, orders_this_cycle=0, status="ok",
    )
    card = read_progress()
    kinds = [u["kind"] for u in card["unresolved"]]
    assert "position_mismatch" in kinds
    detail = next(u["detail"] for u in card["unresolved"] if u["kind"] == "position_mismatch")
    assert "VEEV" in detail


def test_stamp_cycle_progress_matching_positions_stay_clean(monkeypatch):
    """一致：fills 派生的持仓与传入的 broker positions 完全匹配 → 不产生 unresolved。"""
    from types import SimpleNamespace

    import agentic_trading.run as run_mod

    fills = [
        {"symbol": "MSFT", "side": "buy", "qty": 2.0, "price": 450.0,
         "notional": 900.0, "transaction_time": "2026-09-01T15:00:00Z"},
    ]
    monkeypatch.setattr(run_mod, "BookBrokerReader", _FakeReader(fills=fills, fills_reason=None))

    run_mod._stamp_cycle_progress(
        asof=None, fast_mode=True, cycle_no=2,
        account=SimpleNamespace(equity=10000.0, cash=8000.0),
        positions={"MSFT": SimpleNamespace(qty=2.0)}, orders_this_cycle=0, status="ok",
    )
    card = read_progress()
    kinds = [u["kind"] for u in card["unresolved"]]
    assert "position_mismatch" not in kinds


def test_stamp_cycle_progress_qty_mismatch_is_flagged(monkeypatch):
    """数量不等：broker 持仓数量与 fills 派生数量不符。"""
    from types import SimpleNamespace

    import agentic_trading.run as run_mod

    fills = [
        {"symbol": "IQV", "side": "buy", "qty": 2.0, "price": 248.61,
         "notional": 497.22, "transaction_time": "2026-09-01T15:00:00Z"},
    ]
    monkeypatch.setattr(run_mod, "BookBrokerReader", _FakeReader(fills=fills, fills_reason=None))

    run_mod._stamp_cycle_progress(
        asof=None, fast_mode=True, cycle_no=3,
        account=SimpleNamespace(equity=10000.0, cash=8000.0),
        positions={"IQV": SimpleNamespace(qty=2.5)}, orders_this_cycle=0, status="ok",
    )
    card = read_progress()
    detail = next(u["detail"] for u in card["unresolved"] if u["kind"] == "position_mismatch")
    assert "IQV" in detail


def test_stamp_cycle_progress_broker_only_symbol_is_flagged(monkeypatch):
    """少一只（fills 侧）：broker 有持仓，fills 派生记录里没有对应的未平仓回合。"""
    from types import SimpleNamespace

    import agentic_trading.run as run_mod

    monkeypatch.setattr(run_mod, "BookBrokerReader", _FakeReader(fills=[], fills_reason=None))

    run_mod._stamp_cycle_progress(
        asof=None, fast_mode=True, cycle_no=4,
        account=SimpleNamespace(equity=10000.0, cash=8000.0),
        positions={"NVDA": SimpleNamespace(qty=8.0)}, orders_this_cycle=0, status="ok",
    )
    card = read_progress()
    detail = next(u["detail"] for u in card["unresolved"] if u["kind"] == "position_mismatch")
    assert "NVDA" in detail


def test_stamp_cycle_progress_empty_fills_still_reconciles_r4(monkeypatch):
    """R4 (ChatGPT review, PROGRESS.md §10): an empty fills history is a
    normal, complete fetch (broker_read.fills() no longer marks it
    truncated) — it must still be compared against real broker positions,
    not skipped the way a genuinely truncated/incomplete fetch is."""
    from types import SimpleNamespace

    import agentic_trading.run as run_mod

    monkeypatch.setattr(
        run_mod, "BookBrokerReader",
        _FakeReader(fills=[], fills_reason=None, truncated=False),
    )

    run_mod._stamp_cycle_progress(
        asof=None, fast_mode=True, cycle_no=6,
        account=SimpleNamespace(equity=10000.0, cash=8000.0),
        positions={"NVDA": SimpleNamespace(qty=8.0)}, orders_this_cycle=0, status="ok",
    )
    card = read_progress()
    kinds = [u["kind"] for u in card["unresolved"]]
    assert "fills_truncated" not in kinds  # not misreported as incomplete
    detail = next(u["detail"] for u in card["unresolved"] if u["kind"] == "position_mismatch")
    assert "NVDA" in detail


def test_stamp_cycle_progress_reconciliation_failure_never_blocks_the_write(monkeypatch):
    """A broken reader must not stop the cycle from stamping progress at all."""
    from types import SimpleNamespace

    import agentic_trading.run as run_mod

    class BoomReader:
        def __init__(self, _book_root):
            pass

        def fills(self):
            raise RuntimeError("network exploded")

    monkeypatch.setattr(run_mod, "BookBrokerReader", BoomReader)

    run_mod._stamp_cycle_progress(
        asof=None, fast_mode=True, cycle_no=5,
        account=SimpleNamespace(equity=10000.0, cash=8000.0),
        positions={}, orders_this_cycle=0, status="ok",
    )
    card = read_progress()
    assert card is not None
    assert card["round"]["round_id"] == "5"


def test_stamp_cycle_progress_dry_run_skips_reconciliation(monkeypatch):
    """DryRunBroker.get_positions() is always {}, so under --dry-run every
    real fill would look like a vanished position — dry_run=True must skip
    the fills-vs-positions check entirely rather than false-alarm."""
    from types import SimpleNamespace

    import agentic_trading.run as run_mod

    fills = [
        {"symbol": "VEEV", "side": "buy", "qty": 5.83, "price": 263.84,
         "notional": 1538.55, "transaction_time": "2026-09-16T15:15:00Z"},
    ]
    monkeypatch.setattr(run_mod, "BookBrokerReader", _FakeReader(fills=fills, fills_reason=None))

    run_mod._stamp_cycle_progress(
        asof=None, fast_mode=True, cycle_no=6,
        account=SimpleNamespace(equity=10000.0, cash=8000.0),
        positions={}, orders_this_cycle=0, status="ok", dry_run=True,
    )
    card = read_progress()
    kinds = [u["kind"] for u in card["unresolved"]]
    assert "position_mismatch" not in kinds


def test_stamp_cycle_progress_truncated_fills_report_incomplete_not_diffs(monkeypatch):
    """When broker_read hit its max_records cap, the fills window may be
    missing an open lot's opening buy — reporting per-symbol diffs off an
    incomplete window would be a false alarm. Report incompleteness instead."""
    from types import SimpleNamespace

    import agentic_trading.run as run_mod

    fills = [
        {"symbol": "VEEV", "side": "buy", "qty": 5.83, "price": 263.84,
         "notional": 1538.55, "transaction_time": "2026-09-16T15:15:00Z"},
    ]
    monkeypatch.setattr(
        run_mod, "BookBrokerReader",
        _FakeReader(fills=fills, fills_reason=None, truncated=True),
    )

    run_mod._stamp_cycle_progress(
        asof=None, fast_mode=True, cycle_no=8,
        account=SimpleNamespace(equity=10000.0, cash=8000.0),
        positions={}, orders_this_cycle=0, status="ok",
    )
    card = read_progress()
    kinds = [u["kind"] for u in card["unresolved"]]
    assert "fills_truncated" in kinds
    assert "position_mismatch" not in kinds


def test_stamp_cycle_progress_missing_credentials_logs_warning_not_traceback(monkeypatch, caplog):
    """P4 has no engine-visible Alpaca creds: BrokerError fires every cycle.
    That must be a one-line warning, not a full exception traceback."""
    import logging as logging_mod
    from types import SimpleNamespace

    import agentic_trading.run as run_mod
    from agentic_trading.broker_read import BrokerError

    class _CredsMissingReader:
        def __init__(self, _book_root):
            pass

        def fills(self):
            raise BrokerError("no ALPACA credentials in /fake/.env")

    monkeypatch.setattr(run_mod, "BookBrokerReader", _CredsMissingReader)

    with caplog.at_level(logging_mod.WARNING, logger="run_cycle"):
        run_mod._stamp_cycle_progress(
            asof=None, fast_mode=True, cycle_no=9,
            account=SimpleNamespace(equity=10000.0, cash=8000.0),
            positions={}, orders_this_cycle=0, status="ok",
        )
    warnings = [r for r in caplog.records if r.levelno == logging_mod.WARNING]
    assert any("fills reconciliation skipped" in r.getMessage() for r in warnings)
    assert not any(r.exc_info for r in caplog.records)


def test_stamp_cycle_progress_skips_backtest():
    from agentic_trading.run import _stamp_cycle_progress

    if progress_mod.PROGRESS_PATH.exists():
        progress_mod.PROGRESS_PATH.unlink()
    _stamp_cycle_progress(
        asof="2026-09-09", fast_mode=False, cycle_no=1,
        account=None, positions=None, status="ok",
    )
    assert read_progress() is None


# ---- R3 (ChatGPT review, PROGRESS.md §9): positions=None must not be -----
# ---- treated as a real empty book and reconciled against fills. ---------

class _CountingReader:
    """Tracks whether BookBrokerReader was ever constructed, so a test can
    assert the reconciliation never even attempted a broker read when
    positions is None — not just that its output was empty."""

    calls = 0

    def __init__(self, _book_root):
        type(self).calls += 1

    def fills(self):
        raise AssertionError("fills() must not be called when positions is None")


def test_positions_none_skips_reconciliation_entirely(monkeypatch):
    """Unit-level: the helper itself, isolated from _stamp_cycle_progress."""
    import agentic_trading.run as run_mod

    _CountingReader.calls = 0
    monkeypatch.setattr(run_mod, "BookBrokerReader", _CountingReader)

    result = run_mod._fills_reconciliation_unresolved(None)
    assert _CountingReader.calls == 0
    assert [r["kind"] for r in result] == ["positions_unavailable"]


def test_positions_empty_dict_still_reconciles(monkeypatch):
    """A real empty book ({}) is not None — it must still be compared
    against fills, the same VEEV-shaped case as the existing position-
    mismatch test, just phrased as the is-None/is-empty distinction itself."""
    import agentic_trading.run as run_mod

    fills = [
        {"symbol": "VEEV", "side": "buy", "qty": 5.83, "price": 263.84,
         "notional": 1538.55, "transaction_time": "2026-09-16T15:15:00Z"},
    ]
    monkeypatch.setattr(run_mod, "BookBrokerReader", _FakeReader(fills=fills, fills_reason=None))

    result = run_mod._fills_reconciliation_unresolved({})
    assert [r["kind"] for r in result] == ["position_mismatch"]
    assert "VEEV" in result[0]["detail"]


def test_ambiguous_symbol_reaches_progress_as_undetermined_not_mismatch(monkeypatch):
    """R5: an order-ambiguous same-timestamp fill group must surface with
    its own kind (position_undetermined), not be conflated with a confirmed
    position_mismatch — even though both currently land in `unresolved`."""
    from types import SimpleNamespace

    import agentic_trading.run as run_mod

    fills = [
        {"symbol": "X", "side": "buy", "qty": 1.0, "price": 100.0,
         "transaction_time": "2026-08-27T15:00:00Z"},
        # Same timestamp, no id — ambiguous.
        {"symbol": "X", "side": "sell", "qty": 1.0, "price": 110.0,
         "transaction_time": "2026-08-27T16:00:00Z"},
        {"symbol": "X", "side": "buy", "qty": 1.0, "price": 108.0,
         "transaction_time": "2026-08-27T16:00:00Z"},
    ]
    monkeypatch.setattr(run_mod, "BookBrokerReader", _FakeReader(fills=fills, fills_reason=None))

    run_mod._stamp_cycle_progress(
        asof=None, fast_mode=True, cycle_no=9,
        account=SimpleNamespace(equity=10000.0, cash=8000.0),
        positions={"X": SimpleNamespace(qty=1.0)}, orders_this_cycle=0, status="ok",
    )
    card = read_progress()
    kinds = [u["kind"] for u in card["unresolved"]]
    assert "position_undetermined" in kinds
    assert "position_mismatch" not in kinds


def test_partial_prefix_ambiguous_symbol_reaches_progress_as_undetermined(monkeypatch):
    """R5 follow-up (ChatGPT review, PROGRESS.md §11): only ONE fill in a
    same-timestamp buy/sell pair has an id prefix — still undetermined, not
    a confirmed match or mismatch, even though the numbers happen to agree."""
    from types import SimpleNamespace

    import agentic_trading.run as run_mod

    fills = [
        {"symbol": "X", "side": "buy", "qty": 1.0, "price": 100.0,
         "transaction_time": "2026-08-27T15:00:00Z"},
        {"symbol": "X", "side": "buy", "qty": 1.0, "price": 105.0,
         "transaction_time": "2026-08-27T16:00:00Z"},
        # Same timestamp; only the sell has an id.
        {"symbol": "X", "side": "sell", "qty": 1.0, "price": 110.0,
         "transaction_time": "2026-08-27T17:00:00Z",
         "id": "20260827170000000::cccccccc-0000-0000-0000-000000000003"},
        {"symbol": "X", "side": "buy", "qty": 1.0, "price": 108.0,
         "transaction_time": "2026-08-27T17:00:00Z"},
    ]
    monkeypatch.setattr(run_mod, "BookBrokerReader", _FakeReader(fills=fills, fills_reason=None))

    run_mod._stamp_cycle_progress(
        asof=None, fast_mode=True, cycle_no=10,
        account=SimpleNamespace(equity=10000.0, cash=8000.0),
        positions={"X": SimpleNamespace(qty=2.0)}, orders_this_cycle=0, status="ok",
    )
    card = read_progress()
    kinds = [u["kind"] for u in card["unresolved"]]
    assert "position_undetermined" in kinds
    assert "position_mismatch" not in kinds


@pytest.mark.parametrize("status,skipped", [
    ("no_trade", "yielded to deep cycle"),   # main(): fast scan steps aside for a deep cycle
    ("failed", "cycle lock not acquired"),   # main(): could not acquire the cycle lock
    ("failed", "cycle raised"),              # main(): run_cycle() raised before reading positions
])
def test_stamp_cycle_progress_positions_none_paths_never_false_alarm(
    monkeypatch, status, skipped,
):
    """The three real call sites in main() that pass positions=None — same
    argument shape as each, including dry_run=False (args.dry_run can be
    False in all three, so the dry-run guard alone does not protect them;
    this is what R3 flagged as the actual gap)."""
    import agentic_trading.run as run_mod

    _CountingReader.calls = 0
    monkeypatch.setattr(run_mod, "BookBrokerReader", _CountingReader)

    run_mod._stamp_cycle_progress(
        asof=None, fast_mode=True, cycle_no=None, account=None, positions=None,
        status=status, skipped=skipped, dry_run=False,
    )
    assert _CountingReader.calls == 0
    card = read_progress()
    kinds = [u["kind"] for u in card["unresolved"]]
    assert "positions_unavailable" in kinds
    assert "position_mismatch" not in kinds


# --- stop coverage unknown (review R2) ------------------------------------------

def test_stamp_cycle_progress_unknown_stop_coverage_reaches_unresolved(monkeypatch):
    """Open orders unreadable → the card must not read as a clean cycle: an
    explicit stop_coverage_unknown entry, status downgraded to incomplete,
    stops_covered null with the held count kept."""
    from types import SimpleNamespace

    import agentic_trading.run as run_mod

    monkeypatch.setattr(run_mod, "BookBrokerReader", _FakeReader(fills=None, fills_reason="no creds"))
    run_mod._stamp_cycle_progress(
        asof=None, fast_mode=False, cycle_no=3,
        account=SimpleNamespace(equity=8263.29, cash=3634.75),
        positions={"MSFT": object(), "NVDA": object()}, orders_this_cycle=0, status="ok",
        stop_coverage=None, stop_coverage_unknown=run_mod.STOP_COVERAGE_UNKNOWN_REASON,
    )
    card = read_progress()
    unknown = [u for u in card["unresolved"] if u["kind"] == "stop_coverage_unknown"]
    assert len(unknown) == 1
    assert "not verified" in unknown[0]["detail"] and "2 positions held" in unknown[0]["detail"]
    assert card["round"]["status"] == "incomplete"
    assert card["risk"]["stops_covered"] is None and card["risk"]["stops_total"] == 2
    assert "naked_stops" not in [u["kind"] for u in card["unresolved"]]


def test_stamp_cycle_progress_skipped_cycle_is_not_an_unknown_coverage(monkeypatch):
    """A market-closed fast exit never reconciles: neither stop_coverage nor
    stop_coverage_unknown is set, and that must not be reported as a failed check."""
    from types import SimpleNamespace

    import agentic_trading.run as run_mod

    monkeypatch.setattr(run_mod, "BookBrokerReader", _FakeReader(fills=None, fills_reason="no creds"))
    run_mod._stamp_cycle_progress(
        asof=None, fast_mode=True, cycle_no=None,
        account=SimpleNamespace(equity=8263.29, cash=3634.75),
        positions={"MSFT": object()}, status="no_trade", skipped="market closed",
    )
    card = read_progress()
    assert "stop_coverage_unknown" not in [u["kind"] for u in card["unresolved"]]
    assert card["round"]["status"] == "no_trade"
    assert card["risk"]["stops_covered"] is None and card["risk"]["stops_total"] is None
