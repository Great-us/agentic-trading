"""The protective-stop reconciliation is what keeps a position covered between
cycles, so its cancel/replace behaviour is worth pinning down: replacing too
eagerly leaves the position briefly naked, and not replacing at all means the
trailing stop never ratchets."""
import logging

import pytest

from agentic_trading.config import RiskConfig
from agentic_trading.execution.broker import OpenOrder, OrderResult, Position
from agentic_trading.run import _release_shares_for_sale, _reconcile_protective_stops

LOG = logging.getLogger("test")

RISK = RiskConfig(
    risk_per_trade_pct=0.015, max_position_pct=0.18, min_position_pct=0.04,
    max_total_exposure_pct=0.95, max_open_positions=6,
    max_new_orders_per_cycle=5, min_quant_score_to_consider=0.15,
    atr_stop_multiple=2.5, min_stop_pct=0.06, max_stop_pct=0.20,
    trailing_stop_pct=0.12, risk_off_size_multiplier=0.5, risk_off_score_penalty=0.15,
    escalation_cooldown_minutes=60, escalation_cooldown_score_delta=0.15,
    take_profit_pct=None,
)


class FakeBroker:
    def __init__(self, open_orders=None, reject_stops=False):
        self.open_orders = list(open_orders or [])
        self.reject_stops = reject_stops
        self.placed: list[tuple[str, float, float]] = []
        self.cancelled: list[str] = []

    def get_open_orders(self):
        return list(self.open_orders)

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self.open_orders = [o for o in self.open_orders if o.order_id != order_id]
        return True

    def submit_stop_sell(self, symbol, qty, stop_price, *, client_order_id=None):
        if self.reject_stops:
            return None
        self.placed.append((symbol, qty, stop_price))
        return OrderResult(symbol=symbol, side="sell", qty=qty, status="accepted", order_id="new")


def position(symbol="MSFT", qty=3.74, entry=481.0, price=481.0):
    return Position(symbol=symbol, qty=qty, avg_entry_price=entry, current_price=price,
                    market_value=qty * price)


def stop_order(symbol="MSFT", stop_price=452.14, order_id="existing", qty=3.74, status="accepted"):
    return OpenOrder(order_id=order_id, symbol=symbol, side="sell", order_type="stop",
                     qty=qty, stop_price=stop_price, status=status)


def test_places_a_stop_for_an_unprotected_position():
    broker = FakeBroker()
    _reconcile_protective_stops(broker, {"MSFT": position()}, {}, {"MSFT": 12.0}, RISK, LOG)
    assert len(broker.placed) == 1
    symbol, qty, stop_price = broker.placed[0]
    assert symbol == "MSFT" and qty == 3.74
    assert stop_price < 481.0


def test_leaves_an_adequate_stop_alone():
    # Already resting at the level reconciliation would choose: replacing it
    # would briefly uncover the position for no benefit.
    existing = stop_order(stop_price=452.14)
    broker = FakeBroker([existing])
    _reconcile_protective_stops(broker, {"MSFT": position()}, {}, {"MSFT": 12.0}, RISK, LOG)
    assert broker.placed == []
    assert broker.cancelled == []


def test_ratchets_the_stop_up_after_a_run():
    # Peak at 600 pulls the trailing level to 528, well above the resting 452.
    # The market (595) sits just under the peak — a real ratchet, not a crash
    # below the trail, which the client-side exit would have handled already.
    broker = FakeBroker([stop_order(stop_price=452.14)])
    _reconcile_protective_stops(
        broker, {"MSFT": position(price=595.0)}, {"MSFT": 600.0}, {"MSFT": 12.0}, RISK, LOG,
    )
    assert broker.cancelled == ["existing"]
    assert len(broker.placed) == 1
    assert broker.placed[0][2] == round(600.0 * 0.88, 2)


def test_never_lowers_an_existing_stop():
    # A stop resting far above what the formula wants (e.g. left from a higher
    # peak) must not be loosened.
    broker = FakeBroker([stop_order(stop_price=470.0)])
    _reconcile_protective_stops(broker, {"MSFT": position()}, {}, {"MSFT": 12.0}, RISK, LOG)
    assert broker.placed == []
    assert broker.cancelled == []


def test_skips_symbols_without_an_atr():
    broker = FakeBroker()
    _reconcile_protective_stops(broker, {"MSFT": position()}, {}, {}, RISK, LOG)
    assert broker.placed == []


def test_survives_a_rejected_stop():
    broker = FakeBroker(reject_stops=True)
    _reconcile_protective_stops(broker, {"MSFT": position()}, {}, {"MSFT": 12.0}, RISK, LOG)
    assert broker.placed == []  # rejection is logged, not raised


def test_live_retries_a_stop_after_cancel(monkeypatch):
    monkeypatch.setattr("agentic_trading.run.time.sleep", lambda _s: None)

    class Flaky(FakeBroker):
        def __init__(self):
            super().__init__([stop_order(stop_price=452.14)])
            self.attempts = 0

        def submit_stop_sell(self, symbol, qty, stop_price, *, client_order_id=None):
            self.attempts += 1
            if self.attempts == 1:
                return None
            return super().submit_stop_sell(symbol, qty, stop_price, client_order_id=client_order_id)

    broker = Flaky()
    _reconcile_protective_stops(
        broker, {"MSFT": position(price=595.0)}, {"MSFT": 600.0}, {"MSFT": 12.0}, RISK, LOG, live=True,
    )
    assert broker.attempts == 2
    assert len(broker.placed) == 1


def test_ignores_buy_orders_when_looking_for_protection():
    # An open BUY on the symbol is not protection; a stop must still be placed.
    pending_buy = OpenOrder(order_id="buy1", symbol="MSFT", side="buy", order_type="market",
                            qty=0.0, stop_price=None)
    broker = FakeBroker([pending_buy])
    _reconcile_protective_stops(broker, {"MSFT": position()}, {}, {"MSFT": 12.0}, RISK, LOG)
    assert len(broker.placed) == 1
    assert broker.cancelled == []


def test_reconciliation_survives_a_broker_read_failure():
    class Broken(FakeBroker):
        def get_open_orders(self):
            raise RuntimeError("API down")

    broker = Broken()
    _reconcile_protective_stops(broker, {"MSFT": position()}, {}, {"MSFT": 12.0}, RISK, LOG)
    assert broker.placed == []  # skipped this cycle rather than crashing


# --- coverage = live status AND full qty (P0-B-2) ----------------------------
#
# VEEV, 2026-09-16 → 09-17: the protective stop was `accepted` at 19:19 ET,
# logged as "placed", flipped to `rejected` in the 04:00 ET queue run, and the
# heartbeat kept saying 5/5 covered because coverage was "a stop exists for
# this symbol". None of the cases below may report green.

def _coverage(broker, positions, peaks=None, atrs=None, **kw):
    atrs = atrs if atrs is not None else {s: 12.0 for s in positions}
    return _reconcile_protective_stops(broker, positions, peaks or {}, atrs, RISK, LOG, **kw)


def test_full_size_live_stop_at_the_right_level_counts_as_covered():
    broker = FakeBroker([stop_order(stop_price=452.14, qty=3.74, status="accepted")])
    assert _coverage(broker, {"MSFT": position(qty=3.74)}) == (1, 1)
    assert broker.placed == [] and broker.cancelled == []


def test_same_price_but_short_qty_is_replaced_for_the_full_position():
    # A stop sized for the original 2-share lot after a 1.74-share top-up:
    # the level is fine, the size is not — cancel and re-place for all 3.74.
    broker = FakeBroker([stop_order(stop_price=452.14, qty=2.0)])
    assert _coverage(broker, {"MSFT": position(qty=3.74)}) == (1, 1)
    assert broker.cancelled == ["existing"]
    assert len(broker.placed) == 1
    _, qty, price = broker.placed[0]
    assert qty == 3.74 and price == 452.14


def test_short_qty_replacement_keeps_a_higher_earned_level():
    # Replacing for size must not hand back a ratchet: the old (short) stop
    # sat at 470, the formula wants 452.14 — the new full-size stop goes at 470.
    broker = FakeBroker([stop_order(stop_price=470.0, qty=2.0)])
    _coverage(broker, {"MSFT": position(qty=3.74, price=481.0)})
    assert broker.placed[0][2] == 470.0


def test_short_qty_and_cancel_failure_is_not_covered():
    class StuckCancel(FakeBroker):
        def cancel_order(self, order_id):
            return False

    broker = StuckCancel([stop_order(stop_price=452.14, qty=2.0)])
    assert _coverage(broker, {"MSFT": position(qty=3.74)}) == (0, 1)
    assert broker.placed == []


def test_full_size_and_cancel_failure_is_still_covered():
    # Only the level was stale; the whole position is still behind the old stop.
    class StuckCancel(FakeBroker):
        def cancel_order(self, order_id):
            return False

    broker = StuckCancel([stop_order(stop_price=452.14)])
    assert _coverage(broker, {"MSFT": position(price=595.0)}, peaks={"MSFT": 600.0}) == (1, 1)


def test_rejected_stop_reported_by_the_broker_is_not_covered_and_is_replaced():
    broker = FakeBroker([stop_order(status="rejected")])
    assert _coverage(broker, {"MSFT": position()}) == (1, 1)  # replaced → covered now
    assert broker.cancelled == ["existing"]
    assert len(broker.placed) == 1


def test_expired_stop_that_cannot_be_replaced_is_not_covered():
    # The DAY stop expired at the close and the broker now refuses the re-place:
    # this is exactly the VEEV shape and must come out as 0/1, not 1/1.
    broker = FakeBroker([stop_order(status="expired")], reject_stops=True)
    assert _coverage(broker, {"MSFT": position()}) == (0, 1)


def test_pending_cancel_stop_is_not_protection():
    broker = FakeBroker([stop_order(status="pending_cancel")], reject_stops=True)
    assert _coverage(broker, {"MSFT": position()}) == (0, 1)


def test_unknown_status_from_an_old_adapter_still_counts_when_whole():
    # Adapters/fakes that predate the status field return None; Alpaca's open
    # orders query only returns live orders, so None + full qty is coverage.
    broker = FakeBroker([stop_order(status=None)])
    assert _coverage(broker, {"MSFT": position()}) == (1, 1)
    assert broker.placed == []


def test_dead_stop_above_the_market_branch_is_not_covered():
    # Poisoned peak → computed stop above market → nothing can be placed. The
    # resting order used to count regardless; a rejected one covers nothing.
    broker = FakeBroker([stop_order(stop_price=452.14, status="rejected")])
    result = _coverage(broker, {"MSFT": position(price=400.0)}, peaks={"MSFT": 600.0})
    assert result == (0, 1)
    assert broker.placed == []


def test_dead_stop_without_an_atr_is_not_covered():
    broker = FakeBroker([stop_order(qty=1.0)])
    assert _coverage(broker, {"MSFT": position()}, atrs={}) == (0, 1)
    assert broker.placed == []


def test_unreadable_open_orders_report_unknown_not_green():
    class Broken(FakeBroker):
        def get_open_orders(self):
            raise RuntimeError("API down")

    assert _coverage(Broken(), {"MSFT": position()}) is None


def test_broker_that_returns_none_after_a_dead_status_leaves_the_position_uncovered():
    # The AlpacaBroker adapter now returns None when the post-submit re-read
    # says rejected/expired; the reconciliation must treat that like any other
    # failed submit (no retry loop in non-live mode, 0/1).
    broker = FakeBroker(reject_stops=True)
    assert _coverage(broker, {"MSFT": position()}) == (0, 1)


# --- submit result must pass the same resting test (review R1) --------------
#
# AlpacaBroker.submit_stop_sell re-reads the order and returns None only for
# DEAD statuses. Everything else comes back as an OrderResult carrying the
# verified status — and the reconciliation used to count any non-None result
# as covered, bypassing stop_is_resting(). pending_cancel / pending_replace /
# an unknown value are not protection; `filled` means the shares are being
# sold and a second sell order would sell what we no longer hold.

class StatusBroker(FakeBroker):
    """submit_stop_sell answers with a fixed verified status."""

    def __init__(self, status, open_orders=None, positions_after=None):
        super().__init__(open_orders)
        self.status = status
        self.positions_after = positions_after
        self.position_reads = 0

    def submit_stop_sell(self, symbol, qty, stop_price, *, client_order_id=None):
        self.placed.append((symbol, qty, stop_price))
        return OrderResult(symbol=symbol, side="sell", qty=qty, status=self.status, order_id=f"o{len(self.placed)}")

    def get_positions(self):
        self.position_reads += 1
        if self.positions_after is None:
            raise RuntimeError("positions unreadable")
        return dict(self.positions_after)


@pytest.mark.parametrize("status", ["pending_cancel", "pending_replace", "pending_review", "weird_new_value"])
def test_undetermined_submit_status_is_not_covered_and_not_doubled(status, monkeypatch):
    monkeypatch.setattr("agentic_trading.run.time.sleep", lambda _s: None)
    broker = StatusBroker(status)
    assert _coverage(broker, {"MSFT": position()}, live=True) == (0, 1)
    assert len(broker.placed) == 1, "no retry and no second order for an unsettled status"


def test_filled_on_submit_is_not_covered_and_no_second_sell_is_placed(monkeypatch):
    # The stop fired instantly: the position is gone at the broker. Coverage
    # excludes the sold position (it is not an uncovered holding), exactly one
    # sell order was ever sent, and the book was re-read rather than assumed.
    monkeypatch.setattr("agentic_trading.run.time.sleep", lambda _s: None)
    broker = StatusBroker("filled", positions_after={})
    assert _coverage(broker, {"MSFT": position()}, live=True) == (0, 0)
    assert len(broker.placed) == 1
    assert broker.position_reads == 1


def test_filled_on_submit_with_other_positions_keeps_their_coverage():
    nvda_stop = stop_order(symbol="NVDA", order_id="nvda", qty=8.1, stop_price=190.0)
    broker = StatusBroker("filled", open_orders=[nvda_stop],
                          positions_after={"NVDA": position("NVDA", qty=8.1, entry=200.0, price=200.0)})
    covered, total = _coverage(broker, {"MSFT": position(), "NVDA": position("NVDA", qty=8.1, entry=200.0, price=200.0)})
    assert (covered, total) == (1, 1)  # NVDA still held and covered; MSFT sold, not counted either way
    assert [p[0] for p in broker.placed] == ["MSFT"]


def test_filled_on_submit_with_unreadable_positions_stays_conservative():
    broker = StatusBroker("filled", positions_after=None)
    refreshed = {}
    assert _coverage(broker, {"MSFT": position()}, refreshed=refreshed) == (0, 1)  # alert until the next cycle verifies
    assert len(broker.placed) == 1
    assert refreshed == {"attempted": True, "ok": False, "positions": None, "filled": {"MSFT"}}


def test_filled_on_submit_hands_the_refreshed_book_back_to_the_caller():
    # Review round 2, R1: the caller (run_cycle) needs the whole refreshed
    # book, including a symbol that appeared during the loop — not old ∩ new.
    nvda = position("NVDA", qty=8.1, entry=200.0, price=200.0)
    broker = StatusBroker("filled", positions_after={"NVDA": nvda})
    refreshed = {}
    assert _coverage(broker, {"MSFT": position()}, refreshed=refreshed) == (0, 1)  # NVDA held, uncovered
    assert refreshed["attempted"] and refreshed["ok"]
    assert set(refreshed["positions"]) == {"NVDA"}
    assert refreshed["filled"] == {"MSFT"}  # the caller freezes this symbol for the rest of the cycle


def test_no_fill_leaves_the_refreshed_holder_untouched():
    broker = FakeBroker()
    refreshed = {}
    assert _coverage(broker, {"MSFT": position()}, refreshed=refreshed) == (1, 1)
    assert refreshed == {}


def test_resting_submit_statuses_count_as_covered():
    for status in ("new", "accepted", "held", "partially_filled", "pending_new"):
        broker = StatusBroker(status)
        assert _coverage(broker, {"MSFT": position()}) == (1, 1), status


def test_restore_after_failed_sell_does_not_double_on_filled(caplog):
    from agentic_trading.run import _restore_protective_stop

    broker = StatusBroker("filled", positions_after={})
    with caplog.at_level(logging.WARNING, logger="test"):
        _restore_protective_stop(broker, LOG, symbol="MSFT", qty=3.74, entry_price=481.0,
                                 peak=481.0, atr14=12.0, risk=RISK, cycle_timestamp="2026-09-18T00:00:00")
    assert len(broker.placed) == 1
    assert "not treating the position as covered" in caplog.text
    assert "restored a protective stop" not in caplog.text


# --- releasing shares before a sale ----------------------------------------

def test_release_cancels_resting_sell_orders():
    broker = FakeBroker([stop_order()])
    _release_shares_for_sale(broker, "MSFT", LOG)
    assert broker.cancelled == ["existing"]


def test_release_leaves_other_symbols_untouched():
    broker = FakeBroker([stop_order(symbol="NVDA", order_id="nvda-stop")])
    _release_shares_for_sale(broker, "MSFT", LOG)
    assert broker.cancelled == []


def test_release_survives_a_broker_failure():
    class Broken(FakeBroker):
        def get_open_orders(self):
            raise RuntimeError("API down")

    _release_shares_for_sale(Broken(), "MSFT", LOG)  # must not raise
