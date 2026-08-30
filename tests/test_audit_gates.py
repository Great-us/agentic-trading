"""Tests for the P2 research-process guards."""
import inspect

import pandas as pd

import agentic_trading.backtest.audit as audit
from agentic_trading.execution.sim_broker import SimulatedBroker


class TestUniverseDrift:
    def _audit(self, monkeypatch, tmp_path, watchlist_yaml):
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "watchlist.yaml").write_text(watchlist_yaml, encoding="utf-8")
        monkeypatch.setattr(audit, "ROOT", tmp_path)
        return audit

    def test_flags_both_directions_of_drift(self, monkeypatch, tmp_path):
        audit_mod = self._audit(monkeypatch, tmp_path,
                                "symbols:\n  - MU\n  - NVDA\ncontext_symbols:\n  - SPY\n")
        drift = audit_mod._universe_drift({"current_universe": ["NVDA", "XOM"]})
        assert drift["in_watchlist_not_audited"] == ["MU"]
        assert drift["in_audit_not_watchlist"] == ["XOM"]

    def test_context_symbols_are_never_expected_in_the_audit(self, monkeypatch, tmp_path):
        audit_mod = self._audit(monkeypatch, tmp_path,
                                "symbols:\n  - NVDA\ncontext_symbols:\n  - SPY\n  - QQQ\n")
        drift = audit_mod._universe_drift({"current_universe": ["NVDA"]})
        assert drift == {"in_watchlist_not_audited": [], "in_audit_not_watchlist": []}


def _broker(rows):
    idx = pd.to_datetime([r[0] for r in rows])
    df = pd.DataFrame(
        {"Open": [r[1] for r in rows], "High": [r[2] for r in rows],
         "Low": [r[3] for r in rows], "Close": [r[4] for r in rows],
         "Volume": [1_000_000] * len(rows)},
        index=idx,
    )
    return SimulatedBroker(starting_cash=10_000, slippage_bps=0, bars={"AAA": df})


class TestGapVetoParity:
    def test_vetoed_entry_refunds_cash_and_is_recorded(self):
        broker = _broker([
            ("2020-01-02", 100, 101, 99, 100),
            ("2020-01-03", 115, 116, 114, 115),   # open is +7.5 ATR past close
        ])
        broker.process_bar(pd.Timestamp("2020-01-02"))
        broker.submit_notional_buy("AAA", 1_000, atr14=2.0)
        broker.process_bar(pd.Timestamp("2020-01-03"))
        assert "AAA" not in broker.positions
        assert broker.cash == 10_000
        assert broker.gap_vetoed_buys[0][1] == "AAA"

    def test_engine_wires_the_live_threshold(self):
        # The veto cap must come from risk.yaml, not a stale broker default.
        from agentic_trading.backtest.engine import run_backtest

        src = inspect.getsource(run_backtest)
        assert "max_entry_gap_atr=settings.risk.max_entry_gap_atr" in src
