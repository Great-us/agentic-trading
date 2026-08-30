"""Parser for the P2 rotation roster archives (research/p2/rosters/*.md).

File naming: YYYY-MM-DD-evening.md (T-1 close snapshot, generates the next
day's watchlist) and YYYY-MM-DD-midday.md (intraday re-check with hysteresis).
Both share the same shape:

    # Roster 2026-08-25 (midday round, 2026-08-25 16:30 UTC)

    pool=251 scored | Top-10 | buyable(streak>=2)=['CHYM', ...] | held=['EAT', ...]

    | rank | symbol | RS | ex_20d | ex_60d | last | streak | buyable |
    |---:|---|---:|---:|---:|---:|---:|---|
    | 1 | NIQ | 99.6 | +59.9% | +127.5% | 19.10 | 5 | yes |
    ...
    watchlist diff vs previous: +[] -[]
"""
from __future__ import annotations

import re
from pathlib import Path

_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*([A-Z.\-]+)\s*\|\s*([\d.]+)\s*\|\s*([+\-\d.%]+)\s*"
    r"\|\s*([+\-\d.%]+)\s*\|\s*([\d.]+)\s*\|\s*(\d+)\s*\|\s*(yes|no)\s*\|"
)
_META_POOL = re.compile(r"pool=(\d+) scored")
_META_LIST = re.compile(r"\w+\(streak>=2\)=\[([^\]]*)\]|held=\[([^\]]*)\]")
_DIFF = re.compile(r"diff[^:]*:\s*\+\[([^\]]*)\]\s*-\[([^\]]*)\]")


def _parse_list(raw: str) -> list[str]:
    return [item.strip().strip("'\"") for item in raw.split(",") if item.strip()]


def parse_roster(text: str) -> dict:
    result: dict = {"round": None, "generated_at": None, "pool_size": None,
                    "buyable": [], "held": [], "rows": [], "added": [], "removed": []}
    for line in text.splitlines():
        header = re.match(r"^#\s*Roster\s+(\S+)\s*\((.*)\)\s*$", line)
        if header:
            result["round"] = "midday" if "midday" in header.group(2) else "evening"
            stamp = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?) UTC", header.group(2))
            if stamp:
                result["generated_at"] = stamp.group(1)
            continue
        pool = _META_POOL.search(line)
        if pool:
            result["pool_size"] = int(pool.group(1))
        for bracket in _META_LIST.finditer(line):
            raw_items = bracket.group(1) if bracket.group(1) is not None else (bracket.group(2) or "")
            items = _parse_list(raw_items)
            if bracket.group(0).startswith("held"):
                result["held"] = items
            else:
                result["buyable"] = items
            continue
        diff = _DIFF.search(line)
        if diff:
            result["added"] = _parse_list(diff.group(1))
            result["removed"] = _parse_list(diff.group(2))
            continue
        row = _ROW.match(line)
        if row:
            result["rows"].append(
                {
                    "rank": int(row.group(1)),
                    "symbol": row.group(2),
                    "rs": float(row.group(3)),
                    "ex_20d": row.group(4),
                    "ex_60d": row.group(5),
                    "last": float(row.group(6)),
                    "streak": int(row.group(7)),
                    "buyable": row.group(8) == "yes",
                }
            )
    return result


def latest_rosters(rosters_dir: Path) -> dict:
    """Latest evening and midday rosters, plus the newest file's metadata."""
    if not rosters_dir.is_dir():
        return {"available": False, "reason": f"no rosters directory at {rosters_dir}"}
    files = sorted(rosters_dir.glob("*.md"), reverse=True)
    if not files:
        return {"available": False, "reason": "roster archive is empty"}
    payload: dict = {"available": True}
    for kind in ("evening", "midday"):
        match = next((f for f in files if f.name.endswith(f"-{kind}.md")), None)
        payload[kind] = parse_roster(match.read_text(encoding="utf-8")) if match else None
    return payload
