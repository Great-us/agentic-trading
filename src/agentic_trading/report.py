"""Human-readable view of the journal, so reviewing a cycle doesn't require SQL.

Usage:
    python -m agentic_trading.report            # most recent cycle
    python -m agentic_trading.report --last 5   # summary of the last 5 cycles
    python -m agentic_trading.report --symbol NVDA   # one symbol's history
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

from .journal.logger import DEFAULT_DB_PATH, connect

ACTION_MARK = {"buy": "BUY ", "sell": "SELL", "hold": "hold", "wait": "wait", "avoid": "  - "}


def _fmt_score(value: float | None) -> str:
    return f"{value:+.2f}" if value is not None else "  n/a"


def show_latest(conn: sqlite3.Connection) -> None:
    cycle = conn.execute(
        "SELECT id, timestamp, mode, equity, cash, regime_score, regime_label FROM cycles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if cycle is None:
        print("No cycles recorded yet. Run `python -m agentic_trading.run` first.")
        return

    cycle_id, timestamp, mode, equity, cash, regime_score, regime_label = cycle
    print(f"Cycle #{cycle_id}  {timestamp}  [{mode}]")
    print(f"Equity ${equity:,.2f}   Cash ${cash:,.2f}")
    if regime_label:
        print(f"Market regime: {regime_label.upper()} ({regime_score:+.2f})")
    print()
    print(f"{'':4}  {'SYM':<6} {'QUANT':>6} {'LLM':>6} {'COMB':>6}  {'STANCE':<8} ORDER")
    print("-" * 78)

    rows = conn.execute(
        """SELECT symbol, quant_score, llm_stance, llm_confidence, combined_score,
                  action, order_status, order_qty, reasoning, llm_rationale
           FROM decisions WHERE cycle_id = ? ORDER BY combined_score DESC""",
        (cycle_id,),
    ).fetchall()

    for (symbol, quant, stance, conf, combined, action, status, qty, reasoning, rationale) in rows:
        llm_score = None
        if stance is not None and conf is not None:
            llm_score = {"bullish": 1, "neutral": 0, "bearish": -1}[stance] * conf
        # Notional buys report no share count until they fill, so a bare status
        # is the honest display rather than "x0".
        order = (f"{status} x{qty:g}" if status and qty else status or "")
        stance_label = f"{stance[:7]}" if stance else "-"
        print(
            f"{ACTION_MARK.get(action, action):4}  {symbol:<6} {_fmt_score(quant):>6} "
            f"{_fmt_score(llm_score):>6} {_fmt_score(combined):>6}  {stance_label:<8} {order}"
        )

    # An action of buy/sell doesn't guarantee an order: the risk manager can veto
    # it or the per-cycle order cap can skip it. Only report what actually filled.
    placed = [r for r in rows if r[6] is not None]
    if placed:
        print()
        print("Orders this cycle:")
        for (symbol, _q, _s, _c, _comb, action, _st, qty, reasoning, rationale) in placed:
            print(f"  {action.upper()} {symbol}" + (f" x{qty:g}" if qty else ""))
            if rationale:
                print(f"    why: {rationale}")

    blocked = [r for r in rows if r[5] in ("buy", "sell") and r[6] is None]
    if blocked:
        print()
        print("Wanted to trade but blocked:")
        for (symbol, _q, _s, _c, _comb, action, _st, _qty, reasoning, _rat) in blocked:
            print(f"  {action.upper()} {symbol} — {reasoning.split('. ')[-1].strip()}")


def show_recent_cycles(conn: sqlite3.Connection, limit: int) -> None:
    rows = conn.execute(
        """SELECT c.id, c.timestamp, c.mode, c.equity,
                  SUM(CASE WHEN d.action='buy' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN d.action='sell' THEN 1 ELSE 0 END),
                  COUNT(d.id)
           FROM cycles c LEFT JOIN decisions d ON d.cycle_id = c.id
           GROUP BY c.id ORDER BY c.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    if not rows:
        print("No cycles recorded yet.")
        return
    print(f"{'ID':>4}  {'WHEN':<28} {'MODE':<8} {'EQUITY':>12} {'BUY':>4} {'SELL':>5} {'EVAL':>5}")
    print("-" * 74)
    for cycle_id, ts, mode, equity, buys, sells, total in rows:
        print(f"{cycle_id:>4}  {ts[:26]:<28} {mode:<8} ${equity:>11,.2f} {buys or 0:>4} {sells or 0:>5} {total:>5}")


def show_symbol(conn: sqlite3.Connection, symbol: str) -> None:
    rows = conn.execute(
        """SELECT c.timestamp, d.quant_score, d.llm_stance, d.combined_score, d.action, d.llm_rationale
           FROM decisions d JOIN cycles c ON c.id = d.cycle_id
           WHERE d.symbol = ? ORDER BY d.id DESC LIMIT 20""",
        (symbol.upper(),),
    ).fetchall()
    if not rows:
        print(f"No decisions recorded for {symbol.upper()}.")
        return
    print(f"History for {symbol.upper()} (most recent first):")
    print()
    for ts, quant, stance, combined, action, rationale in rows:
        print(f"{ts[:19]}  {action.upper():<5} quant={_fmt_score(quant)} combined={_fmt_score(combined)} stance={stance or '-'}")
        if rationale:
            print(f"    {rationale}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--last", type=int, metavar="N", help="Summarize the last N cycles instead.")
    parser.add_argument("--symbol", type=str, help="Show decision history for one symbol.")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if not DEFAULT_DB_PATH.exists():
        print(f"No journal yet at {DEFAULT_DB_PATH}. Run `python -m agentic_trading.run` first.")
        return

    # Go through the journal's connect() rather than sqlite3 directly, so a
    # journal written by an older version gets migrated before it's queried.
    conn = connect()
    try:
        if args.symbol:
            show_symbol(conn, args.symbol)
        elif args.last:
            show_recent_cycles(conn, args.last)
        else:
            show_latest(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
