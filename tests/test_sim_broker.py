import pandas as pd

from agentic_trading.execution.sim_broker import SimulatedBroker


def _bars(symbol, rows: list[tuple[str, float, float, float, float]]) -> dict:
    idx = pd.to_datetime([r[0] for r in rows])
    df = pd.DataFrame(
        {"Open": [r[1] for r in rows], "High": [r[2] for r in rows],
         "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
         "Volume": [1_000_000] * len(rows)},
        index=idx,
    )
    return {symbol: df}


def test_buy_fills_next_session_open_with_slippage():
    bars = _bars("AAA", [
        ("2020-01-02", 100, 101, 99, 100),
        ("2020-01-03", 110, 112, 109, 111),
    ])
    broker = SimulatedBroker(starting_cash=10_000, slippage_bps=0, bars=bars)
    broker.process_bar(pd.Timestamp("2020-01-02"))
    result = broker.submit_notional_buy("AAA", 1_000, atr14=2.0)
    assert result is not None
    assert "AAA" not in broker.positions
    broker.process_bar(pd.Timestamp("2020-01-03"))
    pos = broker.positions["AAA"]
    assert pos.avg_entry_price == 110
    assert abs(pos.qty * 110 - 1_000) < 1e-6


def test_stop_fires_on_intraday_low_even_if_close_recovers():
    # Classic lookahead trap: a day that tags the stop and closes green.
    bars = _bars("AAA", [
        ("2020-01-02", 100, 101, 99, 100),
        ("2020-01-03", 100, 101, 50, 100),  # low 50, close unchanged
    ])
    broker = SimulatedBroker(starting_cash=10_000, slippage_bps=0, stop_slippage_bps=0, bars=bars)
    broker.process_bar(pd.Timestamp("2020-01-02"))
    broker.submit_notional_buy("AAA", 1_000, atr14=2.0)
    broker.process_bar(pd.Timestamp("2020-01-03"))
    assert "AAA" not in broker.positions
    sell = [f for f in broker.fills if f.side == "sell"]
    assert len(sell) == 1
    # Open was 100, stop is below that, so fill at the stop (not a gap).
    assert sell[0].reason == "stop"
    assert sell[0].price < 100


def test_gapped_stop_fills_at_the_open_not_the_stop_price():
    bars = _bars("AAA", [
        ("2020-01-02", 100, 101, 99, 100),
        ("2020-01-03", 100, 101, 99, 100),  # fill day; stop arms ~6% below 100
        ("2020-01-06", 70, 72, 68, 71),     # opens through the stop
    ])
    broker = SimulatedBroker(starting_cash=10_000, slippage_bps=0, stop_slippage_bps=0, bars=bars)
    broker.process_bar(pd.Timestamp("2020-01-02"))
    broker.submit_notional_buy("AAA", 1_000, atr14=2.0)
    broker.process_bar(pd.Timestamp("2020-01-03"))
    assert "AAA" in broker.positions
    broker.process_bar(pd.Timestamp("2020-01-06"))
    sell = [f for f in broker.fills if f.side == "sell"][0]
    assert sell.reason == "gap_stop"
    assert sell.price == 70


def test_pending_buy_counts_toward_equity():
    bars = _bars("AAA", [
        ("2020-01-02", 100, 101, 99, 100),
        ("2020-01-03", 100, 101, 99, 100),
    ])
    broker = SimulatedBroker(starting_cash=10_000, slippage_bps=0, bars=bars)
    broker.process_bar(pd.Timestamp("2020-01-02"))
    before = broker.equity()
    broker.submit_notional_buy("AAA", 2_000, atr14=2.0)
    assert broker.equity() == before
    assert broker.cash == 8_000
