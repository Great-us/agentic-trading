"""Baseline trade-call trace for c2c_a7e2 (P1-A execution funnel).

Acceptance item #1 (PROGRESS.md §14.5): BEFORE any funnel code touches the
trading path, record the exact sequence of broker calls the current HEAD
(97a65d6) produces under a fixed clock, fixed market data, a fixed LLM
(--skip-llm) and a fake broker against a throwaway in-memory journal.

The scenario runs two complete run_cycle calls with the same frozen clock:

- cycle 1 (deep): SELLER's trailing stop forces a market SELL; RATCHET's
  protective stop is placed at the old peak and then ratcheted up after the
  bar refreshes the high-water mark; BUYA/BUYB/VETO/CHASE all decide BUY and
  queue as TradeIntents (live entries never market-buy on the signal).
- cycle 2 (the same instant, acting as the next scan): the flush executes
  BUYA and BUYB (VETO eats the sector room to the dollar so the third name
  is sizing-vetoed, CHASE is held back by the signal-relative chase guard).

Every submit_notional_buy records symbol / notional(float.hex()) / atr14
(float.hex()) / client_order_id plus the SELL and stop/cancel sequence. The
recorded fixture tests/fixtures/trade_trace_baseline.json is the contract:
later funnel work must reproduce it bit-for-bit. The fixture is NEVER
rewritten by the test suite — regeneration is a manual, code-reviewed step
(`python -m tests.test_trade_trace_baseline` prints the observed events).
"""
from __future__ import annotations

import importlib
import itertools
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from agentic_trading.config import RiskConfig, Settings
from agentic_trading.execution.broker import Account, OpenOrder, OrderResult, Position
try:  # R9: this runner also executes against the exported 97a65d6 tree,
    from agentic_trading.execution.broker import OrderObservation  # pre-A-3
except ImportError:                                               # has no such class
    OrderObservation = None
from agentic_trading.journal.logger import connect, load_trade_intents, update_position_peak
from agentic_trading.run import run_cycle
from agentic_trading.signals.macro import MacroRegime

# A fixed Wednesday 14:30 UTC = 10:30 ET — inside the default entry window, so
# the flush executes deterministically no matter when the suite runs.
FIXED_NOW = datetime(2026, 8, 26, 14, 30, 0, tzinfo=timezone.utc)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "trade_trace_baseline.json"
BASELINE_COMMIT = "97a65d6"

RISK = RiskConfig(
    risk_per_trade_pct=0.015, max_position_pct=0.18, min_position_pct=0.04,
    max_total_exposure_pct=0.95, max_open_positions=6,
    max_new_orders_per_cycle=5, min_quant_score_to_consider=0.15,
    atr_stop_multiple=2.5, min_stop_pct=0.06, max_stop_pct=0.20,
    trailing_stop_pct=0.12, risk_off_size_multiplier=0.5, risk_off_score_penalty=0.15,
    escalation_cooldown_minutes=60, escalation_cooldown_score_delta=0.15,
    take_profit_pct=None,
)

WATCHLIST = ["BUYA", "BUYB", "VETO", "CHASE", "SELLER", "RATCHET"]


class FrozenDateTime(datetime):
    """datetime replacement pinned to FIXED_NOW (tz-aware and naive now())."""

    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is not None else FIXED_NOW.replace(tzinfo=None)


def _ramp_df(start: float, ratio: float, days: int = 80) -> pd.DataFrame:
    closes = [start * ratio ** i for i in range(days)]
    return pd.DataFrame({
        "Open": closes, "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes], "Close": closes,
        "Volume": [1_000_000] * days,
    })


def frames() -> dict[str, pd.DataFrame]:
    up = _ramp_df(100.0, 1.01)
    return {
        "BUYA": up, "BUYB": up.copy(), "VETO": up.copy(), "CHASE": up.copy(),
        "SELLER": _ramp_df(100.0, (78.0 / 100.0) ** (1 / 79)),
        "RATCHET": _ramp_df(100.0, 1.005),
    }


def last_close(frames_: dict[str, pd.DataFrame], symbol: str) -> float:
    return float(frames_[symbol]["Close"].iloc[-1])


class HarnessFeed:
    def __init__(self, frames_: dict[str, pd.DataFrame]):
        self.frames = frames_

    def price_history(self, symbol, asof=None, period=None):
        return self.frames[symbol]

    def news(self, symbol):
        return []

    def fundamentals(self, symbol):
        return {}


class TraceBroker:
    """Broker fake that records the exact trading-call sequence.

    Mimics Alpaca semantics the cycle relies on: a market SELL removes the
    position; a placed stop rests until cancelled; a notional buy is accepted
    but does not appear in positions (fills arrive later, out of band)."""

    def __init__(self, *, positions=None, equity=100_000.0, cash=100_000.0):
        self.positions = dict(positions or {})
        self.equity, self.cash = equity, cash
        self.events: list[dict] = []
        self.open_orders: list[OpenOrder] = []
        self._seq = itertools.count(1)
        # R4: real observation support — map order_id -> OrderObservation.
        # Unmapped ids observe as None (unverified), never a fabricated fill.
        self.observations: dict[str, object] = {}

    def observe_order(self, order_id):
        return self.observations.get(order_id)

    def _record(self, op: str, **fields) -> None:
        self.events.append({"op": op, **fields})

    def get_account(self):
        return Account(self.equity, self.cash)

    def get_positions(self):
        return dict(self.positions)

    def is_market_open(self):
        return True

    def get_open_orders(self):
        return list(self.open_orders)

    def submit_notional_buy(self, symbol, notional, *, atr14=None, client_order_id=None):
        self._record(
            "buy", symbol=symbol, notional=float(notional),
            notional_hex=float(notional).hex(),
            atr14=float(atr14) if atr14 is not None else None,
            atr14_hex=float(atr14).hex() if atr14 is not None else None,
            client_order_id=client_order_id,
        )
        order_id = f"buy-{next(self._seq)}"
        # Faithful Alpaca semantics: an accepted notional buy RESTS as an open
        # order until it fills out of band — the next cycle's pending-buy gate
        # must see it (R4 test relies on this). It is also immediately
        # observable as accepted (R9: the trace fake exercises the REAL
        # observation path, not observe_unavailable).
        if OrderObservation is not None:  # absent on the 97a65d6 baseline tree
            self.observations[order_id] = OrderObservation(
                order_id=order_id, status="accepted",
                observed_at="2026-08-26T14:30:01+00:00", filled_qty=None,
                filled_avg_price=None, filled_at=None, error=None)
        self.open_orders.append(OpenOrder(order_id=order_id, symbol=symbol,
                                          side="buy", order_type="market",
                                          qty=0.0, stop_price=None,
                                          status="accepted"))
        return OrderResult(symbol=symbol, side="buy", qty=0.0, status="accepted",
                           order_id=order_id)

    def submit_market_order(self, symbol, qty, side, *, client_order_id=None):
        self._record("market", symbol=symbol, qty=float(qty), side=side,
                     client_order_id=client_order_id)
        if side == "sell":
            self.positions.pop(symbol, None)
            self.open_orders = [o for o in self.open_orders if o.symbol != symbol]
        return OrderResult(symbol=symbol, side=side, qty=float(qty), status="filled",
                           order_id=f"mkt-{next(self._seq)}")

    def submit_stop_sell(self, symbol, qty, stop_price, *, client_order_id=None):
        self._record("stop", symbol=symbol, qty=float(qty), stop_price=float(stop_price),
                     stop_hex=float(stop_price).hex(), client_order_id=client_order_id)
        order = OpenOrder(order_id=f"stop-{next(self._seq)}", symbol=symbol, side="sell",
                          order_type="stop", qty=float(qty), stop_price=float(stop_price),
                          status="accepted")
        self.open_orders = [o for o in self.open_orders if o.symbol != symbol] + [order]
        return OrderResult(symbol=symbol, side="sell", qty=float(qty), status="accepted",
                           order_id=order.order_id)

    def cancel_order(self, order_id):
        self._record("cancel", order_id=order_id)
        self.open_orders = [o for o in self.open_orders if o.order_id != order_id]
        return True


class _NoNetBrokerReader:
    def __init__(self, _book_root):
        pass

    def fills(self):
        return None, "network disabled in tests"


def make_settings(watchlist=None, sectors=None) -> Settings:
    return Settings(
        alpaca_api_key=None, alpaca_secret_key=None,
        alpaca_base_url="https://paper-api.alpaca.markets",
        moonshot_api_key=None, analyst_provider="cli", analyst_cli_path=None,
        analyst_cli_timeout=180, analyst_cli_home=None, analyst_cli_model=None,
        analyst_model="kimi-k3",
        watchlist=list(watchlist or WATCHLIST),
        core_watchlist=list(watchlist or WATCHLIST),
        context_symbols=[], research_symbols=[], sectors=dict(sectors or {}), risk=RISK,
    )


def neutral_regime(_asof) -> MacroRegime:
    return MacroRegime(score=0.0, label="neutral")


def run_scenario(apply_patch, tmp_dir: Path, *, broker=None, conn=None) -> SimpleNamespace:
    """Two frozen-clock live cycles; returns the recorded trade trace.

    apply_patch(target_dotted_path, value) — monkeypatch.setattr in pytest, a
    manual shim in __main__ (where the same live-state redirects keep the real
    data/ files untouched while generating the fixture)."""
    frames_ = frames()
    signal_px = {s: last_close(frames_, s) for s in ("BUYA", "BUYB", "VETO", "CHASE")}
    live_prices = {
        "BUYA": signal_px["BUYA"],
        "BUYB": signal_px["BUYB"],
        "VETO": signal_px["VETO"],
        # +1.1% over the signal: under the 0.75-ATR gap cap but above the 1.0%
        # signal-relative chase cap, so the intent waits instead of executing.
        "CHASE": signal_px["CHASE"] * 1.011,
    }
    apply_patch("agentic_trading.run.datetime", FrozenDateTime)
    apply_patch("agentic_trading.run.fetch_last_price", lambda s: live_prices[s])
    apply_patch("agentic_trading.run.time.sleep", lambda _s: None)
    apply_patch("agentic_trading.heartbeat.HEARTBEAT_PATH", tmp_dir / "heartbeat.json")
    apply_patch("agentic_trading.live_events.LIVE_EVENTS_PATH", tmp_dir / "live_events.jsonl")
    apply_patch("agentic_trading.progress.PROGRESS_PATH", tmp_dir / "progress.json")
    apply_patch("agentic_trading.run.BookBrokerReader", _NoNetBrokerReader)

    if conn is None:
        conn = connect(":memory:")
    update_position_peak(conn, "SELLER", 100.0, FIXED_NOW.isoformat())
    update_position_peak(conn, "RATCHET", 125.0, FIXED_NOW.isoformat())

    if broker is None:
        ratchet_last = signal_px["RATCHET"] if "RATCHET" in signal_px else last_close(frames_, "RATCHET")
        broker = TraceBroker(positions={
            "SELLER": Position(symbol="SELLER", qty=5.0, avg_entry_price=100.0,
                               current_price=78.0, market_value=390.0),
            "RATCHET": Position(symbol="RATCHET", qty=10.0, avg_entry_price=100.0,
                                current_price=ratchet_last, market_value=10.0 * ratchet_last),
        })

    settings = make_settings(sectors={"BUYA": "Technology", "BUYB": "Technology", "VETO": "Technology"})
    feed = HarnessFeed(frames_)
    for _ in range(2):
        run_cycle(skip_llm=True, settings=settings, broker=broker, feed=feed, conn=conn,
                  regime_model=neutral_regime)
    return SimpleNamespace(
        events=broker.events, intents=load_trade_intents(conn),
        positions=broker.get_positions(), conn=conn, broker=broker,
    )


def public_event(event: dict) -> dict:
    """The fixture form of a trace event: None fields dropped, order kept."""
    return {k: v for k, v in event.items() if v is not None}


def test_baseline_trace_matches_the_recorded_fixture(monkeypatch, tmp_path):
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["meta"]["baseline_commit"] == BASELINE_COMMIT

    result = run_scenario(monkeypatch.setattr, tmp_path)
    observed = [public_event(e) for e in result.events]

    assert observed == fixture["expected_events"], (
        "trade-call trace drifted from the recorded 97a65d6 baseline — "
        "funnel work must not change the trading-call sequence"
    )

    exp = fixture["expectation"]
    buys = [e for e in observed if e["op"] == "buy"]
    assert [e["symbol"] for e in buys] == exp["buy_symbols"]
    assert all("notional_hex" in e and "atr14_hex" in e and "client_order_id" in e for e in buys)
    sells = [e for e in observed if e["op"] == "market" and e["side"] == "sell"]
    assert [e["symbol"] for e in sells] == exp["market_sells"]
    stops = [e for e in observed if e["op"] == "stop"]
    cancels = [e for e in observed if e["op"] == "cancel"]
    assert len(stops) == exp["stop_placements"] and len(cancels) == exp["stop_cancels"]
    assert len(stops) >= 2 and stops[0]["stop_price"] < stops[-1]["stop_price"], \
        "the RATCHET stop must ratchet UP, never down"
    assert sorted(i.symbol for i in result.intents) == exp["remaining_intents"]
    assert sorted(result.positions) == exp["final_positions"]


class _Patch:
    """monkeypatch-shaped shim for manual fixture regeneration in __main__."""

    def __init__(self):
        self._undo: list[tuple] = []

    def setattr(self, target: str, value) -> None:
        parts = target.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            try:
                module = importlib.import_module(".".join(parts[:cut]))
            except ModuleNotFoundError:
                continue
            obj = module
            for part in parts[cut:-1]:
                obj = getattr(obj, part)
            self._undo.append((obj, parts[-1], getattr(obj, parts[-1])))
            setattr(obj, parts[-1], value)
            return
        raise ImportError(target)

    def undo(self) -> None:
        for module, attr, old in reversed(self._undo):
            setattr(module, attr, old)
        self._undo.clear()


if __name__ == "__main__":
    import tempfile

    patcher = _Patch()
    with tempfile.TemporaryDirectory() as td:
        result = run_scenario(patcher.setattr, Path(td))
    patcher.undo()
    print(json.dumps([public_event(e) for e in result.events], indent=2))
