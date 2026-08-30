"""Forward outcomes for journaled decisions. Read-only on live knobs.

Fills `signal_outcomes` with +1d/+5d/+20d returns and 20-day MFE/MAE for every
decision the journal records — including `avoid` and `hold`, which for a long
time were silently skipped although they are the majority of the book's
decisions. An avoided name that then falls vindicates the pass; one that runs
away was an opportunity cost. The columns are the same raw forward returns the
buy rows get — semantics are identical, only the reading differs (for
avoid/hold a negative/positive forward return is the vindication), so the
buckets sit side by side in the same report.

Prints bucket summaries. Does not retune buy_threshold, LLM weights, or
risk.yaml.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .logger import DEFAULT_DB_PATH, connect

logger = logging.getLogger(__name__)

# Every action the decision engine can record. Avoid/hold are decisions too —
# skipping them made the largest decision class invisible to this report.
ACTIONS = ("buy", "sell", "trim", "wait", "hold", "avoid")
MIN_REPORT_N = 5


@dataclass
class OutcomeRow:
    decision_id: int
    symbol: str
    action: str
    quant_score: float
    llm_stance: str | None
    llm_confidence: float | None
    llm_risk_flags: str | None
    asof: str
    ret_1d: float | None
    ret_5d: float | None
    ret_20d: float | None
    mfe_20d: float | None
    mae_20d: float | None


def _naive_index(idx: pd.Index) -> pd.DatetimeIndex:
    di = pd.DatetimeIndex(idx)
    if di.tz is not None:
        di = di.tz_localize(None)
    return pd.DatetimeIndex(di.normalize())


def forward_stats(df: pd.DataFrame, asof: pd.Timestamp) -> dict[str, float | None]:
    """Returns vs the last close on or before `asof`. None if the horizon isn't in the data."""
    empty = {"ret_1d": None, "ret_5d": None, "ret_20d": None, "mfe_20d": None, "mae_20d": None}
    if df is None or df.empty or "Close" not in df.columns:
        return empty
    work = df.copy()
    work.index = _naive_index(work.index)
    work = work[~work.index.duplicated(keep="last")].sort_index()
    asof_d = pd.Timestamp(asof).tz_localize(None).normalize() if pd.Timestamp(asof).tzinfo else pd.Timestamp(asof).normalize()
    prior = work.loc[work.index <= asof_d]
    if prior.empty:
        return empty
    i = work.index.get_loc(prior.index[-1])
    if isinstance(i, slice):
        i = i.stop - 1
    close = work["Close"].astype(float)
    c0 = float(close.iloc[i])
    if c0 <= 0 or c0 != c0:
        return empty

    def _ret(n: int) -> float | None:
        j = i + n
        if j >= len(close):
            return None
        cj = float(close.iloc[j])
        if cj != cj:
            return None
        return cj / c0 - 1.0

    future = work.iloc[i + 1 : i + 21]
    mfe = mae = None
    if not future.empty:
        high = future["High"].astype(float) if "High" in future.columns else future["Close"].astype(float)
        low = future["Low"].astype(float) if "Low" in future.columns else future["Close"].astype(float)
        mfe = float(high.max()) / c0 - 1.0
        mae = float(low.min()) / c0 - 1.0
    return {
        "ret_1d": _ret(1), "ret_5d": _ret(5), "ret_20d": _ret(20),
        "mfe_20d": mfe, "mae_20d": mae,
    }


def _cycle_asof(timestamp: str) -> pd.Timestamp:
    ts = pd.Timestamp(timestamp)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(None)
    return ts.normalize()


def load_decision_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    placeholders = ", ".join("?" for _ in ACTIONS)
    return list(conn.execute(
        f"""SELECT d.id AS decision_id, d.symbol, d.action, d.quant_score,
                  d.llm_stance, d.llm_confidence, d.llm_risk_flags, c.timestamp
           FROM decisions d JOIN cycles c ON c.id = d.cycle_id
           WHERE d.action IN ({placeholders})
           ORDER BY d.id""",
        ACTIONS,
    ))


def evaluate_journal(
    conn: sqlite3.Connection,
    bars: dict[str, pd.DataFrame] | None = None,
) -> list[OutcomeRow]:
    rows = load_decision_rows(conn)
    if not rows:
        return []
    symbols = sorted({r["symbol"] for r in rows})
    if bars is None:
        from ..backtest.data import load_price_history
        bars = load_price_history(symbols)
    now = datetime.now(timezone.utc).isoformat()
    out: list[OutcomeRow] = []
    for row in rows:
        asof = _cycle_asof(row["timestamp"])
        stats = forward_stats(bars.get(row["symbol"], pd.DataFrame()), asof)
        conn.execute(
            """INSERT INTO signal_outcomes
               (decision_id, asof, ret_1d, ret_5d, ret_20d, mfe_20d, mae_20d, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(decision_id) DO UPDATE SET
                 asof=excluded.asof, ret_1d=excluded.ret_1d, ret_5d=excluded.ret_5d,
                 ret_20d=excluded.ret_20d, mfe_20d=excluded.mfe_20d,
                 mae_20d=excluded.mae_20d, evaluated_at=excluded.evaluated_at""",
            (row["decision_id"], asof.date().isoformat(),
             stats["ret_1d"], stats["ret_5d"], stats["ret_20d"],
             stats["mfe_20d"], stats["mae_20d"], now),
        )
        out.append(OutcomeRow(
            decision_id=row["decision_id"], symbol=row["symbol"], action=row["action"],
            quant_score=row["quant_score"] if row["quant_score"] is not None else 0.0,
            llm_stance=row["llm_stance"], llm_confidence=row["llm_confidence"],
            llm_risk_flags=row["llm_risk_flags"], asof=asof.date().isoformat(),
            **stats,
        ))
    conn.commit()
    return out


def symbol_sources() -> dict[str, str] | None:
    """Map watchlist symbol -> 'core' | 'vault' from the live config.

    Vault membership is evaluated as-of-now: a name removed from the vault
    later reports as 'other' for its whole history. Best-effort — any failure
    returns None and the report simply omits the source breakdown."""
    try:
        from ..config import load_settings

        settings = load_settings(include_research=True)
        sources: dict[str, str] = {}
        for s in settings.core_watchlist:
            sources[s.upper()] = "core"
        for s in settings.research_symbols:
            sources.setdefault(s.upper(), "vault")
        return sources or None
    except Exception:  # noqa: BLE001 — reporting must not die on config problems
        return None


def _mean(values: list[float | None]) -> float | None:
    xs = [v for v in values if v is not None and v == v]
    if not xs:
        return None
    return float(sum(xs) / len(xs))


def _fmt(value: float | None) -> str:
    return "     n/a" if value is None else f"{value:+8.1%}"


def format_report(rows: list[OutcomeRow], sources: dict[str, str] | None = None) -> str:
    if not rows:
        return f"No {'/'.join(ACTIONS)} decisions in the journal."
    lines = [
        f"Forward outcomes for {len(rows)} decisions (does not change live knobs).",
        "",
        f"{'bucket':<28} {'n':>4} {'+1d':>8} {'+5d':>8} {'+20d':>8} {'MFE20':>8} {'MAE20':>8}",
        "-" * 76,
    ]

    def add(name: str, subset: list[OutcomeRow]) -> None:
        n = len(subset)
        if n == 0:
            return
        if n < MIN_REPORT_N:
            lines.append(f"{name:<28} {n:4d}   (n<{MIN_REPORT_N}, skip averages)")
            return
        lines.append(
            f"{name:<28} {n:4d} {_fmt(_mean([r.ret_1d for r in subset]))} "
            f"{_fmt(_mean([r.ret_5d for r in subset]))} {_fmt(_mean([r.ret_20d for r in subset]))} "
            f"{_fmt(_mean([r.mfe_20d for r in subset]))} {_fmt(_mean([r.mae_20d for r in subset]))}"
        )

    add("all", rows)
    add("action=buy", [r for r in rows if r.action == "buy"])
    add("action=sell", [r for r in rows if r.action == "sell"])
    add("action=avoid", [r for r in rows if r.action == "avoid"])
    add("action=hold", [r for r in rows if r.action == "hold"])
    # Below the noise floor, and by far the biggest bucket — these are the names
    # the engine passed on. Leaving it out made the printed buckets look like a
    # ladder when the full picture is a barbell (the same shape factor_ic finds
    # cross-sectionally), and hid that the best forward returns sit here.
    add("quant <0.15", [r for r in rows if r.quant_score < 0.15])
    add("quant 0.15–0.30", [r for r in rows if 0.15 <= r.quant_score < 0.30])
    add("quant 0.30–0.45", [r for r in rows if 0.30 <= r.quant_score < 0.45])
    add("quant ≥0.45", [r for r in rows if r.quant_score >= 0.45])
    add(
        "LLM bullish ≥70%",
        [r for r in rows if r.llm_stance == "bullish" and (r.llm_confidence or 0) >= 0.70],
    )
    add(
        "LLM bullish <70%",
        [r for r in rows if r.llm_stance == "bullish" and (r.llm_confidence or 0) < 0.70],
    )
    add("LLM bearish", [r for r in rows if r.llm_stance == "bearish"])
    add(
        "quant+ / LLM−",
        [r for r in rows if r.quant_score > 0 and r.llm_stance == "bearish"],
    )
    add(
        "flag~earnings",
        [r for r in rows if r.llm_risk_flags and "earn" in r.llm_risk_flags.lower()],
    )
    if sources is not None:
        # Where the name came from: hand-maintained watchlist vs Obsidian vault
        # extraction. This is how the vault mechanism earns (or loses) trust.
        add("source=core", [r for r in rows if sources.get(r.symbol) == "core"])
        add("source=vault", [r for r in rows if sources.get(r.symbol) == "vault"])
        add("source=other", [r for r in rows if r.symbol not in sources])
    lines.append("")
    lines.append(
        "Reading the non-buy rows: action=avoid was right when its mean forward "
        "return is negative (the pass dodged a loss); action=hold was right "
        "when positive. Same raw returns as the buy rows, opposite vindication."
    )
    lines.append("Averages are descriptive. They are not a licence to retune live.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.db.exists() and str(args.db) != ":memory:":
        print(f"No journal at {args.db}")
        return 0
    conn = connect(args.db)
    rows = evaluate_journal(conn)
    print(format_report(rows, sources=symbol_sources()))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
