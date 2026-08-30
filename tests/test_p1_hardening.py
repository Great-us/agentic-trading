"""Tests for the P1 hardening layer:

- risk.yaml numeric validation
- credential-free subprocess environments
- cancel-on-404 semantics
- after-close exits queue instead of submitting doomed market orders
- forward-split detection re-bases the trailing-stop peak
"""
import logging
from dataclasses import replace

import pytest

from agentic_trading.config import validate_risk
from agentic_trading.execution.broker import Position, _is_not_found
from agentic_trading.journal.logger import (
    connect, load_position_peaks, load_position_state,
    record_position_state,
)
from agentic_trading.llm.cli_provider import sanitized_child_env
from agentic_trading.run import _detect_and_reset_splits

from tests.test_p0_hardening import (
    RISK, HarnessBroker, HarnessFeed, _settings, _upward_df,
)
from agentic_trading.signals.macro import MacroRegime

TS = "2026-08-23T15:00:00+00:00"


# --- risk.yaml validation ----------------------------------------------------

def test_valid_risk_config_passes():
    validate_risk(RISK)  # must not raise


@pytest.mark.parametrize("field,value", [
    ("risk_per_trade_pct", 2.0),        # the classic '2 meaning 2%' typo
    ("max_position_pct", 1.5),
    ("min_stop_pct", -0.05),
    ("trailing_stop_pct", 0.0),
])
def test_out_of_range_fractions_are_rejected(field, value):
    broken = replace(RISK, **{field: value})
    with pytest.raises(ValueError, match=field):
        validate_risk(broken)


def test_inconsistent_bounds_are_rejected():
    with pytest.raises(ValueError, match="min_position_pct"):
        validate_risk(replace(RISK, min_position_pct=RISK.max_position_pct + 0.01))
    with pytest.raises(ValueError, match="sell_threshold"):
        validate_risk(replace(RISK, sell_threshold=0.1))


def test_watchlist_symbols_are_normalized(tmp_path):
    # Covered through load_settings' normaliser indirectly; assert the rule
    # that downstream sees upper-case tickers by checking the loader output.
    from agentic_trading.config import load_settings
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "watchlist.yaml").write_text(
        "symbols:\n  - nvda\n  - ' mu '\ncontext_symbols:\n  - spy\n",
        encoding="utf-8",
    )
    (cfg_dir / "risk.yaml").write_text(
        "\n".join(f"{k}: {v}" for k, v in [
            ("risk_per_trade_pct", 0.015), ("max_position_pct", 0.18),
            ("min_position_pct", 0.04), ("max_total_exposure_pct", 0.95),
            ("max_open_positions", 6), ("max_new_orders_per_cycle", 5),
            ("min_quant_score_to_consider", 0.15), ("atr_stop_multiple", 2.5),
            ("min_stop_pct", 0.06), ("max_stop_pct", 0.20),
            ("trailing_stop_pct", 0.12), ("risk_off_size_multiplier", 0.5),
            ("risk_off_score_penalty", 0.15), ("escalation_cooldown_minutes", 60),
            ("escalation_cooldown_score_delta", 0.15),
        ]), encoding="utf-8",
    )
    settings = load_settings(cfg_dir, include_research=False)
    assert settings.watchlist == ["NVDA", "MU"]
    assert "spy" not in settings.context_symbols and "SPY" in settings.context_symbols


# --- child environment hygiene ------------------------------------------------

def test_child_env_strips_credentials(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "secret")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    monkeypatch.setenv("MOONSHOT_API_KEY", "secret")
    monkeypatch.setenv("TIINGO_API_KEY", "secret")
    monkeypatch.setenv("PATH", "irrelevant-but-needed")
    env = sanitized_child_env({"KIMI_CODE_HOME": "/tmp/x"})
    assert "ALPACA_API_KEY" not in env
    assert "ALPACA_SECRET_KEY" not in env
    assert "MOONSHOT_API_KEY" not in env
    assert "TIINGO_API_KEY" not in env
    assert env.get("PATH") == "irrelevant-but-needed"
    assert env.get("KIMI_CODE_HOME") == "/tmp/x"


# --- idempotency keys ---------------------------------------------------------

# (client_order_id() removed — OrderIdMinter in execution/broker.py owns this now)

# --- cancel semantics ---------------------------------------------------------

def test_cancel_treats_404_as_success():
    assert _is_not_found(Exception("404 Client Error: Not Found for url: https://x"))
    assert _is_not_found(Exception("order not found"))
    class WithStatus(Exception):
        status_code = 404
    assert _is_not_found(WithStatus("gone"))
    assert not _is_not_found(Exception("429 rate limited"))


# --- after-close exit queuing --------------------------------------------------

class ClosedMarketBroker(HarnessBroker):
    def is_market_open(self):
        return False


def test_forced_exit_after_close_queues_instead_of_submitting(monkeypatch):
    crashed = Position(symbol="OLD", qty=10.0, avg_entry_price=100.0,
                       current_price=80.0, market_value=800.0)
    feed = HarnessFeed({
        "AAA": _upward_df(),
        "OLD": _upward_df(days=80, start=100.0, daily=-0.01),
    })
    broker = ClosedMarketBroker(positions={"OLD": crashed})
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: 100.0)
    conn = connect(":memory:")
    try:
        from agentic_trading.run import run_cycle
        run_cycle(force_dry_run=False, skip_llm=False, fast_mode=False,
                  settings=_settings(), broker=broker, feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
        row = conn.execute(
            "SELECT action, order_status, reasoning FROM decisions "
            "WHERE symbol='OLD' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert broker.sells == [], "no market order may be submitted after the close"
    assert row is not None and row[0] == "sell"
    assert row[1] == "queued_closed"
    assert "Queued" in row[2]


def test_after_close_buys_become_intents_not_orders(monkeypatch):
    broker = ClosedMarketBroker()
    feed = HarnessFeed({"AAA": _upward_df()})
    monkeypatch.setattr("agentic_trading.run.fetch_last_price", lambda _s: None)
    conn = connect(":memory:")
    try:
        from agentic_trading.run import run_cycle
        run_cycle(force_dry_run=False, skip_llm=False, fast_mode=False,
                  settings=_settings(), broker=broker, feed=feed, conn=conn,
                  regime_model=lambda _asof: MacroRegime(score=0.0, label="neutral"))
        intents = conn.execute("SELECT symbol FROM trade_intents").fetchall()
        decisions = conn.execute(
            "SELECT action, order_status FROM decisions WHERE symbol='AAA'"
        ).fetchall()
    finally:
        conn.close()
    assert broker.buys == []
    assert intents == [("AAA",)]


# --- forward-split detection ---------------------------------------------------

def _position(symbol="X", qty=10.0, entry=90.0, price=101.0):
    return Position(symbol=symbol, qty=qty, avg_entry_price=entry,
                    current_price=price, market_value=qty * price)


def test_split_detection_resets_the_peak():
    conn = connect(":memory:")
    try:
        record_position_state(conn, "X", 5.0, 1000.0, TS)          # pre-split book
        from agentic_trading.journal.logger import update_position_peak
        update_position_peak(conn, "X", 200.0, TS)                 # pre-split peak
        peaks: dict[str, float] = {}
        _detect_and_reset_splits(
            conn, peaks, {"X": _position()}, logging.getLogger("t"), TS,
        )
        hwm = load_position_peaks(conn)["X"]
        state = load_position_state(conn)["X"]
    finally:
        conn.close()
    assert abs(hwm - 101.0) < 1e-9   # re-based to the post-split price...
    assert peaks["X"] == hwm
    assert state[0] == 10.0           # baseline updated for the next cycle


def test_a_real_crash_is_not_mistaken_for_a_split():
    conn = connect(":memory:")
    try:
        from agentic_trading.journal.logger import update_position_peak
        record_position_state(conn, "X", 5.0, 1000.0, TS)
        update_position_peak(conn, "X", 200.0, TS)
        crashed = _position(qty=10.0, price=50.0)   # qty doubled BUT value halved
        peaks: dict[str, float] = {}
        _detect_and_reset_splits(
            conn, peaks, {"X": crashed}, logging.getLogger("t"), TS,
        )
        hwm = load_position_peaks(conn)["X"]
    finally:
        conn.close()
    assert hwm == 200.0   # unchanged: this looks like a crash, keep the peak


# --- verdict parameter-leak salvage -------------------------------------------

def test_parse_verdict_salvages_leaked_parameter_block():
    # Observed live from claude -p: the rationale carried an antml-style
    # parameter block and the real risk_flags array was stranded inside it.
    from agentic_trading.llm.schema import parse_verdict
    payload = {
        "stance": "bullish",
        "confidence": 0.55,
        "rationale": (
            "Price is above both SMA20 and SMA50, indicating an uptrend with "
            "no bearish crossover.</rationale>\n"
            '<parameter name="risk_flags">'
            '["No news coverage", "No fundamental data available"]'
        ),
        "risk_flags": [],
        "evidence_quality": "low",
    }
    verdict = parse_verdict(payload, "TEST")
    assert verdict is not None
    assert "parameter" not in verdict.rationale
    assert "</rationale>" not in verdict.rationale
    assert verdict.rationale.endswith("no bearish crossover.")
    assert sorted(verdict.risk_flags) == ["No fundamental data available", "No news coverage"]


def test_parse_verdict_cleans_trailing_tag_without_flags():
    from agentic_trading.llm.schema import parse_verdict
    payload = {
        "stance": "neutral",
        "confidence": 0.4,
        "rationale": "Flat tape, nothing decisive.</rationale>",
        "risk_flags": ["thin"],
        "evidence_quality": "low",
    }
    verdict = parse_verdict(payload, "TEST")
    assert verdict.rationale == "Flat tape, nothing decisive."
    assert verdict.risk_flags == ["thin"]
