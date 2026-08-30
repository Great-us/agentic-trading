"""Quant-only historical replay of the live decision/risk/execution stack.

Usage:
    python -m agentic_trading.backtest                          # 2007-04-11 → today
    python -m agentic_trading.backtest --start 2007-04-11 --end 2026-08-21
    python -m agentic_trading.backtest --sweep
    python -m agentic_trading.backtest --surface
    python -m agentic_trading.backtest --walk-forward
    python -m agentic_trading.backtest --ablate

The default start is **2007-04-11**, the first day HYG exists — the earliest
date the live five-ratio regime can run without silently dropping credit.
Earlier defaults were tried and rejected: a 2019 default re-validates the
strategy on the 2019-2026 mega-cap bull (SPY itself +17.5%), which is exactly
the biased window that produced the overstated 22% CAGR. See
HANDOFF-BACKTEST.md §5.4 for the window comparison.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from .engine import BACKTEST_DIR, format_report, run_backtest
from .experiments import ablations, format_ablations, format_surface, format_walk_forward, parameter_surface, walk_forward

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
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default="2007-04-11",
                        help="Backtest start date. Defaults to 2007-04-11 (first HYG bar): the "
                             "earliest date the live regime can run without silently dropping "
                             "credit. Later starts re-enter the 2019+ bull window — pass one only "
                             "deliberately.")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--cash", type=float, default=100_000.0)
    parser.add_argument("--sweep", action="store_true",
                        help="Run the small one-at-a-time parameter sweep around current risk.yaml defaults.")
    parser.add_argument("--surface", action="store_true",
                        help="Grid buy_threshold × atr_stop_multiple on the full window (in-sample ranking).")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Expanding IS, 1-year OOS; pick the surface winner on IS, report concatenated OOS.")
    parser.add_argument("--ablate", action="store_true",
                        help="Rerun with RSI off, macro off, stops-only exits, and SPY/QQQ tradeable.")
    parser.add_argument("--force-download", action="store_true")
    args = parser.parse_args()

    _setup()
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)

    if date.fromisoformat(args.end) >= date(2023, 1, 1):
        logging.getLogger("backtest").info(
            "This run extends into 2023+ — overlapping the CONSUMED holdout window "
            "(2023-01-03 → 2026-08-21, see HOLDOUT-LEDGER.md). Treat any number from "
            "that slice as in-sample for parameter decisions."
        )

    from .data import load_price_history
    from ..config import load_settings
    shared_bars = None

    def _bars():
        nonlocal shared_bars
        if shared_bars is None:
            settings = load_settings(include_research=False)
            shared_bars = load_price_history(
                list(settings.core_watchlist or settings.watchlist) + ["SPY", "QQQ"],
                force=args.force_download,
            )
        return shared_bars

    if args.surface:
        rows = parameter_surface(args.start, args.end, bars=_bars(), initial_cash=args.cash)
        text = format_surface(rows)
        print(text)
        path = BACKTEST_DIR / "surface.txt"
        path.write_text(text + "\n", encoding="utf-8")
        print(f"\nWrote {path}")
        return

    if args.walk_forward:
        folds, equity, combined = walk_forward(
            args.start, args.end, bars=_bars(), initial_cash=args.cash,
        )
        text = format_walk_forward(folds, combined)
        print(text)
        (BACKTEST_DIR / "walk_forward.txt").write_text(text + "\n", encoding="utf-8")
        if not equity.empty:
            path = BACKTEST_DIR / "walk_forward_oos.csv"
            equity.rename("equity").to_csv(path, header=True)
            print(f"\nWrote {path}")
        return

    if args.ablate:
        rows = ablations(args.start, args.end, bars=_bars(), initial_cash=args.cash)
        text = format_ablations(rows)
        print(text)
        path = BACKTEST_DIR / "ablations.txt"
        path.write_text(text + "\n", encoding="utf-8")
        print(f"\nWrote {path}")
        return

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
                bars=_bars(),
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
