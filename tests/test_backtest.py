from dataclasses import replace

import numpy as np
import pandas as pd

from agentic_trading.backtest.engine import run_backtest
from agentic_trading.config import Settings, load_settings
from agentic_trading.data.feed import HistoricalFeed
from agentic_trading.data.market_data import slice_asof
from agentic_trading.execution.broker import Position
from agentic_trading.execution.sim_broker import SimulatedBroker
from agentic_trading.journal.logger import connect
from agentic_trading.run import _fill_holding_atrs, run_cycle
from agentic_trading.signals.macro import MacroRegime
from agentic_trading.signals.technical import compute_signal
from tests.test_risk import RISK


def _settings(symbols, **risk_over):
    risk = replace(RISK, **risk_over) if risk_over else RISK
    return Settings(
        alpaca_api_key=None, alpaca_secret_key=None,
        alpaca_base_url="https://paper-api.alpaca.markets",
        moonshot_api_key=None, analyst_provider="cli", analyst_cli_path=None,
        analyst_cli_timeout=30, analyst_cli_home=None, analyst_cli_model=None,
        analyst_cli_extra_args=None,
        analyst_model="kimi-k3",
        watchlist=list(symbols), core_watchlist=list(symbols),
        research_symbols=[], risk=risk,
    )


def _with_macro(bars: dict) -> dict:
    """Give the regime layer enough history so tests don't fail-closed as UNKNOWN."""
    idx = next(iter(bars.values())).index
    n = len(idx)
    close = 100.0 + np.arange(n) * 0.02
    dummy = pd.DataFrame(
        {"Open": close - 0.1, "High": close + 0.5, "Low": close - 0.5,
         "Close": close, "Volume": 1e7},
        index=idx,
    )
    vix = dummy.copy()
    vix["Close"] = 15.0
    out = dict(bars)
    for ticker in ("RSP", "SPY", "QQQ", "HYG", "LQD", "IWM", "TLT", "XLY", "XLP"):
        out.setdefault(ticker, dummy.copy())
    out.setdefault("^VIX", vix)
    return out


def _trend(symbol, n=90, start_px=100.0, step=0.4, start="2019-01-02", volume=5_000_000):
    idx = pd.bdate_range(start, periods=n)
    close = start_px + np.arange(n) * step
    high = close + 0.5
    low = close - 0.5
    open_ = close - 0.1
    return symbol, pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=idx,
    )


def test_asof_slice_does_not_see_the_next_close():
    idx = pd.bdate_range("2019-01-02", periods=80)
    close = np.concatenate([np.full(79, 100.0), [250.0]])
    df = pd.DataFrame({
        "Open": close, "High": close + 1, "Low": close - 1,
        "Close": close, "Volume": 5_000_000,
    }, index=idx)
    feed = HistoricalFeed({"SPIKE": df})
    before = compute_signal("SPIKE", feed.price_history("SPIKE", asof=idx[-2]))
    after = compute_signal("SPIKE", feed.price_history("SPIKE", asof=idx[-1]))
    full = compute_signal("SPIKE", df)
    assert before is not None and after is not None and full is not None
    assert abs(before.score) < 0.05
    assert after.score == full.score
    assert after.score > before.score
    assert 250.0 not in set(slice_asof(df, idx[-2])["Close"])


def test_ranked_buys_prefer_the_higher_score_when_the_order_cap_is_one():
    # YAML order is WEAK then STRONG; with max_new_orders_per_cycle=1 the
    # stronger score must consume the slot, not the list order.
    _, weak = _trend("WEAK", step=0.15)
    _, strong = _trend("STRONG", step=0.8)
    bars = _with_macro({"WEAK": weak, "STRONG": strong})
    start = weak.index[60].date().isoformat()
    end = weak.index[-1].date().isoformat()
    result = run_backtest(
        start, end,
        settings=_settings(["WEAK", "STRONG"], max_new_orders_per_cycle=1, max_open_positions=1),
        bars=bars, initial_cash=100_000, slippage_bps=0, quiet=True,
    )
    buys = [f for f in result.fills if f.side == "buy"]
    assert buys, "expected at least one fill"
    assert buys[0].symbol == "STRONG"


def test_replay_stop_catches_an_intraday_tag_that_closes_green():
    n = 80
    idx = pd.bdate_range("2019-01-02", periods=n)
    close = 100 + np.arange(n) * 0.5
    open_ = close - 0.1
    high = close + 0.5
    low = close - 0.5
    # Last session: opens at the trend, dumps 40%, closes back near the open.
    open_[-1] = close[-2]
    high[-1] = close[-2] + 0.5
    low[-1] = close[-2] * 0.55
    close[-1] = close[-2] + 0.2
    df = pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": 5_000_000},
        index=idx,
    )
    result = run_backtest(
        idx[60].date().isoformat(), idx[-1].date().isoformat(),
        settings=_settings(["AAA"], max_new_orders_per_cycle=1),
        bars=_with_macro({"AAA": df}), initial_cash=100_000, slippage_bps=0, stop_slippage_bps=0,
        quiet=True,
    )
    sells = [f for f in result.fills if f.side == "sell"]
    assert any(f.reason in ("stop", "gap_stop") for f in sells), (
        f"expected a stop fill on the recovery bar; fills={result.fills}"
    )


def test_extra_symbols_can_produce_fills():
    _, flat = _trend("AAA", n=90, step=0.0)
    _, spy = _trend("SPY", n=90, step=0.8)
    bars = _with_macro({"AAA": flat, "SPY": spy})
    start = flat.index[60].date().isoformat()
    end = flat.index[-1].date().isoformat()
    result = run_backtest(
        start, end,
        settings=_settings(["AAA"]),
        extra_symbols=["SPY"],
        bars=bars, initial_cash=100_000, slippage_bps=0, quiet=True,
    )
    buys = [f.symbol for f in result.fills if f.side == "buy"]
    assert "SPY" in buys
    assert "AAA" not in buys


def test_same_cycle_clone_gets_correlation_haircut():
    _, mega1 = _trend("MEGA1", n=90, step=0.8)
    mega2 = mega1.copy()
    bars = _with_macro({"MEGA1": mega1, "MEGA2": mega2})
    start = mega1.index[60].date().isoformat()
    end = mega1.index[-1].date().isoformat()
    result = run_backtest(
        start, end,
        settings=_settings(
            ["MEGA1", "MEGA2"],
            corr_penalty_threshold=0.50,
            corr_size_multiplier=0.5,
            max_new_orders_per_cycle=5,
            max_open_positions=8,
        ),
        bars=bars, initial_cash=100_000, slippage_bps=0, quiet=True,
    )
    buys = [f for f in result.fills if f.side == "buy"]
    assert len(buys) >= 2
    first, second = buys[0], buys[1]
    assert first.symbol == "MEGA1"
    assert second.symbol == "MEGA2"
    assert second.notional < first.notional * 0.6


def test_load_settings_reads_sector_and_regime_caps():
    settings = load_settings(include_research=False)
    assert settings.sectors["NVDA"] == "Technology"
    assert settings.sectors["META"] == "Communication Services"
    assert settings.risk.regime_max_exposure["risk_off"] == 0.55
    assert "risk_off" in settings.risk.trim_regimes


def test_risk_off_trims_the_weakest_when_fully_invested(monkeypatch):
    monkeypatch.setattr(
        "agentic_trading.run.assess_regime",
        lambda **_k: MacroRegime(score=-0.7, label="risk_off", notes=["test"]),
    )
    _, strong = _trend("STRONG", n=90, step=0.8)
    _, weak = _trend("WEAK", n=90, step=0.2)
    bars = _with_macro({"STRONG": strong, "WEAK": weak})
    asof = strong.index[-1]
    px_s, px_w = float(strong["Close"].iloc[-1]), float(weak["Close"].iloc[-1])
    broker = SimulatedBroker(starting_cash=100_000, slippage_bps=0, bars=bars)
    broker.current_date = asof
    broker.cash = 0.0
    broker.positions = {
        "STRONG": Position("STRONG", 50_000 / px_s, px_s, px_s, 50_000),
        "WEAK": Position("WEAK", 50_000 / px_w, px_w, px_w, 50_000),
    }
    conn = connect(":memory:")
    try:
        run_cycle(
            skip_llm=True, settings=_settings(["STRONG", "WEAK"]),
            broker=broker, feed=HistoricalFeed(bars), conn=conn, asof=asof,
        )
        # Depth+dwell buffers: one deep risk_off reading only arms the sell
        # counter — nothing is sold until the condition persists.
        assert not any(p.symbol == "WEAK" for p in broker.pending_sells)
        run_cycle(
            skip_llm=True, settings=_settings(["STRONG", "WEAK"]),
            broker=broker, feed=HistoricalFeed(bars), conn=conn, asof=asof,
        )
    finally:
        conn.close()
    assert any(p.symbol == "WEAK" for p in broker.pending_sells)


def test_fast_tier_does_not_trim(monkeypatch):
    monkeypatch.setattr(
        "agentic_trading.run.assess_regime",
        lambda **_k: MacroRegime(score=-0.7, label="risk_off", notes=["test"]),
    )
    _, strong = _trend("STRONG", n=90, step=0.8)
    bars = _with_macro({"STRONG": strong})
    asof = strong.index[-1]
    px = float(strong["Close"].iloc[-1])
    broker = SimulatedBroker(starting_cash=100_000, slippage_bps=0, bars=bars)
    broker.current_date = asof
    broker.cash = 0.0
    broker.positions = {
        "STRONG": Position("STRONG", 100_000 / px, px, px, 100_000),
    }
    run_cycle(
        skip_llm=True, fast_mode=True, settings=_settings(["STRONG"]),
        broker=broker, feed=HistoricalFeed(bars), conn=connect(":memory:"), asof=asof,
    )
    assert broker.pending_sells == []


def test_neutral_does_not_trim_a_full_book(monkeypatch):
    monkeypatch.setattr(
        "agentic_trading.run.assess_regime",
        lambda **_k: MacroRegime(score=0.1, label="neutral", notes=["test"]),
    )
    _, held = _trend("HELD", n=90, step=0.5)
    bars = _with_macro({"HELD": held})
    asof = held.index[-1]
    px = float(held["Close"].iloc[-1])
    broker = SimulatedBroker(starting_cash=100_000, slippage_bps=0, bars=bars)
    broker.current_date = asof
    broker.cash = 0.0
    broker.positions = {
        "HELD": Position("HELD", 100_000 / px, px, px, 100_000),
    }
    run_cycle(
        skip_llm=True, settings=_settings(["HELD"]),
        broker=broker, feed=HistoricalFeed(bars), conn=connect(":memory:"), asof=asof,
    )
    assert broker.pending_sells == []


def test_neutral_exposure_cap_blocks_a_new_buy(monkeypatch):
    monkeypatch.setattr(
        "agentic_trading.run.assess_regime",
        lambda **_k: MacroRegime(score=0.1, label="neutral", notes=["test"]),
    )
    _, held = _trend("HELD", n=90, step=0.4)
    _, new = _trend("NEW", n=90, step=0.9)
    bars = _with_macro({"HELD": held, "NEW": new})
    asof = held.index[-1]
    px = float(held["Close"].iloc[-1])
    broker = SimulatedBroker(starting_cash=100_000, slippage_bps=0, bars=bars)
    broker.current_date = asof
    broker.cash = 15_000.0
    broker.positions = {
        "HELD": Position("HELD", 85_000 / px, px, px, 85_000),
    }
    run_cycle(
        skip_llm=True, settings=_settings(["HELD", "NEW"]),
        broker=broker, feed=HistoricalFeed(bars), conn=connect(":memory:"), asof=asof,
    )
    assert broker.pending_sells == []
    assert not any(p.symbol == "NEW" for p in broker.pending_buys)


def test_sector_cap_blocks_a_third_tech_name(monkeypatch):
    monkeypatch.setattr(
        "agentic_trading.run.assess_regime",
        lambda **_k: MacroRegime(score=0.4, label="risk_on", notes=["test"]),
    )
    _, aapl = _trend("AAPL", n=90, step=0.5)
    _, msft = _trend("MSFT", n=90, step=0.5)
    _, nvda = _trend("NVDA", n=90, step=0.9)
    bars = _with_macro({"AAPL": aapl, "MSFT": msft, "NVDA": nvda})
    asof = aapl.index[-1]
    px_a, px_m = float(aapl["Close"].iloc[-1]), float(msft["Close"].iloc[-1])
    broker = SimulatedBroker(starting_cash=100_000, slippage_bps=0, bars=bars)
    broker.current_date = asof
    broker.cash = 64_000.0
    broker.positions = {
        "AAPL": Position("AAPL", 18_000 / px_a, px_a, px_a, 18_000),
        "MSFT": Position("MSFT", 18_000 / px_m, px_m, px_m, 18_000),
    }
    settings = _settings(["AAPL", "MSFT", "NVDA"])
    settings.sectors = {"AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology"}
    run_cycle(
        skip_llm=True, settings=settings,
        broker=broker, feed=HistoricalFeed(bars), conn=connect(":memory:"), asof=asof,
    )
    assert not any(p.symbol == "NVDA" for p in broker.pending_buys)


def test_off_watchlist_holding_gets_an_atr():
    _, df = _trend("ACET", n=90, step=0.2)
    feed = HistoricalFeed({"ACET": df})
    positions = {
        "ACET": Position("ACET", qty=10, avg_entry_price=100.0, current_price=100.0, market_value=1_000),
    }
    atrs: dict[str, float] = {}
    _fill_holding_atrs(positions, atrs, feed, df.index[-1], None)
    assert "ACET" in atrs
    assert atrs["ACET"] > 0
