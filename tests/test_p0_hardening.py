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
