"""Wiring tests: overfit.py consumed by experiments.py and audit.py.

The corrections were written as a standalone CLI; these tests pin the two
integration points — the parameter-surface report and the audit payload —
so the module cannot silently regress back to "prototype nothing calls".
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from agentic_trading.backtest.audit import _format_overfitting, overfit_block
from agentic_trading.backtest.experiments import (
    SurfaceRow, format_surface, surface_overfit_report,
)
from agentic_trading.backtest.metrics import Metrics


def _metrics(sharpe: float) -> Metrics:
    return Metrics(
        start_equity=100_000.0, end_equity=120_000.0, cagr=0.20,
        volatility=0.15, sharpe=sharpe, max_drawdown=-0.10, calmar=2.0,
        n_buys=10, n_sells=8, win_rate=0.6, profit_factor=1.5,
        avg_exposure=0.8, years={},
    )


def _equity(drift: float, *, n: int = 300, seed: int = 1,
            start: str = "2022-01-03") -> pd.Series:
    rng = np.random.default_rng(seed)
    steps = drift + rng.normal(scale=0.01, size=n)
    return pd.Series(
        100_000.0 * np.cumprod(1.0 + steps),
        index=pd.bdate_range(start, periods=n),
    )


def _rows(n_configs: int = 3, with_equity: bool = True) -> list[SurfaceRow]:
    rows = []
    for i in range(n_configs):
        equity = _equity(0.0005 * (i + 1), seed=i + 1) if with_equity else None
        rows.append(SurfaceRow(
            overrides={"buy_threshold": 0.20 + 0.05 * i, "atr_stop_multiple": 2.5},
            metrics=_metrics(sharpe=1.0 + 0.1 * i),
            equity=equity,
        ))
    # parameter_surface sorts best-first; mirror that.
    rows.sort(key=lambda r: r.metrics.sharpe, reverse=True)
    return rows


def _ascii(text: str) -> bool:
    return all(ord(ch) < 128 for ch in text)


# --- experiments.surface_overfit_report --------------------------------------

def test_surface_report_is_empty_without_equities():
    assert surface_overfit_report(_rows(with_equity=False)) == ""
    assert surface_overfit_report([]) == ""


def test_surface_report_covers_pbo_dsr_and_min_length():
    text = surface_overfit_report(_rows(4))
    assert "Multiple-testing correction across the 4 grid cells" in text
    assert "PBO (CSCV, 8 splits)" in text
    assert "Deflated Sharpe" in text
    assert "4 trials" in text
    assert "Min backtest length" in text
    assert _ascii(text)


def test_format_surface_appends_the_report_and_keeps_the_disclaimer():
    text = format_surface(_rows(3))
    assert "Multiple-testing correction across the 3 grid cells" in text
    # The pre-existing trailing line must survive the new block.
    assert text.rstrip().endswith(
        "This is in-sample ranking, not a licence to promote the winner to live."
    )
    assert _ascii(text)


def test_format_surface_unchanged_when_equities_are_absent():
    text = format_surface(_rows(2, with_equity=False))
    assert "Multiple-testing correction" not in text


# --- audit.overfit_block ------------------------------------------------------

def test_overfit_block_computes_pbo_and_headline_dsr():
    equities = {
        "current_full": _equity(0.001, seed=11),
        "stress_full": _equity(0.0005, seed=12),
        "cost_severe": _equity(0.0002, seed=13),
    }
    block = overfit_block(equities, trials=15)
    assert block["status"] == "completed"
    assert block["trials"] == 15
    assert 0.0 <= block["pbo"] <= 1.0
    assert block["curves_used"] == list(equities)
    # The headline spec is the first one handed in (spec order: current_full).
    assert block["headline"] == "current_full"
    dsr = block["dsr"]
    assert dsr["observations"] == 299  # 300 sessions -> 299 daily returns
    assert 0.0 <= dsr["deflated"] <= 1.0
    assert block["min_backtest_years"] >= 0.0
    # The honesty line travels with the numbers.
    assert "not the full parameter search" in block["note"]


def test_overfit_block_skips_pbo_when_ranges_do_not_overlap_but_keeps_dsr():
    equities = {
        "current_full": _equity(0.001, seed=21, start="2020-01-02"),
        "point_in_time_2007_2018": _equity(0.0005, seed=22, start="2007-01-02"),
    }
    block = overfit_block(equities, trials=6)
    assert "pbo" not in block
    assert "common date range" in block["reason"]
    assert block["dsr"] is not None


def test_overfit_block_drops_short_curves_from_the_matrix():
    equities = {
        "current_full": _equity(0.001, seed=31),
        "stress_full": _equity(0.0005, seed=32),
        "stub": _equity(0.0, n=5, seed=33),  # < 16 daily returns
    }
    block = overfit_block(equities, trials=3)
    assert block["status"] == "completed"
    assert block["curves_used"] == ["current_full", "stress_full"]


def test_overfit_block_never_raises_on_empty_or_degenerate_input():
    assert overfit_block({}, trials=5)["status"] == "skipped"
    flat = pd.Series(100_000.0, index=pd.bdate_range("2022-01-03", periods=60))
    block = overfit_block({"current_full": flat}, trials=1)
    assert block["headline"] == "current_full"  # zero-variance curve survives


def test_format_overfitting_renders_ascii_lines():
    equities = {"current_full": _equity(0.001, seed=41), "alt": _equity(0.0, seed=42)}
    lines = _format_overfitting(overfit_block(equities, trials=9))
    text = "\n".join(lines)
    assert "Multiple-testing correction" in text
    assert "PBO" in text
    assert "Deflated Sharpe" in text
    assert _ascii(text)
