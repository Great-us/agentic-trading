"""Quant-only historical replay of the live decision/risk/execution stack.

Usage:
    python -m agentic_trading.backtest --start 2019-01-01
    python -m agentic_trading.backtest --start 2019-01-01 --end 2026-08-18 --cash 100000
    python -m agentic_trading.backtest --sweep
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from .engine import BACKTEST_DIR, format_report, run_backtest

# One-at-a-time around the live defaults — not a combinatorial grid.
SWEEPS: list[tuple[str, dict]] = [
    ("baseline", {}),
    ("buy_threshold=0.20", {"buy_threshold": 0.20}),
    ("buy_threshold=0.35", {"buy_threshold": 0.35}),
    ("atr_stop=2.0", {"atr_stop_multiple": 2.0}),
    ("atr_stop=3.0", {"atr_stop_multiple": 3.0}),
    ("trail=8%", {"trailing_stop_pct": 0.08}),
    ("trail=18%", {"trailing_stop_pct": 0.18}),
    ("regime_off", {"risk_off_size_multiplier": 1.0, "risk_off_score_penalty": 0.0}),
    ("max_pos=4", {"max_open_positions": 4}),
    ("max_pos=6", {"max_open_positions": 6}),
]


def _setup() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--cash", type=float, default=100_000.0)
    parser.add_argument("--sweep", action="store_true",
                        help="Run the small one-at-a-time parameter sweep around current risk.yaml defaults.")
    parser.add_argument("--force-download", action="store_true")
    args = parser.parse_args()

    _setup()
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)

    if args.force_download:
        from .data import load_price_history
        from ..config import load_settings
        settings = load_settings(include_research=False)
        load_price_history(settings.core_watchlist or settings.watchlist, force=True)

    if args.sweep:
        print(f"Sweep {args.start} → {args.end}  cash=${args.cash:,.0f}\n")
        print(f"{'name':<22} {'CAGR':>8} {'Sharpe':>8} {'MaxDD':>8} {'End $':>12} {'buys':>6}")
        print("-" * 70)
        for name, overrides in SWEEPS:
            result = run_backtest(
                args.start, args.end,
                initial_cash=args.cash,
                risk_overrides=overrides or None,
                journal_path=":memory:",
            )
            m = result.metrics
            print(f"{name:<22} {m.cagr:+8.1%} {m.sharpe:8.2f} {m.max_drawdown:8.1%} "
                  f"{m.end_equity:12,.0f} {m.n_buys:6d}")
        return

    journal = BACKTEST_DIR / "journal.db"
    if journal.exists():
        journal.unlink()
    result = run_backtest(
        args.start, args.end,
        initial_cash=args.cash,
        journal_path=journal,
    )
    print(format_report(result))
    equity_path = BACKTEST_DIR / "equity.csv"
    result.equity.rename("equity").to_csv(equity_path, header=True)
    print(f"\nWrote {equity_path} and {journal}")


if __name__ == "__main__":
    main()
