"""A-3 (contract 2): OrderObservation is the single read-only order re-query
entry point, and submit_stop_sell must use it without changing its P0-B-2
verified three-way semantics (DEAD -> None / resting -> "placed" / anything
else -> OrderResult with the verified status).

Buy-side observations are strictly separate from OrderResult: seeing
`rejected` later never rewrites a non-None submit result, and `accepted`
(a stop-coverage semantics) never means a buy filled.
"""
from __future__ import annotations

import logging

import pytest

from agentic_trading.execution.broker import (
    DEAD_ORDER_STATUSES,
    DryRunBroker,
    OrderObservation,
    OrderResult,
    stop_is_resting,
)

LOG = logging.getLogger("test")


# --- fake Alpaca plumbing (same shape as test_p0_hardening) ------------------

class _FakeOrder:
    def __init__(self, status, order_id="ord-1", qty="3.74", filled_qty=None,
                 filled_avg_price=None, filled_at=None):
        self.id = order_id
        self.status = status
        self.qty = qty
        self.symbol = "MSFT"
        self.side = "sell"
        self.type = "stop"
        self.stop_price = "452.14"
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.filled_at = filled_at


class _EnumLike:
    """str() would be 'OrderStatus.Accepted'; the bare value is the truth."""

    def __init__(self, value):
        self.value = value


class ScriptedClient:
    """get_order_by_id replays a scripted sequence of order states."""

    def __init__(self, *states, statuses=None):
        # states: list of dicts per call, or statuses=[...] shorthand
        self.script = list(statuses) if statuses is not None else list(states)
        self.rereads = 0

    def submit_order(self, request):
        return _FakeOrder("accepted")

    def get_order_by_id(self, order_id):
        self.rereads += 1
        entry = self.script[min(self.rereads - 1, len(self.script) - 1)]
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, dict):
            return _FakeOrder(entry.get("status"), order_id=order_id,
                              filled_qty=entry.get("filled_qty"),
                              filled_avg_price=entry.get("filled_avg_price"),
                              filled_at=entry.get("filled_at"))
        return _FakeOrder(entry, order_id=order_id)


def _alpaca_with(client):
    from agentic_trading.execution.broker import AlpacaBroker

    broker = AlpacaBroker.__new__(AlpacaBroker)  # no real TradingClient
    broker._client = client
    broker._api_key = broker._secret_key = "x"
    broker._data = None
    return broker


# --- field shape -------------------------------------------------------------

def test_observation_fields_and_defaults():
    obs = OrderObservation(
        order_id="o1", status="accepted", observed_at="2026-09-20T12:00:00+00:00",
        filled_qty=None, filled_avg_price=None, filled_at=None, error=None,
    )
    assert obs.order_id == "o1"
    assert obs.status == "accepted"
    assert not obs.is_terminal()
    assert obs.error is None


def test_is_terminal_covers_dead_statuses_and_filled():
    for status in DEAD_ORDER_STATUSES | {"filled"}:
        obs = OrderObservation("o", status, "t", None, None, None, None)
        assert obs.is_terminal(), status
    for status in ("accepted", "new", "held", "partially_filled", "pending_new", None):
        obs = OrderObservation("o", status, "t", None, None, None, None)
        assert not obs.is_terminal(), status


# --- AlpacaBroker.observe_order ----------------------------------------------

def test_accepted_without_fill_reports_no_fill_evidence():
    broker = _alpaca_with(ScriptedClient(statuses=[{"status": "accepted"}]))
    obs = broker.observe_order("ord-1")
    assert obs is not None and obs.status == "accepted"
    assert not obs.is_terminal()
    assert obs.filled_qty in (None, 0.0)
    assert obs.filled_avg_price is None and obs.filled_at is None
    assert obs.error is None
    assert obs.order_id == "ord-1"
    assert "T" in obs.observed_at  # ISO UTC timestamp present


def test_accepted_then_filled_same_order_one_identity():
    broker = _alpaca_with(ScriptedClient(statuses=[
        {"status": "accepted"},
        {"status": "filled", "filled_qty": "3.74", "filled_avg_price": "452.14",
         "filled_at": "2026-09-20T14:31:05Z"},
    ]))
    first = broker.observe_order("ord-1")
    second = broker.observe_order("ord-1")
    assert first.order_id == second.order_id == "ord-1"
    assert not first.is_terminal() and second.is_terminal()
    assert second.status == "filled"
    assert second.filled_qty == 3.74 and second.filled_avg_price == 452.14
    assert second.filled_at == "2026-09-20T14:31:05Z"


def test_partial_then_complete_still_one_order():
    broker = _alpaca_with(ScriptedClient(statuses=[
        {"status": "accepted"},
        {"status": "partially_filled", "filled_qty": "2.0", "filled_avg_price": "451.0"},
        {"status": "partially_filled", "filled_qty": "3.0", "filled_avg_price": "451.5"},
        {"status": "filled", "filled_qty": "3.74", "filled_avg_price": "451.8"},
    ]))
    seen = {broker.observe_order("ord-1").order_id for _ in range(4)}
    obs = broker.observe_order("ord-1")
    assert seen == {"ord-1"}  # repeated observation is not a second order
    assert obs.status == "filled" and obs.is_terminal()
    assert obs.filled_qty == 3.74


def test_one_fill_activity_does_not_prove_the_whole_order_done():
    broker = _alpaca_with(ScriptedClient(statuses=[
        {"status": "partially_filled", "filled_qty": "2.0", "filled_avg_price": "451.0"},
    ]))
    obs = broker.observe_order("ord-1")
    assert obs.filled_qty == 2.0  # positive fill evidence exists...
    assert not obs.is_terminal()  # ...but the order is not complete


@pytest.mark.parametrize("dead", ["rejected", "canceled", "expired"])
def test_dead_statuses_are_terminal(dead):
    broker = _alpaca_with(ScriptedClient(statuses=[{"status": dead}]))
    obs = broker.observe_order("ord-1")
    assert obs.status == dead and obs.is_terminal()
    assert obs.filled_qty is None  # no fabricated fill on a dead order


def test_unknown_status_is_reported_not_guessesd():
    broker = _alpaca_with(ScriptedClient(statuses=[{"status": "calculus"}]))
    obs = broker.observe_order("ord-1")
    assert obs.status == "calculus"
    assert not obs.is_terminal()  # unknown is unknown, not dead


def test_status_is_normalized_to_lowercase_from_enum_like():
    broker = _alpaca_with(ScriptedClient(statuses=[{"status": _EnumLike("Accepted")}]))
    obs = broker.observe_order("ord-1")
    assert obs.status == "accepted"


def test_requery_failure_gives_status_none_and_error():
    broker = _alpaca_with(ScriptedClient(statuses=[RuntimeError("504 from broker")]))
    obs = broker.observe_order("ord-1")
    assert obs.status is None
    assert obs.error and "504" in obs.error
    assert not obs.is_terminal()  # failure to verify is not evidence of death


# --- degraded observation: DryRun / legacy fakes ------------------------------

def test_dry_run_broker_returns_none_and_does_not_fabricate_a_fill():
    broker = DryRunBroker()
    assert broker.observe_order("anything") is None
    # and the dry-run stop result is untouched by observation:
    result = broker.submit_stop_sell("MSFT", 3.74, 452.14)
    assert result is not None and result.status == "dry_run"
    assert result.order_id is None


def test_legacy_fake_without_observe_order_degrades_via_getattr():
    class OldFake:
        def submit_notional_buy(self, symbol, notional, *, atr14=None, client_order_id=None):
            return OrderResult(symbol=symbol, side="buy", qty=0.0,
                               status="accepted", order_id="buy-1")

    broker = OldFake()
    observe = getattr(broker, "observe_order", None)
    assert observe is None  # the documented caller pattern: unverified, no crash
    result = broker.submit_notional_buy("MSFT", 1800.0)
    assert result is not None and result.status == "accepted"


# --- submit_stop_sell now goes through observe_order --------------------------

def test_stop_submit_uses_the_single_requery_entry():
    client = ScriptedClient(statuses=[{"status": "held"}])
    broker = _alpaca_with(client)
    result = broker.submit_stop_sell("MSFT", 3.74, 452.14)
    assert client.rereads == 1  # exactly one re-read, via observe_order
    assert result is not None and result.status == "held"
    assert result.order_id == "ord-1"


def test_stop_submit_dead_returns_none_without_placing(caplog):
    client = ScriptedClient(statuses=[{"status": "rejected"}])
    broker = _alpaca_with(client)
    with caplog.at_level("WARNING", logger="agentic_trading.execution.broker"):
        result = broker.submit_stop_sell("MSFT", 3.74, 452.14)
    assert result is None
    assert "NOT accepted" in caplog.text and "rejected" in caplog.text


@pytest.mark.parametrize("later", ["pending_cancel", "pending_review", "some_future_status"])
def test_stop_submit_undetermined_returns_result_with_status(later):
    broker = _alpaca_with(ScriptedClient(statuses=[{"status": later}]))
    result = broker.submit_stop_sell("MSFT", 3.74, 452.14)
    assert result is not None and result.status == later


def test_stop_submit_filled_returns_filled():
    broker = _alpaca_with(ScriptedClient(statuses=[{"status": "filled"}]))
    result = broker.submit_stop_sell("MSFT", 3.74, 452.14)
    assert result is not None and result.status == "filled"


def test_stop_submit_requery_failure_falls_back_to_submit_response(caplog):
    client = ScriptedClient(statuses=[RuntimeError("504 from broker")])
    broker = _alpaca_with(client)
    with caplog.at_level("WARNING", logger="agentic_trading.execution.broker"):
        result = broker.submit_stop_sell("MSFT", 3.74, 452.14)
    assert result is not None and result.status == "accepted"
    assert "Could not re-read" in caplog.text


# --- buy-side separation -------------------------------------------------------

def test_observing_rejected_does_not_rewrite_a_non_empty_buy_result():
    broker = _alpaca_with(ScriptedClient(statuses=[{"status": "rejected"}]))
    submitted = OrderResult(symbol="MSFT", side="buy", qty=3.0,
                            status="accepted", order_id="buy-1")
    obs = broker.observe_order("buy-1")
    assert obs.status == "rejected" and obs.is_terminal()
    # the submit fact stands on its own: no None-ing, no retry, no budget
    # semantics live here — the observation is read-only evidence.
    assert submitted is not None and submitted.status == "accepted"
    assert submitted.order_id == "buy-1"


def test_accepted_is_not_a_buy_fill_and_resting_is_stop_coverage_only():
    broker = _alpaca_with(ScriptedClient(statuses=[{"status": "accepted"}]))
    obs = broker.observe_order("buy-1")
    # stop_is_resting is a protective-stop coverage notion; it must never be
    # used as buy-fill evidence.
    assert stop_is_resting(obs.status)
    assert obs.filled_qty in (None, 0.0)
    assert not obs.is_terminal()


def test_submit_notional_buy_response_is_unchanged_by_observation():
    client = ScriptedClient(statuses=[{"status": "rejected"}])
    broker = _alpaca_with(client)
    result = broker.submit_notional_buy("MSFT", 1800.0, atr14=12.0, client_order_id="c-1")
    assert result is not None
    assert result.status == "accepted" and result.order_id == "ord-1"
    assert result.qty == 3.74  # submit response qty, untouched by any re-query
    assert client.rereads == 0  # observing is opt-in, never auto-appended


# --- ITERATION 2 / R1: optional fill fields must never break a valid status --

class _BadQtyOrder:
    status = "accepted"
    filled_qty = "bad"
    filled_avg_price = "also-bad"
    filled_at = None


class _StubClient:
    def get_order_by_id(self, order_id):
        return _BadQtyOrder()


def test_observe_order_garbage_optional_fields_keep_valid_status():
    from agentic_trading.execution.broker import AlpacaBroker
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._client = _StubClient()
    obs = broker.observe_order("o-1")  # must not raise (review R1 counter-example)
    assert obs.status == "accepted"
    assert obs.filled_qty is None and obs.filled_avg_price is None
    assert obs.error is None


class _RaisingClient:
    def get_order_by_id(self, order_id):
        raise RuntimeError("requery exploded")


def test_submit_stop_sell_survives_raising_requery():
    """R1: the stop path keeps the P0 fallback when the shared re-query entry
    itself raises — the submit response still decides, nothing propagates."""
    from agentic_trading.execution.broker import AlpacaBroker, OrderResult, stop_is_resting
    from agentic_trading.execution.broker import _enum_str

    class _Order:
        id = "stop-9"
        status = "accepted"

    class _StubClient:
        def submit_order(self, request):
            return _Order()

    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._client = _StubClient()

    def boom(order_id):
        raise RuntimeError("requery exploded")

    broker.observe_order = boom
    result = broker.submit_stop_sell("X", 1.0, 90.0)  # must not raise
    assert result is not None and result.status == "accepted"
    assert stop_is_resting(result.status) or result.status == "accepted"
