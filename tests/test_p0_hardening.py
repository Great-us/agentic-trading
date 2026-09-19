"""Regression tests for the P0 execution-layer hardening:

- cross-process cycle lock
- bad-tick guard on the trailing-stop peak update
- refusing to place a protective stop at/above the market
- off-watchlist holdings keep their peak updates and exit checks
- TradeIntent TTL and preservation when open orders are unreadable
- live entries queue as TradeIntents and execute only inside the entry window
- chase guards keep an intent that would buy the top of the move
- a missing LLM verdict fails closed for new entries
- order ids are unique per placement action; rejected stops retry on a fresh id
"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from agentic_trading.config import RiskConfig, Settings
from agentic_trading.cycle_lock import CycleLock
from agentic_trading.execution.broker import Account, OrderIdMinter, OrderResult, Position
from agentic_trading.signals.macro import MacroRegime
from agentic_trading.journal.logger import (
    TradeIntent, clear_trade_intent, connect, load_trade_intents, save_trade_intent,
)
from agentic_trading.llm.schema import AnalystVerdict
from agentic_trading.run import (
    LLM_FAIL_FAST_STREAK, STOP_REJECTION_RETRY_SECONDS, TRADE_INTENT_TTL, _bar_high,
    _flush_trade_intents, _reconcile_protective_stops, run_cycle,
)

import logging

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


# --- cross-process lock ------------------------------------------------------

def test_cycle_lock_is_exclusive(tmp_path):
    lock_path = tmp_path / "cycle.lock"
    first = CycleLock(lock_path)
    assert first.acquire() is True
    second = CycleLock(lock_path)
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True  # freed by the OS once released...
    second.release()
    third = CycleLock(lock_path)
    assert third.acquire() is True   # ...and re-acquirable after an abnormal exit


# --- bad-tick guard ----------------------------------------------------------

def _df(closes, highs=None):
    n = len(closes)
    highs = highs or [c * 1.01 for c in closes]
    return pd.DataFrame({
        "Open": closes, "High": highs,
        "Low": [c * 0.99 for c in closes], "Close": closes,
        "Volume": [1_000_000] * n,
    })


def test_bar_high_accepts_a_normal_bar():
    df = _df([100.0], highs=[103.0])
    assert _bar_high(df, 100.0) == 103.0


def test_bar_high_rejects_an_implausible_spike():
    # A 10x print against its own close would pin the peak forever (the peak
    # table never lowers) — it must fall back to the close-based price.
    df = _df([100.0], highs=[1000.0])
    assert _bar_high(df, 100.0) == 100.0


def test_bar_high_rejects_a_high_below_its_close():
    df = _df([100.0], highs=[50.0])
    assert _bar_high(df, 100.0) == 100.0


# --- stop reconciliation refuses stops at/above the market -------------------

class StopBroker:
    def __init__(self, open_orders=None):
        self.open_orders = list(open_orders or [])
        self.placed = []

    def get_open_orders(self):
        return list(self.open_orders)

    def cancel_order(self, order_id):
        self.open_orders = [o for o in self.open_orders if o.order_id != order_id]
        return True

    def submit_stop_sell(self, symbol, qty, stop_price, *, client_order_id=None):
        self.placed.append((symbol, qty, stop_price))
        return OrderResult(symbol=symbol, side="sell", qty=qty, status="accepted", order_id="new")


def test_reconcile_refuses_a_stop_at_or_above_the_market():
    # A poisoned peak (or stale price) pushes the computed level over the
    # market; submitting it would just be rejected every cycle.
    pos = Position(symbol="MSFT", qty=3.74, avg_entry_price=481.0,
                   current_price=400.0, market_value=1496.0)
    broker = StopBroker()
    peaks = {"MSFT": 600.0}          # trail level 528 > market 400
    _reconcile_protective_stops(broker, {"MSFT": pos}, peaks, {"MSFT": 12.0}, RISK, LOG)
    assert broker.placed == []


def test_reconcile_still_places_a_normal_stop():
    pos = Position(symbol="MSFT", qty=3.74, avg_entry_price=481.0,
                   current_price=481.0, market_value=1797.9)
    broker = StopBroker()
    _reconcile_protective_stops(broker, {"MSFT": pos}, {}, {"MSFT": 12.0}, RISK, LOG)
    assert len(broker.placed) == 1
    assert broker.placed[0][2] < 481.0


# --- AlpacaBroker: "submitted" is not "resting" (P0-B-2) ---------------------

class _FakeOrder:
    def __init__(self, status, order_id="ord-1", qty="3.74"):
        self.id = order_id
        self.status = status
        self.qty = qty
        self.symbol = "MSFT"
        self.side = "sell"
        self.type = "stop"
        self.stop_price = "452.14"


class _FakeTradingClient:
    """submit_order answers `accepted`; get_order_by_id answers whatever the
    test says the broker decided a moment later."""

    def __init__(self, later_status="accepted", reread_raises=False):
        self.later_status = later_status
        self.reread_raises = reread_raises
        self.rereads = 0

    def submit_order(self, request):
        return _FakeOrder("accepted")

    def get_order_by_id(self, order_id):
        self.rereads += 1
        if self.reread_raises:
            raise RuntimeError("504 from broker")
        return _FakeOrder(self.later_status, order_id=order_id)

    def get_orders(self, request):
        return [_FakeOrder(self.later_status)]


def _alpaca_with(client):
    from agentic_trading.execution.broker import AlpacaBroker

    broker = AlpacaBroker.__new__(AlpacaBroker)  # skip __init__: no real TradingClient
    broker._client = client
    broker._api_key = broker._secret_key = "x"
    broker._data = None
    return broker


@pytest.mark.parametrize("later", ["rejected", "expired", "canceled"])
def test_alpaca_stop_that_dies_right_after_submit_is_reported_as_failed(later, caplog):
    client = _FakeTradingClient(later_status=later)
    broker = _alpaca_with(client)
    with caplog.at_level("WARNING", logger="agentic_trading.execution.broker"):
        result = broker.submit_stop_sell("MSFT", 3.74, 452.14, client_order_id="at-stop-1")
    assert result is None, "a dead order must not be returned as a placed stop"
    assert client.rereads == 1
    assert "NOT accepted" in caplog.text and later in caplog.text
    assert "Protective stop placed" not in caplog.text


@pytest.mark.parametrize("later", ["pending_cancel", "pending_replace", "pending_review", "some_future_status"])
def test_alpaca_stop_in_an_undetermined_state_is_returned_but_flagged(later, caplog):
    # Review R1: not DEAD, so the order stays (no re-place); not resting, so
    # the result carries the verified status and a WARNING — the caller, not
    # None-ness, decides coverage.
    broker = _alpaca_with(_FakeTradingClient(later_status=later))
    with caplog.at_level("WARNING", logger="agentic_trading.execution.broker"):
        result = broker.submit_stop_sell("MSFT", 3.74, 452.14)
    assert result is not None and result.status == later
    assert "undetermined state" in caplog.text and later in caplog.text
    assert "Protective stop placed" not in caplog.text


def test_alpaca_stop_filled_on_submit_is_returned_as_filled_with_a_warning(caplog):
    broker = _alpaca_with(_FakeTradingClient(later_status="filled"))
    with caplog.at_level("WARNING", logger="agentic_trading.execution.broker"):
        result = broker.submit_stop_sell("MSFT", 3.74, 452.14)
    assert result is not None and result.status == "filled"
    assert "FILLED on submit" in caplog.text
    assert "Protective stop placed" not in caplog.text


def test_full_chain_undetermined_and_filled_statuses_never_count_as_covered(monkeypatch):
    # AlpacaBroker adapter + _reconcile_protective_stops end to end: one
    # submit per position, no doubling, and only the resting one is covered.
    monkeypatch.setattr("agentic_trading.run.time.sleep", lambda _s: None)

    class PerSymbolClient(_FakeTradingClient):
        def __init__(self):
            super().__init__()
            self.submits = []
            self.by_symbol = {"AAA": "pending_replace", "BBB": "filled", "CCC": "accepted"}

        def submit_order(self, request):
            self.submits.append(request.symbol)
            return _FakeOrder("accepted", order_id=f"o-{request.symbol}")

        def get_order_by_id(self, order_id):
            return _FakeOrder(self.by_symbol[order_id.split("-")[1]], order_id=order_id)

        def get_orders(self, request):
            return []

        def get_all_positions(self):
            class P:
                def __init__(self, s):
                    self.symbol, self.qty, self.avg_entry_price = s, "1", "100"
                    self.current_price, self.market_value = "100", "100"
            return [P("AAA"), P("CCC")]  # BBB's stop filled: it is gone

    client = PerSymbolClient()
    broker = _alpaca_with(client)
    positions = {s: Position(symbol=s, qty=1.0, avg_entry_price=100.0, current_price=100.0, market_value=100.0)
                 for s in ("AAA", "BBB", "CCC")}
    covered, total = _reconcile_protective_stops(
        broker, positions, {}, {s: 3.0 for s in positions}, RISK, LOG, live=True,
    )
    assert sorted(client.submits) == ["AAA", "BBB", "CCC"], "exactly one sell order per position"
    assert (covered, total) == (1, 2)  # CCC covered; AAA unsettled and uncovered; BBB sold, excluded


def test_alpaca_stop_that_stays_accepted_is_placed_with_its_verified_status():
    broker = _alpaca_with(_FakeTradingClient(later_status="held"))
    result = broker.submit_stop_sell("MSFT", 3.74, 452.14)
    assert result is not None and result.status == "held" and result.order_id == "ord-1"


def test_alpaca_stop_reread_failure_falls_back_to_the_submit_response(caplog):
    broker = _alpaca_with(_FakeTradingClient(reread_raises=True))
    with caplog.at_level("WARNING", logger="agentic_trading.execution.broker"):
        result = broker.submit_stop_sell("MSFT", 3.74, 452.14)
    assert result is not None and result.status == "accepted"
    assert "Could not re-read" in caplog.text


def test_alpaca_open_orders_carry_the_status():
    broker = _alpaca_with(_FakeTradingClient(later_status="accepted"))
    orders = broker.get_open_orders()
    assert orders and orders[0].status == "accepted" and orders[0].qty == 3.74


def test_stop_is_resting_buckets():
    from agentic_trading.execution.broker import stop_is_resting

    for live in ("new", "accepted", "held", "partially_filled", "pending_new", None):
        assert stop_is_resting(live), live
    for dead in ("rejected", "expired", "canceled", "pending_cancel", "filled", "weird"):
        assert not stop_is_resting(dead), dead


# --- TradeIntent TTL / unreadable open orders ---------------------------------

# A fixed Wednesday 10:30 ET — inside the default entry window — so flush
# behaviour is deterministic instead of depending on when the suite runs.
FLUSH_NOW = datetime(2026, 8, 26, 14, 30, 0, tzinfo=timezone.utc)


def _save(conn, symbol, age_hours, signal_price=100.0, not_before=None):
    save_trade_intent(conn, TradeIntent(
        symbol=symbol,
        created_at=(FLUSH_NOW - timedelta(hours=age_hours)).isoformat(),
        signal_price=signal_price, atr14=2.0, quant_score=0.4, combined_score=0.4,
        reasoning="test", not_before=not_before,
    ))


class FlushBroker:
    def __init__(self):
        self.buys = []

    def submit_notional_buy(self, symbol, notional, *, atr14=None, client_order_id=None):
        self.buys.append((symbol, notional))
        return OrderResult(symbol=symbol, side="buy", qty=0.0, status="accepted", order_id="x")


class OpenQuoteBroker(FlushBroker):
    """Reports today's open so the open-relative chase gate can run."""

    def get_today_open(self, symbol):
        return 100.0


def _flush(conn, broker, *, readable=True, pending=frozenset(), now=None):
    return _flush_trade_intents(
        broker=broker, conn=conn, settings=_settings(), account=Account(100_000.0, 100_000.0),
        positions={}, pending_buys=set(pending), peaks={},
        cash_remaining=100_000.0, invested_value=0.0, open_position_count=0,
        orders_this_cycle=0, regime_multiplier=1.0,
        cycle_timestamp=FLUSH_NOW.isoformat(), log=LOG, atrs={}, feed=None, asof=None,
        sizing_risk=RISK, sector_map={}, occupancy={}, orders_readable=readable,
        now_utc=now or FLUSH_NOW,
    )


def _settings():
    return Settings(
        alpaca_api_key=None, alpaca_secret_key=None,
        alpaca_base_url="https://paper-api.alpaca.markets",
        moonshot_api_key=None, analyst_provider="cli", analyst_cli_path=None,
        analyst_cli_timeout=180, analyst_cli_home=None, analyst_cli_model=None,
        analyst_model="kimi-k3", watchlist=["AAA"], core_watchlist=["AAA"],
        context_symbols=[], research_symbols=[], sectors={}, risk=RISK,
    )


@pytest.fixture()
def intent_conn(monkeypatch):
    conn = connect(":memory:")
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    yield conn
    conn.close()


def test_expired_intents_are_discarded(intent_conn):
    _save(intent_conn, "OLD", age_hours=TRADE_INTENT_TTL.total_seconds() / 3600 + 1)
    broker = FlushBroker()
    _flush(intent_conn, broker)
    assert load_trade_intents(intent_conn) == []
    assert broker.buys == []


def test_fresh_intents_are_executed(intent_conn):
    _save(intent_conn, "NEW", age_hours=1)
    broker = FlushBroker()
    _flush(intent_conn, broker)
    assert [s for s, _ in broker.buys] == ["NEW"]
    assert load_trade_intents(intent_conn) == []


def test_unreadable_open_orders_preserve_intents(intent_conn):
    # A transient broker error must not wipe the queue: flushing while
    # pending_buys is untrustworthy could double a genuinely pending buy.
    _save(intent_conn, "NEW", age_hours=1)
    broker = FlushBroker()
    _flush(intent_conn, broker, readable=False)
    assert [i.symbol for i in load_trade_intents(intent_conn)] == ["NEW"]
    assert broker.buys == []
    clear_trade_intent(intent_conn, "NEW")


def test_intent_for_a_symbol_with_pending_buy_is_cleared(intent_conn):
    _save(intent_conn, "NEW", age_hours=1)
    broker = FlushBroker()
    _flush(intent_conn, broker, pending={"NEW"})
    assert load_trade_intents(intent_conn) == []
    assert broker.buys == []


# --- entry window / not_before / chase guards ---------------------------------

def test_flush_outside_the_entry_window_keeps_everything(intent_conn):
    _save(intent_conn, "NEW", age_hours=1)
    broker = FlushBroker()
    # 13:30 UTC = 09:30 ET — before the window opens.
    _flush(intent_conn, broker, now=FLUSH_NOW.replace(hour=13, minute=30))
    assert broker.buys == []
    assert [i.symbol for i in load_trade_intents(intent_conn)] == ["NEW"]
    clear_trade_intent(intent_conn, "NEW")


def test_intent_not_yet_due_stays_queued(intent_conn):
    _save(intent_conn, "LATER", age_hours=1,
          not_before=(FLUSH_NOW + timedelta(hours=1)).isoformat())
    broker = FlushBroker()
    _flush(intent_conn, broker)
    assert broker.buys == []
    assert [i.symbol for i in load_trade_intents(intent_conn)] == ["LATER"]
    clear_trade_intent(intent_conn, "LATER")


def test_due_intent_executes(intent_conn):
    _save(intent_conn, "DUE", age_hours=25,
          not_before=(FLUSH_NOW - timedelta(hours=1)).isoformat())
    broker = FlushBroker()
    _flush(intent_conn, broker)
    assert [s for s, _ in broker.buys] == ["DUE"]
    assert load_trade_intents(intent_conn) == []


def test_intent_chasing_the_signal_price_is_kept_for_later(intent_conn, monkeypatch):
    # Live +1.5% over the signal price vs a 1.0% chase cap: wait, don't chase.
    _save(intent_conn, "HOT", age_hours=1)
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 101.5)
    broker = FlushBroker()
    _flush(intent_conn, broker)
    assert broker.buys == []
    assert [i.symbol for i in load_trade_intents(intent_conn)] == ["HOT"]
    clear_trade_intent(intent_conn, "HOT")


def test_intent_within_the_chase_cap_executes(intent_conn, monkeypatch):
    _save(intent_conn, "OK", age_hours=1)
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.5)  # +0.5%
    broker = FlushBroker()
    _flush(intent_conn, broker)
    assert [s for s, _ in broker.buys] == ["OK"]
    assert load_trade_intents(intent_conn) == []


def test_intent_chasing_today_s_open_is_kept_for_later(intent_conn, monkeypatch):
    # +0.99% over the signal passes the signal gate; +2% over today's open
    # hits the open gate. Either guard alone is enough to hold the buy.
    _save(intent_conn, "HOT", age_hours=1, signal_price=101.0)
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 102.0)
    broker = OpenQuoteBroker()
    _flush(intent_conn, broker)
    assert broker.buys == []
    assert [i.symbol for i in load_trade_intents(intent_conn)] == ["HOT"]
    clear_trade_intent(intent_conn, "HOT")


def test_missing_today_s_open_falls_back_to_the_signal_gate(intent_conn, monkeypatch):
    # No session bar (pre-open feed, dry-run broker): the open-relative gate
    # cannot run, but the signal-relative gate still protects the entry.
    _save(intent_conn, "OK", age_hours=1, signal_price=102.0)
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 102.0)
    broker = FlushBroker()  # no get_today_open
    _flush(intent_conn, broker)
    assert [s for s, _ in broker.buys] == ["OK"]


# --- full-cycle behaviour (quote gate + off-watchlist holdings) ---------------

class HarnessBroker:
    """Enough of the Broker protocol for run_cycle against one watchlist name."""

    def __init__(self, positions=None):
        self.positions = dict(positions or {})
        self.sells = []
        self.buys = []
        self.stops = []
        self.cancels = 0

    def get_account(self):
        return Account(equity=100_000.0, cash=100_000.0)

    def get_positions(self):
        return dict(self.positions)

    def is_market_open(self):
        return True

    def get_open_orders(self):
        return []

    def submit_market_order(self, symbol, qty, side, *, client_order_id=None):
        self.sells.append((symbol, qty, side))
        return OrderResult(symbol=symbol, side=side, qty=qty, status="filled", order_id="m")

    def submit_notional_buy(self, symbol, notional, *, atr14=None, client_order_id=None):
        self.buys.append((symbol, notional))
        return OrderResult(symbol=symbol, side="buy", qty=0.0, status="accepted", order_id="b")

    def submit_stop_sell(self, symbol, qty, stop_price, *, client_order_id=None):
        self.stops.append((symbol, qty, stop_price))
        return OrderResult(symbol=symbol, side="sell", qty=qty, status="accepted", order_id="s")

    def cancel_order(self, order_id):
        self.cancels += 1
        return True


def _upward_df(days=80, start=100.0, daily=0.01):
    closes = [start * (1 + daily) ** i for i in range(days)]
    return _df(closes)


def _downward_df(days=80, start=100.0, end=78.0):
    step = (end / start) ** (1 / (days - 1))
    closes = [start * step ** i for i in range(days)]
    return _df(closes)


class HarnessFeed:
    def __init__(self, frames):
        self.frames = frames

    def price_history(self, symbol, asof=None, period=None):
        return self.frames[symbol]

    def news(self, symbol):
        return []

    def fundamentals(self, symbol):
        return {}


def test_live_buy_queues_an_intent_instead_of_buying(monkeypatch):
    # Live entries never market-buy on the signal: they queue a TradeIntent
    # that the fast scans execute inside the entry window, after the gap and
    # chase revalidation.
    feed = HarnessFeed({"AAA": _upward_df()})
    broker = HarnessBroker()
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=False, fast_mode=False,
                  settings=_settings(), broker=broker, feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
        decision = conn.execute(
            "SELECT action, reasoning, order_status FROM decisions WHERE symbol='AAA' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        intents = conn.execute(
            "SELECT symbol, not_before FROM trade_intents"
        ).fetchall()
    finally:
        conn.close()
    assert broker.buys == [], "a live cycle must not submit an immediate market buy"
    assert decision is not None and decision[0] == "buy"
    assert decision[2] == "intent"
    assert intents and intents[0][0] == "AAA"
    assert intents[0][1], "the intent must carry a not_before execution time"


def test_missing_llm_verdict_fails_closed_for_new_entries(tmp_path, monkeypatch):
    # The analyst CLI was configured but produced nothing (crashed mid-cycle):
    # with require_llm_for_entry on, the BUY is downgraded to WAIT instead of
    # letting the quant score open the position alone.
    analyst_shim = tmp_path / "analyst.cmd"
    analyst_shim.write_text("@echo off\r\n", encoding="utf-8")
    settings = _settings()
    settings.analyst_provider = "cli"
    settings.analyst_cli_path = str(analyst_shim)
    feed = HarnessFeed({"AAA": _upward_df()})
    broker = HarnessBroker()
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    monkeypatch.setattr("agentic_trading.run.analyze", lambda *args, **kwargs: None)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=False, fast_mode=False,
                  settings=settings, broker=broker, feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
        decision = conn.execute(
            "SELECT action, reasoning FROM decisions WHERE symbol='AAA' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert broker.buys == []
    assert decision is not None and decision[0] == "wait"
    assert "fail closed" in decision[1]


def _bullish_verdict(signal, news, fundamentals, settings):
    return AnalystVerdict(
        symbol=signal.symbol, stance="bullish", confidence=0.7,
        rationale="test", risk_flags=[],
    )


def test_skip_llm_cycle_emits_start_and_end(tmp_path, monkeypatch):
    from agentic_trading import live_events as le
    from agentic_trading.live_events import read_tail

    path = tmp_path / "cockpit.jsonl"
    monkeypatch.setattr(le, "LIVE_EVENTS_PATH", path)
    settings = _settings()
    settings.analyst_provider = "cli"
    settings.analyst_cli_path = str(tmp_path / "missing-cli")
    feed = HarnessFeed({"AAA": _upward_df()})
    broker = HarnessBroker()
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=True, skip_llm=True, fast_mode=False,
                  settings=settings, broker=broker, feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
    finally:
        conn.close()
    stages = [row["stage"] for row in read_tail(path)]
    assert "cycle_start" in stages
    assert "cycle_end" in stages
    assert "regime" in stages


def test_consecutive_llm_failures_trip_the_circuit(tmp_path, monkeypatch):
    # Three Nones in a row skip remaining LLM calls this cycle. The fourth
    # name still gets a fail-closed WAIT (require_llm_for_entry), but analyze
    # is not invoked for it — that's what keeps a stdin hang from eating the
    # 30-minute task budget.
    analyst_shim = tmp_path / "analyst.cmd"
    analyst_shim.write_text("@echo off\r\n", encoding="utf-8")
    settings = _settings()
    settings.analyst_provider = "cli"
    settings.analyst_cli_path = str(analyst_shim)
    settings.watchlist = ["AAA", "BBB", "CCC", "DDD"]
    settings.core_watchlist = list(settings.watchlist)
    frames = {s: _upward_df() for s in settings.watchlist}
    feed = HarnessFeed(frames)
    broker = HarnessBroker()
    calls = []

    def boom(*args, **kwargs):
        calls.append(args[0].symbol)
        return None

    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    monkeypatch.setattr("agentic_trading.run.analyze", boom)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=False, fast_mode=False,
                  settings=settings, broker=broker, feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
        actions = conn.execute(
            "SELECT symbol, action FROM decisions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert LLM_FAIL_FAST_STREAK == 3
    assert calls == ["AAA", "BBB", "CCC"], calls
    by_sym = {row[0]: row[1] for row in actions}
    assert by_sym["DDD"] == "wait"
    assert broker.buys == []


def test_open_circuit_reports_why_in_heartbeat_and_progress(tmp_path, monkeypatch):
    # P0-B-1: when the circuit opens, the cycle must say WHY the analyst was
    # unavailable — the CLI provider's classification (quota, with the CLI's
    # own "try again at ..." hint) lands in the heartbeat stamp and as an
    # `llm_unavailable` entry in progress.json, not just in a log line.
    import json

    from agentic_trading import heartbeat as hb
    from agentic_trading.llm import cli_provider
    from agentic_trading.progress import read_progress

    analyst_shim = tmp_path / "analyst.cmd"
    analyst_shim.write_text("@echo off\r\n", encoding="utf-8")
    settings = _settings()
    settings.analyst_provider = "cli"
    settings.analyst_cli_path = str(analyst_shim)
    settings.watchlist = ["AAA", "BBB", "CCC", "DDD"]
    settings.core_watchlist = list(settings.watchlist)
    feed = HarnessFeed({s: _upward_df() for s in settings.watchlist})
    broker = HarnessBroker()

    def quota_exhausted(signal, *args, **kwargs):
        # What analyze_via_cli does internally on a non-zero exit.
        cli_provider._record_failure(
            "quota", signal.symbol, exit_code=1, elapsed_s=2.5,
            raw="Reading additional input from stdin...\nERROR: You've hit your usage limit. "
                "Upgrade or try again at Sep 20th, 2026 4:00 PM.",
        )
        return None

    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    monkeypatch.setattr("agentic_trading.run.analyze", quota_exhausted)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=False, fast_mode=False,
                  settings=settings, broker=broker, feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
    finally:
        conn.close()
        cli_provider._clear_failure()

    stamp = json.loads(hb.HEARTBEAT_PATH.read_text(encoding="utf-8"))["deep"]
    status = stamp["llm_status"]
    assert status["state"] == "circuit_open"
    assert status["category"] == "quota"
    assert status["failures"] == LLM_FAIL_FAST_STREAK and status["calls"] == LLM_FAIL_FAST_STREAK
    assert "Sep 20th, 2026 4:00 PM" in status["retry_hint"]
    # The watchdog turns an open circuit into an alert (lead ruling 2026-09-17:
    # two days of silent quant-only is exactly what must ring).
    problem = hb.check_heartbeat(now=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert problem is not None and "quota" in problem and "Sep 20th" in problem

    card = read_progress()
    kinds = {item["kind"] for item in card["unresolved"]}
    assert "llm_unavailable" in kinds
    why = next(item["detail"] for item in card["unresolved"] if item["kind"] == "llm_unavailable")
    assert "quota" in why and "Sep 20th" in why
    assert "llm_fail_closed" in kinds  # per-name entries are still there


def test_healthy_llm_cycle_stamps_state_ok(tmp_path, monkeypatch):
    import json

    from agentic_trading import heartbeat as hb

    analyst_shim = tmp_path / "analyst.cmd"
    analyst_shim.write_text("@echo off\r\n", encoding="utf-8")
    settings = _settings()
    settings.analyst_provider = "cli"
    settings.analyst_cli_path = str(analyst_shim)
    feed = HarnessFeed({"AAA": _upward_df()})
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    monkeypatch.setattr("agentic_trading.run.analyze", _bullish_verdict)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=False, fast_mode=False,
                  settings=settings, broker=HarnessBroker(), feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
    finally:
        conn.close()
    stamp = json.loads(hb.HEARTBEAT_PATH.read_text(encoding="utf-8"))["deep"]
    assert stamp["llm_status"] == {"state": "ok", "calls": 1, "failures": 0}
    assert hb.llm_status_note({"deep": stamp}) is None


def test_skip_llm_cycle_stamps_state_off(tmp_path, monkeypatch):
    import json

    from agentic_trading import heartbeat as hb

    feed = HarnessFeed({"AAA": _upward_df()})
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=True, skip_llm=True, fast_mode=False,
                  settings=_settings(), broker=HarnessBroker(), feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
    finally:
        conn.close()
    stamp = json.loads(hb.HEARTBEAT_PATH.read_text(encoding="utf-8"))["deep"]
    assert stamp["llm_status"]["state"] == "off"
    assert "skip-llm" in stamp["llm_status"]["reason"]


def test_unreadable_open_orders_mark_coverage_unknown_end_to_end(tmp_path, monkeypatch):
    # Review R2, whole chain: broker's open-orders read fails during the cycle
    # → heartbeat stamp says stops_unknown (not "no fields"), --check alerts,
    # progress.json carries stop_coverage_unknown and is not status=ok.
    import json

    from agentic_trading import heartbeat as hb
    from agentic_trading.progress import read_progress

    class BlindBroker(HarnessBroker):
        def get_open_orders(self):
            raise RuntimeError("orders endpoint 502")

    held = Position(symbol="AAA", qty=10.0, avg_entry_price=90.0, current_price=100.0, market_value=1000.0)
    broker = BlindBroker(positions={"AAA": held})
    feed = HarnessFeed({"AAA": _upward_df()})
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=True, fast_mode=False,
                  settings=_settings(), broker=broker, feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
    finally:
        conn.close()
    stamp = json.loads(hb.HEARTBEAT_PATH.read_text(encoding="utf-8"))["deep"]
    assert stamp["stops_unknown"] is True
    assert stamp["stops_covered"] is None and stamp["positions"] == 1
    assert "not verified" in stamp["stops_unknown_reason"]
    problem = hb.check_heartbeat(now=datetime.now(timezone.utc) + timedelta(seconds=1))
    assert problem is not None and "没能核验止损覆盖" in problem
    card = read_progress()
    kinds = [u["kind"] for u in card["unresolved"]]
    assert "stop_coverage_unknown" in kinds and "naked_stops" not in kinds
    assert card["round"]["status"] == "incomplete"
    assert card["risk"]["stops_covered"] is None and card["risk"]["stops_total"] == 1


class _FilledFillsReader:
    """BookBrokerReader stand-in whose fill history already contains the
    stop's sell — what the broker really shows once a stop fills on submit."""

    def __init__(self, fills):
        self._fills = fills
        self.last_fills_truncated = False

    def __call__(self, _book_root):
        return self

    def fills(self):
        return self._fills, None


def _run_cycle_with_stop_that_fills(monkeypatch, *, positions_after_fill):
    """XOM's stop fills the instant it is submitted (gap below the trail); the
    broker's positions no longer hold XOM afterwards. Returns (broker, card,
    stamp)."""
    import json

    from agentic_trading import heartbeat as hb
    from agentic_trading.progress import read_progress

    df = _upward_df()
    last = float(df["Close"].iloc[-1])

    class FillingBroker(HarnessBroker):
        """The cycle-start stop rests normally (but this harness never lists
        open orders, so the end-of-cycle reconciliation places again). THAT
        second submit — after run_cycle has already re-read final_positions —
        fills instantly, and the book no longer holds XOM from then on."""

        def __init__(self):
            super().__init__(positions={
                "XOM": Position(symbol="XOM", qty=10.0, avg_entry_price=last * 0.9,
                                current_price=last, market_value=10 * last),
            })
            self.after = positions_after_fill
            self.filled = False

        def get_positions(self):
            return dict(self.after) if self.filled else dict(self.positions)

        def submit_stop_sell(self, symbol, qty, stop_price, *, client_order_id=None):
            self.stops.append((symbol, qty, stop_price))
            if symbol == "XOM" and len([s for s, _q, _p in self.stops if s == "XOM"]) >= 2:
                self.filled = True
                return OrderResult(symbol=symbol, side="sell", qty=qty, status="filled", order_id="filled-1")
            return OrderResult(symbol=symbol, side="sell", qty=qty, status="accepted", order_id="s")

    broker = FillingBroker()
    fills = [
        {"id": "f1", "order_id": "b1", "symbol": "XOM", "side": "buy", "qty": 10.0, "price": last * 0.9,
         "notional": 9 * last, "transaction_time": "2026-09-17T14:00:00Z"},
        {"id": "f2", "order_id": "filled-1", "symbol": "XOM", "side": "sell", "qty": 10.0, "price": last * 0.95,
         "notional": 9.5 * last, "transaction_time": "2026-09-18T14:00:00Z"},
    ]
    monkeypatch.setattr("agentic_trading.run.BookBrokerReader", _FilledFillsReader(fills))
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: last)
    monkeypatch.setattr("agentic_trading.run.time.sleep", lambda _s: None)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=True, fast_mode=False,
                  settings=_settings(), broker=broker, feed=HarnessFeed({"AAA": df, "XOM": df, "NVDA": df}),
                  conn=conn, regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
    finally:
        conn.close()
    return broker, read_progress(), json.loads(hb.HEARTBEAT_PATH.read_text(encoding="utf-8"))["deep"]


def test_stop_filled_on_submit_refreshed_positions_reach_progress_and_heartbeat(monkeypatch):
    # Review round 2, R1: the reconciliation re-read positions after the fill
    # but run_cycle kept handing the PRE-fill snapshot to progress, whose
    # fills-vs-positions comparison then saw XOM "held" against fills that
    # already contain the sell → a false position_mismatch.
    broker, card, stamp = _run_cycle_with_stop_that_fills(monkeypatch, positions_after_fill={})
    # One stop at cycle start (rested), one at cycle end (filled) — and NOT a
    # third one after the fill.
    assert [s for s, _q, _p in broker.stops] == ["XOM", "XOM"], broker.stops
    kinds = [u["kind"] for u in card["unresolved"]]
    assert "position_mismatch" not in kinds, card["unresolved"]
    assert card["broker"]["n_positions"] == 0
    assert card["risk"]["stops_covered"] == 0 and card["risk"]["stops_total"] == 0
    assert stamp["stops_covered"] == 0 and stamp["positions"] == 0
    assert "naked_stops" not in kinds


def test_stop_filled_on_submit_new_symbol_after_refresh_is_not_reported_as_empty_book(monkeypatch):
    # A buy that filled during the loop shows up in the refreshed book: the
    # refreshed positions (not old ∩ new) are what count — one held, uncovered.
    new = Position(symbol="NVDA", qty=2.0, avg_entry_price=100.0, current_price=100.0, market_value=200.0)
    broker, card, stamp = _run_cycle_with_stop_that_fills(monkeypatch, positions_after_fill={"NVDA": new})
    assert card["broker"]["n_positions"] == 1
    assert card["risk"]["stops_total"] == 1 and card["risk"]["stops_covered"] == 0
    assert stamp["positions"] == 1 and stamp["stops_covered"] == 0
    assert "naked_stops" in [u["kind"] for u in card["unresolved"]]


def _sliding_closes():
    """60 flat sessions, then a 9% slide: quant score past sell_threshold
    while the last price is still above the trailing stop (signal exit, not
    a hard stop). Same shape as the off-watchlist signal-sell test."""
    closes = [100.0 + (0.3 if i % 2 else -0.3) for i in range(60)]
    return closes + [100.0 * (1 - 0.09 * (i + 1) / 20) for i in range(20)]


def _run_cycle_with_opening_stop_that_fills(monkeypatch, *, positions_after_fill, reread_fail_times=0,
                                            extra_positions=None):
    """OLD is held at cycle start; its FIRST protective stop (the cycle-start
    reconciliation) fills on submit, and the book no longer holds OLD. Later
    in the same cycle the falling tape produces a signal SELL for OLD. Returns
    (broker, card, stamp). `reread_fail_times` = how many get_positions calls
    AFTER the fill raise before the book becomes readable again (the opening
    refresh is the first such call; run_cycle's end-of-cycle re-read the next)."""
    import json

    from agentic_trading import heartbeat as hb
    from agentic_trading.progress import read_progress

    closes = _sliding_closes()
    old = Position(symbol="OLD", qty=10.0, avg_entry_price=50.0,
                   current_price=closes[-1], market_value=10.0 * closes[-1])

    class OpeningFillBroker(HarnessBroker):
        def __init__(self):
            super().__init__(positions={"OLD": old, **(extra_positions or {})})
            self.filled = False
            self.position_reads_after_fill = 0

        def get_positions(self):
            if self.filled:
                self.position_reads_after_fill += 1
                if self.position_reads_after_fill <= reread_fail_times:
                    raise RuntimeError("positions endpoint 502")
                return dict(positions_after_fill)
            return dict(self.positions)

        def submit_stop_sell(self, symbol, qty, stop_price, *, client_order_id=None):
            self.stops.append((symbol, qty, stop_price))
            if symbol == "OLD" and not self.filled:
                self.filled = True   # gap below the trail: the very first stop fires
                return OrderResult(symbol=symbol, side="sell", qty=qty, status="filled", order_id="f-OLD")
            return OrderResult(symbol=symbol, side="sell", qty=qty, status="accepted", order_id="s")

    broker = OpeningFillBroker()
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    monkeypatch.setattr("agentic_trading.run.time.sleep", lambda _s: None)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=False, fast_mode=False,
                  settings=_settings(), broker=broker,
                  feed=HarnessFeed({"AAA": _upward_df(), "OLD": _df(closes), "NVDA": _upward_df(),
                                    "KEEP": _upward_df()}),
                  conn=conn, regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
    finally:
        conn.close()
    return broker, read_progress(), json.loads(hb.HEARTBEAT_PATH.read_text(encoding="utf-8"))["deep"]


def test_opening_stop_fill_blocks_a_second_sell_from_the_stale_snapshot(monkeypatch):
    # Review round 3, R1: the cycle-start reconciliation refreshed the book
    # internally but run_cycle kept the pre-fill snapshot, so the signal SELL
    # for OLD later in the cycle called _submit_protected_sell with the old
    # 10 shares — a second sale of shares the stop had already sold.
    broker, card, stamp = _run_cycle_with_opening_stop_that_fills(monkeypatch, positions_after_fill={})
    assert [s for s, _q, _p in broker.stops if s == "OLD"] == ["OLD"], "one stop for OLD, then nothing"
    assert broker.sells == [], f"no market sell may follow the filled stop: {broker.sells}"
    assert broker.cancels == 0
    # Heartbeat and progress describe the final book, not the stale snapshot.
    assert stamp["positions"] == 0 and stamp["stops_covered"] == 0
    assert card["broker"]["n_positions"] == 0
    assert card["risk"]["stops_total"] == 0
    assert "naked_stops" not in [u["kind"] for u in card["unresolved"]]


def test_opening_stop_fill_with_failed_rereads_is_reported_as_unverified(monkeypatch):
    # Review round 4, R1 — the full failure path: the opening stop fills, the
    # refresh fails, OLD is excluded so nothing resells it, positions is now
    # the filtered {} … and the end-of-cycle re-read fails too. That filtered
    # dict is an EXECUTION exclusion, not a verified empty book: the heartbeat
    # may not record a clean (0,0) check, the watchdog must alert, progress
    # must be incomplete, and no fills-vs-positions verdict may be drawn from it.
    import json

    from agentic_trading import heartbeat as hb

    broker, card, stamp = _run_cycle_with_opening_stop_that_fills(
        monkeypatch, positions_after_fill={}, reread_fail_times=99,
    )
    # 1. Still no second sale.
    assert [s for s, _q, _p in broker.stops if s == "OLD"] == ["OLD"]
    assert broker.sells == [], f"a stale snapshot must not drive a sell: {broker.sells}"
    # 2. The last check is UNVERIFIED, with the reason, not a clean 0/0.
    hb_file = json.loads(hb.HEARTBEAT_PATH.read_text(encoding="utf-8"))
    check = hb.latest_stop_check(hb_file)
    assert check["unknown"] is True, check
    assert "re-read" in (check["reason"] or "") and "OLD" in (check["reason"] or "")
    assert stamp.get("stops_unknown") is True and stamp["stops_covered"] is None
    assert stamp["positions"] is None, "the held count is unknown, not 0"
    # 3. The watchdog alerts on exactly that.
    problem = hb.check_heartbeat(now=datetime.now(timezone.utc) + timedelta(seconds=1))
    assert problem is not None and "没能核验止损覆盖" in problem and "re-read" in problem
    # 4. progress: incomplete, positions marked unverified, no determinate
    #    fills verdict from the filtered snapshot.
    kinds = [u["kind"] for u in card["unresolved"]]
    assert card["round"]["status"] == "incomplete"
    assert "positions_unverified" in kinds and "stop_coverage_unknown" in kinds
    assert "position_mismatch" not in kinds
    assert "positions_unavailable" in kinds, "fills reconciliation must take the unavailable branch"
    assert card["broker"]["degraded"] is True and "last known" in (card["broker"]["reason"] or "")
    # Round 5: the "last known" number is the last SUCCESSFUL broker read (1:
    # OLD, at cycle start) — not the execution-filtered dict (0 after OLD was
    # excluded), which is not a read at all.
    assert card["broker"]["n_positions"] == 1
    assert "last known snapshot: 1 positions" in card["broker"]["reason"]
    unverified = next(u for u in card["unresolved"] if u["kind"] == "positions_unverified")
    assert "last known snapshot: 1 positions" in unverified["detail"]
    assert stamp["positions"] is None and card["round"]["status"] == "incomplete"


def test_last_known_count_is_the_last_successful_read_not_the_filtered_set(monkeypatch):
    # Two holdings at cycle start (OLD + KEEP). OLD's stop fills, every later
    # positions read fails: execution works from {KEEP} (1), but the last
    # verified read said 2 — and 2 is what the card must call "last known".
    keep = Position(symbol="KEEP", qty=3.0, avg_entry_price=100.0, current_price=100.0, market_value=300.0)
    broker, card, stamp = _run_cycle_with_opening_stop_that_fills(
        monkeypatch, positions_after_fill={"KEEP": keep}, reread_fail_times=99,
        extra_positions={"KEEP": keep},
    )
    assert [s for s, _q, _p in broker.stops if s == "OLD"] == ["OLD"]
    assert not [s for s in broker.sells if s[0] == "OLD"], "OLD is never resold"
    assert card["broker"]["n_positions"] == 2
    assert "last known snapshot: 2 positions" in card["broker"]["reason"]
    unverified = next(u for u in card["unresolved"] if u["kind"] == "positions_unverified")
    assert "last known snapshot: 2 positions" in unverified["detail"]
    assert stamp["positions"] is None and stamp["stops_unknown"] is True
    assert card["round"]["status"] == "incomplete"
    assert card["risk"]["stops_covered"] is None and card["risk"]["stops_total"] is None
    assert card["risk"]["stops_covered"] is None and card["risk"]["stops_total"] is None


def test_opening_refresh_failure_recovered_by_a_successful_end_reread_empty_book(monkeypatch):
    # Contrast: the opening refresh failed, but the end-of-cycle re-read
    # succeeded and returned the real (empty) book. Real numbers, verified
    # check, no lingering "unverified" — and still no resale of OLD.
    import json

    from agentic_trading import heartbeat as hb

    broker, card, stamp = _run_cycle_with_opening_stop_that_fills(
        monkeypatch, positions_after_fill={}, reread_fail_times=1,
    )
    assert [s for s, _q, _p in broker.stops if s == "OLD"] == ["OLD"] and broker.sells == []
    check = hb.latest_stop_check(json.loads(hb.HEARTBEAT_PATH.read_text(encoding="utf-8")))
    assert check["unknown"] is False and check["positions"] == 0 and check["stops_covered"] == 0
    assert "stops_unknown" not in stamp
    kinds = [u["kind"] for u in card["unresolved"]]
    assert "positions_unverified" not in kinds and "stop_coverage_unknown" not in kinds
    assert "positions_stale" not in kinds
    assert card["round"]["status"] == "ok"
    assert card["broker"]["n_positions"] == 0 and card["broker"]["degraded"] is False
    assert hb.check_heartbeat(now=datetime.now(timezone.utc) + timedelta(seconds=1)) is None or         "没能核验" not in hb.check_heartbeat(now=datetime.now(timezone.utc) + timedelta(seconds=1))


def test_opening_refresh_failure_recovered_by_a_successful_end_reread_with_holdings(monkeypatch):
    import json

    from agentic_trading import heartbeat as hb

    nvda = Position(symbol="NVDA", qty=2.0, avg_entry_price=100.0, current_price=100.0, market_value=200.0)
    broker, card, stamp = _run_cycle_with_opening_stop_that_fills(
        monkeypatch, positions_after_fill={"NVDA": nvda}, reread_fail_times=1,
    )
    assert broker.sells == []
    assert [s for s, _q, _p in broker.stops if s == "OLD"] == ["OLD"]
    # NVDA appeared off-watchlist mid-cycle, so no ATR was computed for it and
    # no stop is sized — it is a real, counted, UNCOVERED holding (a verified
    # 0/1, which is a naked-stop alert), not an unknown.
    check = hb.latest_stop_check(json.loads(hb.HEARTBEAT_PATH.read_text(encoding="utf-8")))
    assert check["unknown"] is False and check["positions"] == 1 and check["stops_covered"] == 0
    assert card["broker"]["n_positions"] == 1 and card["broker"]["degraded"] is False
    assert card["risk"]["stops_total"] == 1 and card["risk"]["stops_covered"] == 0
    kinds = [u["kind"] for u in card["unresolved"]]
    assert "naked_stops" in kinds
    assert "positions_unverified" not in kinds and "stop_coverage_unknown" not in kinds


def test_opening_stop_fill_keeps_other_holdings_in_play(monkeypatch):
    # The refreshed book (NVDA still held) is what the rest of the cycle works
    # with: NVDA gets its stop, is counted, and OLD is simply gone.
    nvda = Position(symbol="NVDA", qty=2.0, avg_entry_price=100.0, current_price=100.0, market_value=200.0)
    broker, card, stamp = _run_cycle_with_opening_stop_that_fills(monkeypatch, positions_after_fill={"NVDA": nvda})
    assert broker.sells == []
    assert card["broker"]["n_positions"] == 1
    assert stamp["positions"] == 1


def test_stop_filled_on_submit_and_reread_failure_keeps_the_uncertainty(monkeypatch):
    # The refresh after the fill fails: the card keeps the pre-fill snapshot
    # but says so (positions_stale), rather than claiming a verified book.
    import json

    from agentic_trading import heartbeat as hb
    from agentic_trading.progress import read_progress

    df = _upward_df()
    last = float(df["Close"].iloc[-1])

    class FillingThenBlind(HarnessBroker):
        def __init__(self):
            super().__init__(positions={"XOM": Position(symbol="XOM", qty=10.0, avg_entry_price=last * 0.9,
                                                        current_price=last, market_value=10 * last)})
            self.filled = False

        def get_positions(self):
            if self.filled:
                raise RuntimeError("positions endpoint 502")
            return dict(self.positions)

        def submit_stop_sell(self, symbol, qty, stop_price, *, client_order_id=None):
            self.stops.append((symbol, qty, stop_price))
            if len(self.stops) >= 2:
                self.filled = True
                return OrderResult(symbol=symbol, side="sell", qty=qty, status="filled", order_id="f")
            return OrderResult(symbol=symbol, side="sell", qty=qty, status="accepted", order_id="s")

    broker = FillingThenBlind()
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: last)
    monkeypatch.setattr("agentic_trading.run.time.sleep", lambda _s: None)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=True, fast_mode=False,
                  settings=_settings(), broker=broker, feed=HarnessFeed({"AAA": df, "XOM": df}), conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
    finally:
        conn.close()
    assert [s for s, _q, _p in broker.stops] == ["XOM", "XOM"]
    # Round 4 principle: a snapshot that could not be re-read after a fill is
    # UNVERIFIED — the monitors get "unknown + reason", never a tidy count.
    card = read_progress()
    kinds = [u["kind"] for u in card["unresolved"]]
    assert kinds.count("positions_unverified") == 1 and "stop_coverage_unknown" in kinds
    assert "positions_stale" not in kinds
    assert card["round"]["status"] == "incomplete"
    assert card["broker"]["degraded"] is True and "last known snapshot: 1 positions" in card["broker"]["reason"]
    assert card["broker"]["n_positions"] == 1          # the last known count, labelled as such
    assert card["risk"]["stops_covered"] is None and card["risk"]["stops_total"] is None
    stamp = json.loads(hb.HEARTBEAT_PATH.read_text(encoding="utf-8"))["deep"]
    assert stamp["stops_unknown"] is True and stamp["stops_covered"] is None and stamp["positions"] is None
    assert "re-read failed" in stamp["stops_unknown_reason"]
    assert "没能核验止损覆盖" in hb.check_heartbeat(now=datetime.now(timezone.utc) + timedelta(seconds=1))


def test_readable_open_orders_do_not_mark_coverage_unknown(tmp_path, monkeypatch):
    import json

    from agentic_trading import heartbeat as hb
    from agentic_trading.progress import read_progress

    df = _upward_df()
    last = float(df["Close"].iloc[-1])
    held = Position(symbol="AAA", qty=10.0, avg_entry_price=last * 0.9, current_price=last, market_value=10 * last)
    feed = HarnessFeed({"AAA": df})
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: last)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=True, fast_mode=False,
                  settings=_settings(), broker=HarnessBroker(positions={"AAA": held}), feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
    finally:
        conn.close()
    stamp = json.loads(hb.HEARTBEAT_PATH.read_text(encoding="utf-8"))["deep"]
    assert "stops_unknown" not in stamp and stamp["stops_covered"] == 1 and stamp["positions"] == 1
    assert "stop_coverage_unknown" not in [u["kind"] for u in read_progress()["unresolved"]]


def test_fast_scan_with_no_escalation_stamps_off_not_ok(tmp_path, monkeypatch):
    # The analyst is configured and healthy, but a flat tape crosses no
    # threshold so the fast tier never asks it. "ok" would tell the watchdog
    # reader the analyst answered; it did not.
    import json
    import math

    from agentic_trading import heartbeat as hb

    analyst_shim = tmp_path / "analyst.cmd"
    analyst_shim.write_text("@echo off\r\n", encoding="utf-8")
    settings = _settings()
    settings.analyst_provider = "cli"
    settings.analyst_cli_path = str(analyst_shim)
    flat = _df([100.0 + 0.3 * math.sin(i / 3) for i in range(80)])
    feed = HarnessFeed({"AAA": flat})
    calls = []
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    monkeypatch.setattr("agentic_trading.run.analyze",
                        lambda *a, **k: calls.append(1) or _bullish_verdict(*a, **k))
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=False, fast_mode=True,
                  settings=settings, broker=HarnessBroker(), feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
    finally:
        conn.close()
    assert calls == [], "a flat tape must not escalate"
    stamp = json.loads(hb.HEARTBEAT_PATH.read_text(encoding="utf-8"))["fast"]
    assert stamp["llm_status"] == {
        "state": "off", "reason": "fast tier: no symbol escalated to the analyst this scan", "calls": 0,
    }


def test_llm_status_states():
    from agentic_trading.run import _LlmStatus

    assert _LlmStatus(use_llm=False, reason="x").as_dict() == {"state": "off", "reason": "x"}
    assert _LlmStatus(use_llm=True, fast_mode=True).as_dict()["state"] == "off"
    assert _LlmStatus(use_llm=True).as_dict() == {"state": "off", "reason": "no analyst call this cycle", "calls": 0}
    s = _LlmStatus(use_llm=True)
    s.calls, s.failures = 5, 0
    assert s.as_dict()["state"] == "ok"
    s.failures = 2
    assert s.as_dict()["state"] == "degraded"
    s.circuit_open = True
    assert s.as_dict()["state"] == "circuit_open"


def test_llm_fail_streak_resets_on_a_success(tmp_path, monkeypatch):
    # Two failures, one success, two more failures: the circuit must not trip
    # because the streak is consecutive, not cumulative.
    analyst_shim = tmp_path / "analyst.cmd"
    analyst_shim.write_text("@echo off\r\n", encoding="utf-8")
    settings = _settings()
    settings.analyst_provider = "cli"
    settings.analyst_cli_path = str(analyst_shim)
    settings.watchlist = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    settings.core_watchlist = list(settings.watchlist)
    frames = {s: _upward_df() for s in settings.watchlist}
    feed = HarnessFeed(frames)
    broker = HarnessBroker()
    calls = []

    def flaky(signal, news, fundamentals, settings):
        calls.append(signal.symbol)
        if signal.symbol == "CCC":
            return _bullish_verdict(signal, news, fundamentals, settings)
        return None

    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    monkeypatch.setattr("agentic_trading.run.analyze", flaky)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=False, fast_mode=False,
                  settings=settings, broker=broker, feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
    finally:
        conn.close()
    assert calls == ["AAA", "BBB", "CCC", "DDD", "EEE"], calls


def test_missing_live_quote_preserves_the_intent(intent_conn, monkeypatch):
    # Without a quote the gap/chase checks cannot run at flush time; the intent
    # waits for a later scan rather than buying blind.
    _save(intent_conn, "NEW", age_hours=1)
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: None)
    broker = FlushBroker()
    _flush(intent_conn, broker)
    assert broker.buys == []
    assert [i.symbol for i in load_trade_intents(intent_conn)] == ["NEW"]
    clear_trade_intent(intent_conn, "NEW")


def test_off_watchlist_holding_gets_exit_checks_and_peak_updates(monkeypatch):
    # A position whose symbol left the watchlist must not lose protection:
    # its peak still ratchets and a crash below the trailing stop still exits.
    crashed = Position(symbol="OLD", qty=10.0, avg_entry_price=100.0,
                       current_price=80.0, market_value=800.0)
    feed = HarnessFeed({
        "AAA": _upward_df(),
        # ~22% under entry breaches even the widest clamped ATR stop (20%).
        "OLD": _downward_df(days=80, start=100.0, end=78.0),
    })
    broker = HarnessBroker(positions={"OLD": crashed})
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=False, fast_mode=False,
                  settings=_settings(), broker=broker, feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
        rows = conn.execute(
            "SELECT symbol, action FROM decisions WHERE symbol='OLD'"
        ).fetchall()
        peak = conn.execute(
            "SELECT high_water_mark FROM position_peaks WHERE symbol='OLD'"
        ).fetchone()
    finally:
        conn.close()
    sells = [s for s in broker.sells if s[0] == "OLD" and s[2] == "sell"]
    assert sells, "off-watchlist holding should be force-exited through the client-side check"
    assert rows and rows[-1][1] == "sell"
    # A closed position leaves no stale peak behind to poison a re-entry.
    assert peak is None


def test_healthy_off_watchlist_holding_is_held_but_peaked(monkeypatch):
    steady = Position(symbol="OLD", qty=10.0, avg_entry_price=100.0,
                      current_price=101.0, market_value=1010.0)
    feed = HarnessFeed({
        "AAA": _upward_df(),
        # Real uptrend (quant ~+0.33): comfortably above legacy_sell_threshold,
        # so this stays a test of "a holding that still earns its exposure is
        # kept". A flat series scores ~+0.01 and now exits on the legacy bar —
        # that case is test_flat_off_watchlist_holding_exits_on_the_legacy_bar.
        "OLD": _upward_df(days=80, start=100.0, daily=0.003),
    })
    broker = HarnessBroker(positions={"OLD": steady})
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    conn = connect(":memory:")
    run_cycle(force_dry_run=False, skip_llm=False, fast_mode=False,
              settings=_settings(), broker=broker, feed=feed, conn=conn,
              regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
    try:
        peak = conn.execute(
            "SELECT high_water_mark FROM position_peaks WHERE symbol='OLD'"
        ).fetchone()
        actions = conn.execute(
            "SELECT action FROM decisions WHERE symbol='OLD' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert broker.sells == []
    assert peak is not None  # peak refreshed even though nothing exited
    assert actions is None or actions[0] != "buy"  # never re-buys what it holds


def test_flat_off_watchlist_holding_exits_on_the_legacy_bar(monkeypatch):
    # A leftover from a pool swap is outside the book under validation but still
    # occupies its exposure and book stop-risk. legacy_sell_threshold retires one
    # that has gone flat (quant ~+0.01) well before sell_threshold (-0.25) would,
    # returning the budget to the names actually being tested.
    steady = Position(symbol="OLD", qty=10.0, avg_entry_price=100.0,
                      current_price=101.0, market_value=1010.0)
    feed = HarnessFeed({
        "AAA": _upward_df(),
        "OLD": _upward_df(days=80, start=100.0, daily=0.0001),
    })
    broker = HarnessBroker(positions={"OLD": steady})
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=False, fast_mode=False,
                  settings=_settings(), broker=broker, feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
        action = conn.execute(
            "SELECT action FROM decisions WHERE symbol='OLD' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert action and action[0] == "sell"
    assert [s for s in broker.sells if s[0] == "OLD" and s[2] == "sell"]


def test_watchlist_holding_is_not_subject_to_the_legacy_bar(monkeypatch):
    # The stricter bar is scoped to names that fell off the watchlist. A held
    # name still on it keeps sell_threshold, so the core pool's exit semantics
    # are untouched: flat is a HOLD there, not a SELL.
    steady = Position(symbol="AAA", qty=10.0, avg_entry_price=100.0,
                      current_price=101.0, market_value=1010.0)
    feed = HarnessFeed({"AAA": _upward_df(days=80, start=100.0, daily=0.0001)})
    broker = HarnessBroker(positions={"AAA": steady})
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=False, fast_mode=False,
                  settings=_settings(), broker=broker, feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
        action = conn.execute(
            "SELECT action FROM decisions WHERE symbol='AAA' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert action and action[0] == "hold"
    assert [s for s in broker.sells if s[0] == "AAA" and s[2] == "sell"] == []


def test_off_watchlist_holding_still_gets_a_signal_sell(monkeypatch):
    # Hard stops are not the whole exit contract. A decayed holding that left
    # the watchlist can sit above both its ATR stop and its trailing stop while
    # its quant score has fallen through sell_threshold — before this was
    # wired, no decide() ran for it, so the signal exit could never fire and
    # nothing about it reached the journal.
    # 60 flat sessions, then a 9% slide: score -0.36 (past -0.25) with the last
    # price still above the 12%-from-peak trail, and entry far below both.
    closes = [100.0 + (0.3 if i % 2 else -0.3) for i in range(60)]
    closes += [100.0 * (1 - 0.09 * (i + 1) / 20) for i in range(20)]
    winner = Position(symbol="OLD", qty=10.0, avg_entry_price=50.0,
                      current_price=closes[-1], market_value=10.0 * closes[-1])
    feed = HarnessFeed({"AAA": _upward_df(), "OLD": _df(closes)})
    broker = HarnessBroker(positions={"OLD": winner})
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    conn = connect(":memory:")
    try:
        run_cycle(force_dry_run=False, skip_llm=False, fast_mode=False,
                  settings=_settings(), broker=broker, feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
        row = conn.execute(
            "SELECT action, quant_score FROM decisions WHERE symbol='OLD' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "an off-watchlist holding must reach the journal"
    assert row[0] == "sell"
    assert row[1] <= -0.25, "the sell must be the signal exit, not a hard stop"
    assert [s for s in broker.sells if s[0] == "OLD" and s[2] == "sell"]


# --- order id minting / stop rejection retry -----------------------------------

def test_order_id_minter_gives_each_action_a_distinct_id():
    # Two placements in one cycle (place, then cancel/replace) must not share
    # an id: Alpaca burns a consumed client_order_id and 422s the reuse.
    ids = OrderIdMinter("2026-08-24T14:00:00+00:00")
    first = ids.mint("stop", "ZETA")
    second = ids.mint("stop", "ZETA")
    assert first != second
    assert first.startswith("at-stop-ZETA-")
    assert all(ch.isalnum() or ch == "-" for ch in first)


def test_reconcile_retries_with_a_fresh_id_after_an_explicit_rejection(monkeypatch):
    # Attempt 1 and its same-id retry both fail (e.g. Alpaca's wash-trade rule
    # while the entry buy is still settling). The third attempt — after the
    # settle delay — must carry a FRESH id: an explicit rejection leaves no
    # order behind, so a new id cannot double anything, and reusing the burned
    # one would just 422 again and leave the position bare.
    sleeps: list[float] = []
    monkeypatch.setattr("agentic_trading.run.time.sleep", lambda s: sleeps.append(s))

    class WashTraded(StopBroker):
        def __init__(self):
            super().__init__()
            self.ids = []

        def submit_stop_sell(self, symbol, qty, stop_price, *, client_order_id=None):
            self.ids.append(client_order_id)
            if len(self.ids) < 3:
                return None
            return OrderResult(symbol=symbol, side="sell", qty=qty, status="accepted", order_id="s")

    pos = Position(symbol="MSFT", qty=3.74, avg_entry_price=481.0,
                   current_price=481.0, market_value=1797.9)
    broker = WashTraded()
    _reconcile_protective_stops(broker, {"MSFT": pos}, {}, {"MSFT": 12.0}, RISK, LOG, live=True)
    assert len(broker.ids) == 3
    assert broker.ids[0] == broker.ids[1], "an ambiguous-failure retry must reuse the id"
    assert broker.ids[2] != broker.ids[1], "the post-rejection retry must mint a fresh id"
    assert STOP_REJECTION_RETRY_SECONDS in sleeps


# --- TRIM double buffer (depth + dwell) -----------------------------------------

def test_trim_needs_depth_and_dwell():
    # Selling the existing book requires the regime to be beyond the trigger
    # score (depth) on this many consecutive deep-cycle readings (time). A
    # shallow dip arms nothing; one deep reading arms nothing; recovery resets.
    from dataclasses import replace

    from agentic_trading.run import _update_trim_dwell

    risk = replace(RISK, trim_trigger_score=-0.30, trim_confirm_cycles=2)
    shallow = MacroRegime(score=-0.25, label="risk_off")   # past risk_off, not deep enough
    deep = MacroRegime(score=-0.35, label="risk_off")
    conn = connect(":memory:")
    try:
        assert _update_trim_dwell(conn, shallow, risk, LOG, "t0") is False
        assert _update_trim_dwell(conn, deep, risk, LOG, "t1") is False    # deep but 1/2 readings
        assert _update_trim_dwell(conn, deep, risk, LOG, "t2") is True     # second reading: armed
        assert _update_trim_dwell(conn, MacroRegime(score=0.10, label="neutral"), risk, LOG, "t3") is False
        assert _update_trim_dwell(conn, deep, risk, LOG, "t4") is False    # counter restarted: 1/2
    finally:
        conn.close()


# --- intent lifecycle: what survives a veto, and what gets journaled ----------

def _intent_events(conn):
    return conn.execute(
        "SELECT symbol, kind, deferred FROM intent_events ORDER BY id"
    ).fetchall()


def test_sizing_veto_keeps_the_intent_and_journals_it(intent_conn):
    # Exposure/book-risk/sector room are book-level and move every cycle, so a
    # sizing veto is transient: the same intent can size fine on the next scan.
    # Discarding it here threw away analysis over a passing condition, while the
    # chase guards (an equally transient, per-name condition) kept theirs.
    _save(intent_conn, "AAA", age_hours=1)
    broker = FlushBroker()
    _flush_kwargs = dict(
        broker=broker, conn=intent_conn, settings=_settings(),
        account=Account(100_000.0, 100_000.0), positions={}, pending_buys=set(),
        peaks={}, cash_remaining=0.0,  # no cash -> sizing vetoes
        invested_value=0.0, open_position_count=0, orders_this_cycle=0,
        regime_multiplier=1.0, cycle_timestamp=FLUSH_NOW.isoformat(), log=LOG,
        atrs={}, feed=None, asof=None, sizing_risk=RISK, sector_map={},
        occupancy={}, orders_readable=True, now_utc=FLUSH_NOW,
    )
    _flush_trade_intents(**_flush_kwargs)
    assert broker.buys == []
    assert [i.symbol for i in load_trade_intents(intent_conn)] == ["AAA"]
    assert _intent_events(intent_conn) == [("AAA", "sizing", 1)]


def test_gap_veto_discards_the_intent_and_journals_it(intent_conn, monkeypatch):
    # The opposite case: the price the analysis was written against is gone, so
    # the intent is dropped rather than deferred. deferred=0 records which.
    _save(intent_conn, "AAA", age_hours=1, signal_price=100.0)
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 130.0)
    broker = FlushBroker()
    _flush(intent_conn, broker)
    assert broker.buys == []
    assert load_trade_intents(intent_conn) == []
    assert _intent_events(intent_conn) == [("AAA", "gap", 0)]


def test_expired_intent_is_journaled_as_ttl(intent_conn):
    _save(intent_conn, "OLD", age_hours=TRADE_INTENT_TTL.total_seconds() / 3600 + 1)
    _flush(intent_conn, FlushBroker())
    assert _intent_events(intent_conn) == [("OLD", "ttl", 0)]
