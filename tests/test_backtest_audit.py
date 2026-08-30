import numpy as np
import pandas as pd

from agentic_trading.backtest.benchmarks import allocation_benchmark, daily_cash_returns_from_yield
from agentic_trading.backtest.engine import run_backtest
from agentic_trading.backtest.metrics import yearly_returns
from agentic_trading.backtest.precompute import CachedCorrelationModel, CachedRegimeModel, CachedSignalModel
from agentic_trading.data.market_data import average_dollar_volume
from agentic_trading.risk.manager import average_corr_to_holdings
from agentic_trading.backtest.statistics import alpha_beta, block_bootstrap_comparison
from agentic_trading.backtest.trades import build_trade_records
from agentic_trading.backtest.universe import UniverseSchedule
from agentic_trading.backtest.audit import _build_specs
from agentic_trading.execution.sim_broker import Fill
from agentic_trading.signals.technical import compute_signal
from tests.test_backtest import _settings, _trend, _with_macro


def test_yearly_returns_include_each_first_session_and_chain():
    equity = pd.Series(
        [110.0, 121.0, 133.1],
        index=pd.to_datetime(["2020-12-31", "2021-01-04", "2021-12-31"]),
    )
    years = yearly_returns(equity, starting_equity=100.0)
    assert abs(years[2020] - 0.10) < 1e-12
    assert abs(years[2021] - 0.21) < 1e-12
    assert abs(np.prod([1 + value for value in years.values()]) - 1.331) < 1e-12


def test_backtest_never_processes_a_session_after_end():
    _, strong = _trend("AAA", n=100, step=0.8)
    bars = _with_macro({"AAA": strong})
    end = strong.index[-2]
    result = run_backtest(
        strong.index[60].date().isoformat(),
        end.date().isoformat(),
        settings=_settings(["AAA"]),
        bars=bars,
        slippage_bps=0,
    )
    assert result.equity.index[-1] == end
    assert all(pd.Timestamp(fill.date) <= end for fill in result.fills)


def test_cash_return_accrues_only_between_sessions():
    _, flat = _trend("AAA", n=70, step=0.0)
    bars = _with_macro({"AAA": flat})
    start, end = flat.index[60], flat.index[-1]
    dates = pd.bdate_range(start, end)
    cash_returns = pd.Series(0.001, index=dates)
    result = run_backtest(
        start.date().isoformat(), end.date().isoformat(),
        settings=_settings(["AAA"]), bars=bars, cash_returns=cash_returns,
        slippage_bps=0,
    )
    expected = 100_000 * (1.001 ** (len(dates) - 1))
    assert abs(result.equity.iloc[-1] - expected) < 1e-6
    assert result.exposure.max() == 0


def test_dynamic_universe_blocks_early_entry_but_keeps_removed_holding_managed():
    _, strong = _trend("AAA", n=100, step=0.8)
    strong.loc[strong.index[90], "Low"] = 1.0
    bars = _with_macro({"AAA": strong})
    schedule = UniverseSchedule.fixed(
        ["AAA"], start_date=strong.index[65].date(), end_date=strong.index[80].date(),
    )
    result = run_backtest(
        strong.index[60].date().isoformat(), strong.index[-1].date().isoformat(),
        settings=_settings(["AAA"]), bars=bars, universe_schedule=schedule,
        slippage_bps=0, stop_slippage_bps=0,
    )
    buys = [fill for fill in result.fills if fill.side == "buy"]
    sells = [fill for fill in result.fills if fill.side == "sell"]
    assert buys and buys[0].date > strong.index[65]
    assert sells and sells[-1].date == strong.index[90]


def test_dynamic_benchmark_uses_prior_day_exposure():
    dates = pd.bdate_range("2024-01-02", periods=3)
    prices = pd.Series([100.0, 110.0, 121.0], index=dates)
    cash = pd.Series(0.0, index=dates)
    exposure = pd.Series([0.0, 1.0, 1.0], index=dates)
    equity = allocation_benchmark(prices, cash, dates, risky_weight=exposure, starting_cash=100.0)
    assert equity.iloc[1] == 100.0
    assert abs(equity.iloc[2] - 110.0) < 1e-12


def test_irx_conversion_is_positive_and_aligned():
    dates = pd.bdate_range("2024-01-02", periods=3)
    yields = pd.Series([5.0], index=[dates[0]])
    returns = daily_cash_returns_from_yield(yields, dates)
    assert list(returns.index) == list(dates)
    assert (returns > 0).all()


def test_trade_records_include_closed_and_open_episodes():
    idx = pd.bdate_range("2024-01-02", periods=5)
    bars = {
        "AAA": pd.DataFrame({"Open": [100, 105, 110, 108, 112], "High": [101, 108, 113, 110, 115],
                             "Low": [99, 103, 107, 106, 110], "Close": [100, 106, 111, 109, 114]}, index=idx),
        "BBB": pd.DataFrame({"Open": [50] * 5, "High": [52] * 5, "Low": [48] * 5, "Close": [50] * 5}, index=idx),
    }
    fills = [
        Fill(idx[0], "AAA", "buy", 10, 100, "signal", 1000, atr14=4),
        Fill(idx[3], "AAA", "sell", 10, 108, "signal", 1080),
        Fill(idx[1], "BBB", "buy", 20, 50, "signal", 1000, atr14=2),
    ]
    records = build_trade_records(fills, bars, idx[-1])
    assert len(records) == 2
    aaa = next(record for record in records if record.symbol == "AAA")
    bbb = next(record for record in records if record.symbol == "BBB")
    assert aaa.pnl == 80
    assert aaa.r_multiple is not None
    assert abs(aaa.mfe - 0.13) < 1e-12
    assert not aaa.is_open
    assert bbb.is_open


def test_cached_signal_is_exact_on_causal_slices():
    _, frame = _trend("AAA", n=240, step=0.4)
    cached = CachedSignalModel({"AAA": frame})
    for size in (55, 100, 240):
        direct = compute_signal("AAA", frame.iloc[:size])
        fast = cached("AAA", frame.iloc[:size])
        assert direct is not None and fast is not None
        assert abs(direct.score - fast.score) < 1e-12
        assert abs(direct.atr14 - fast.atr14) < 1e-12
        assert abs(cached.average_dollar_volume("AAA", frame.iloc[:size]) - average_dollar_volume(frame.iloc[:size])) < 1e-9


def test_cached_correlation_matches_causal_direct_calculation():
    _, first = _trend("AAA", n=180, step=0.4)
    _, second = _trend("BBB", n=180, step=0.25)
    second.loc[second.index[::17], "Close"] *= 0.99
    model = CachedCorrelationModel({"AAA": first, "BBB": second})
    for size in (60, 100, 180):
        direct = average_corr_to_holdings(
            first.iloc[:size]["Close"], {"BBB": second.iloc[:size]["Close"]}, window=60,
        )
        cached = model("AAA", ["BBB"], first.index[size - 1], 60)
        assert direct is not None and cached is not None
        assert abs(direct - cached) < 1e-12


def test_cached_regime_reports_first_full_date():
    _, frame = _trend("AAA", n=100, step=0.2)
    bars = _with_macro({"AAA": frame})
    model = CachedRegimeModel(bars)
    assert model.first_full_date() == frame.index[59]
    assert model(frame.index[-1]).label in {"risk_on", "neutral", "risk_off"}


def test_universe_csv_requires_the_documented_schema(tmp_path):
    path = tmp_path / "membership.csv"
    pd.DataFrame({
        "symbol": ["AAA"], "start_date": ["2020-01-01"], "end_date": [None],
        "sector": ["Technology"], "source_id": ["test"],
    }).to_csv(path, index=False)
    schedule = UniverseSchedule.from_csv(path)
    assert schedule.active_on("2020-01-01") == ["AAA"]
    assert schedule.sectors["AAA"] == "Technology"


def test_alpha_and_bootstrap_are_deterministic():
    idx = pd.bdate_range("2020-01-02", periods=260)
    benchmark = pd.Series(100 * np.cumprod(np.full(len(idx), 1.0002)), index=idx)
    strategy = pd.Series(100 * np.cumprod(np.full(len(idx), 1.0004)), index=idx)
    alpha = alpha_beta(strategy, benchmark)
    first = block_bootstrap_comparison(strategy, benchmark, samples=100, seed=7)
    second = block_bootstrap_comparison(strategy, benchmark, samples=100, seed=7)
    assert alpha["alpha_annual"] > 0
    assert first == second


def test_delisting_return_forces_a_terminal_fill():
    idx = pd.bdate_range("2024-01-02", periods=3)
    frame = pd.DataFrame({
        "Open": [100.0, 101.0, 0.0],
        "High": [101.0, 102.0, 0.0],
        "Low": [99.0, 100.0, 0.0],
        "Close": [100.0, 101.0, 0.0],
        "Volume": [1_000_000] * 3,
        "DelistingReturn": [np.nan, np.nan, -0.5],
    }, index=idx)
    from agentic_trading.execution.sim_broker import SimulatedBroker

    broker = SimulatedBroker(starting_cash=1000, slippage_bps=0, bars={"AAA": frame})
    broker.current_date = idx[0]
    broker.submit_notional_buy("AAA", 1000, atr14=2)
    broker.process_bar(idx[1])
    broker.process_bar(idx[2])
    sell = next(fill for fill in broker.fills if fill.side == "sell")
    assert sell.reason == "delisting"
    assert sell.price == 50.5


def test_full_audit_matrix_contains_cost_ablation_and_leave_one_out():
    config = {
        "start": "2007-04-11", "end": "2026-08-21",
        "current_universe": ["AAA", "BBB"], "stress_additions": ["CCC"],
        "cost_scenarios": {
            "base": {"slippage_bps": 5, "stop_slippage_bps": 10},
            "medium": {"slippage_bps": 15, "stop_slippage_bps": 30},
            "severe": {"slippage_bps": 30, "stop_slippage_bps": 60},
        },
    }
    names = {spec.name for spec in _build_specs(config, "2007-07-01")}
    assert {"stress_full", "cost_severe", "ablate_no_macro", "baseline_simple_trend"} <= names
    assert {"leave_out_AAA", "leave_out_BBB"} <= names
