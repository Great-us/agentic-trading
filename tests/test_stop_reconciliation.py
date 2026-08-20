"""The protective-stop reconciliation is what keeps a position covered between
cycles, so its cancel/replace behaviour is worth pinning down: replacing too
eagerly leaves the position briefly naked, and not replacing at all means the
trailing stop never ratchets."""
import logging

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

    def submit_stop_sell(self, symbol, qty, stop_price):
        if self.reject_stops:
            return None
        self.placed.append((symbol, qty, stop_price))
        return OrderResult(symbol=symbol, side="sell", qty=qty, status="accepted", order_id="new")


def position(symbol="MSFT", qty=3.74, entry=481.0, price=481.0):
    return Position(symbol=symbol, qty=qty, avg_entry_price=entry, current_price=price,
                    market_value=qty * price)


def stop_order(symbol="MSFT", stop_price=452.14, order_id="existing"):
    return OpenOrder(order_id=order_id, symbol=symbol, side="sell", order_type="stop",
                     qty=3.74, stop_price=stop_price)


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
    broker = FakeBroker([stop_order(stop_price=452.14)])
    _reconcile_protective_stops(broker, {"MSFT": position()}, {"MSFT": 600.0}, {"MSFT": 12.0}, RISK, LOG)
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
