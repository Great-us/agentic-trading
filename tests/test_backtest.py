from dataclasses import replace

import numpy as np
import pandas as pd

from agentic_trading.backtest.engine import run_backtest
from agentic_trading.config import Settings
from agentic_trading.data.feed import HistoricalFeed
from agentic_trading.data.market_data import slice_asof
from agentic_trading.signals.technical import compute_signal
from tests.test_risk import RISK


def _settings(symbols, **risk_over):
    risk = replace(RISK, **risk_over) if risk_over else RISK
    return Settings(
        alpaca_api_key=None, alpaca_secret_key=None,
        alpaca_base_url="https://paper-api.alpaca.markets",
        moonshot_api_key=None, analyst_provider="cli", analyst_cli_path=None,
        analyst_cli_timeout=30, analyst_cli_home=None, analyst_cli_model=None,
        analyst_model="kimi-k3",
        watchlist=list(symbols), core_watchlist=list(symbols),
        research_symbols=[], risk=risk,
    )


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
    bars = {"WEAK": weak, "STRONG": strong}
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
        bars={"AAA": df}, initial_cash=100_000, slippage_bps=0, stop_slippage_bps=0,
        quiet=True,
    )
    sells = [f for f in result.fills if f.side == "sell"]
    assert any(f.reason in ("stop", "gap_stop") for f in sells), (
        f"expected a stop fill on the recovery bar; fills={result.fills}"
    )
