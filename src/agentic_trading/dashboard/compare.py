"""Cross-book comparison metrics — the "did rotation add information" view.

Implements the two diagnostics pre-registered in research/PAPER2-THEME-ROTATION.md
§5: daily holdings overlap and the rolling correlation of daily returns, plus
a normalized equity overlay for context.
"""
from __future__ import annotations

from datetime import datetime


def _parse_day(value: str | None) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def _holdings_by_day(fills: list[dict]) -> dict[str, set[str]]:
    """day -> set of held symbols, walked from chronological fills.

    fills arrive newest-first; sort ascending first. A symbol is held on every
    day between the fill that opened it and the fill that closed it (inclusive
    of open day; if still open, through the last fill date).
    """
    series = sorted(fills, key=lambda f: f["transaction_time"] or "")
    lots: dict[str, float] = {}
    opened: dict[str, str] = {}
    held: dict[str, set[str]] = {}

    def stamp(day: str, symbol: str) -> None:
        held.setdefault(day, set()).add(symbol)

    def backfill(symbol: str, start: str, end: str) -> None:
        # Mark every calendar day in [start, end] — both books trade the same
        # US session so a compact business-day walk is enough.
        cursor = datetime.fromisoformat(start)
        stop = datetime.fromisoformat(end)
        while cursor <= stop:
            key = cursor.date().isoformat()
            if cursor.weekday() < 5:
                stamp(key, symbol)
            cursor = datetime.fromordinal(cursor.toordinal() + 1)

    for fill in series:
        symbol = fill["symbol"]
        day = _parse_day(fill["transaction_time"])
        if not day or not symbol:
            continue
        signed = float(fill["qty"]) * (1 if fill["side"] == "buy" else -1)
        was_flat = abs(lots.get(symbol, 0.0)) < 1e-9
        lots[symbol] = lots.get(symbol, 0.0) + signed
        is_flat = abs(lots[symbol]) < 1e-9
        if was_flat and not is_flat:
            opened[symbol] = day
        elif not is_flat:
            backfill(symbol, opened.get(symbol, day), day)
        if is_flat and symbol in opened:
            backfill(symbol, opened.pop(symbol), day)
            lots[symbol] = 0.0
    for symbol, start in opened.items():
        last_day = max(
            (d for d in (_parse_day(f["transaction_time"]) for f in series) if d),
            default=start,
        )
        backfill(symbol, start, last_day)
    return held


def overlap_timeline(fills_a: list[dict], fills_b: list[dict]) -> list[dict]:
    """Per-day overlap = |A∩B| / max(|A|,|B|); 0 when either book is flat."""
    a = _holdings_by_day(fills_a)
    b = _holdings_by_day(fills_b)
    days = sorted(set(a) | set(b))
    out = []
    for day in days:
        sa, sb = a.get(day, set()), b.get(day, set())
        denominator = max(len(sa), len(sb))
        ratio = len(sa & sb) / denominator if denominator else 0.0
        out.append({"date": day, "overlap": round(ratio, 4),
                    "p1_count": len(sa), "p2_count": len(sb)})
    return out[-180:]


def _daily_returns(equity_series: list[dict]) -> dict[str, float]:
    """date -> daily return of the book's equity, using one point per day."""
    points = sorted((e for e in equity_series if e["equity"] is not None),
                    key=lambda e: e["date"])
    out: dict[str, float] = {}
    prev: tuple[str, float] | None = None
    for point in points:
        equity = float(point["equity"])
        if prev is not None:
            if prev[1] > 0:
                out[point["date"]] = equity / prev[1] - 1.0
        prev = (point["date"], equity)
    return out


def rolling_correlation(series_a: list[dict], series_b: list[dict],
                        window: int = 60) -> list[dict]:
    """60-day rolling Pearson correlation of daily returns on common dates."""
    ra, rb = _daily_returns(series_a), _daily_returns(series_b)
    days = sorted(set(ra) & set(rb))
    pairs = [(day, ra[day], rb[day]) for day in days]
    out: list[dict] = []
    for i in range(window, len(pairs)):
        chunk = pairs[i - window:i]
        xs = [c[1] for c in chunk]
        ys = [c[2] for c in chunk]
        mean_x, mean_y = sum(xs) / window, sum(ys) / window
        cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        var_x = sum((x - mean_x) ** 2 for x in xs)
        var_y = sum((y - mean_y) ** 2 for y in ys)
        corr = cov / ((var_x ** 0.5) * (var_y ** 0.5)) if var_x > 0 and var_y > 0 else None
        out.append({"date": pairs[i][0], "correlation": round(corr, 4) if corr is not None else None})
    return out


def normalized_equity(series_a: list[dict], series_b: list[dict],
                      days: int = 180) -> dict[str, list]:
    """Both books' equity rebased to 1.0 at their own window start."""
    def rebase(series: list[dict]) -> list[dict]:
        trimmed = [s for s in series if s["equity"] is not None][-days:]
        base = float(trimmed[0]["equity"]) if trimmed and float(trimmed[0]["equity"]) > 0 else None
        return [{"date": s["date"],
                 "value": round(float(s["equity"]) / base, 4) if base else None}
                for s in trimmed]

    return {"p1": rebase(series_a), "p2": rebase(series_b)}
