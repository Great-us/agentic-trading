"""Append-only JSONL of in-flight cycle progress for the dashboard cockpit.

Writes are best-effort: a failed emit must never abort trading. The file is
truncated when it grows past MAX_BYTES so a crashed writer cannot fill the disk.

Path is `{book_root}/data/live_events.jsonl`. Trading processes write via
LIVE_EVENTS_PATH (this package's ROOT). The dashboard tails each book's own
copy and must not import run.py.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
LIVE_EVENTS_PATH = ROOT / "data" / "live_events.jsonl"

MAX_BYTES = 256 * 1024
KEEP_BYTES = 128 * 1024
MAX_REPLAY_LINES = 120

ET = ZoneInfo("America/New_York")

# Lockstep with run.RUN_TIMES_ET / the FastScan scheduled task. Duplicated
# here so the dashboard can compute the next slot without importing run.py.
DEEP_SLOTS_ET = ((9, 45), (16, 15))
FAST_START_ET = (9, 35)
FAST_INTERVAL_MIN = 20
FAST_SPAN_MIN = 6 * 60 + 30  # 09:35 → 16:05
DEEP_YIELD_MINUTES = 5

_SECRET_KEYS = frozenset({
    "api_key", "secret", "secret_key", "password", "token", "prompt",
    "authorization", "alpaca_api_key", "alpaca_secret_key", "moonshot_api_key",
})


def events_path(book_root: Path | None = None) -> Path:
    return (Path(book_root) if book_root is not None else ROOT) / "data" / "live_events.jsonl"


def emit(stage: str, **fields) -> None:
    """Append one cockpit event. Never raises."""
    try:
        payload: dict = {
            "t": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
        }
        for key, value in fields.items():
            if value is None or key in _SECRET_KEYS:
                continue
            payload[key] = value
        path = LIVE_EVENTS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        log.exception("live_events emit %s failed — continuing without it.", stage)


def _rotate_if_needed(path: Path, max_bytes: int = MAX_BYTES, keep_bytes: int = KEEP_BYTES) -> None:
    try:
        if not path.is_file() or path.stat().st_size < max_bytes:
            return
        data = path.read_bytes()
        tail = data[-keep_bytes:] if keep_bytes < len(data) else data
        nl = tail.find(b"\n")
        if nl >= 0:
            tail = tail[nl + 1 :]
        path.write_bytes(tail)
    except OSError:
        log.exception("live_events rotate failed")


def read_tail(path: Path, max_lines: int = MAX_REPLAY_LINES) -> list[dict]:
    """Last complete JSON objects in the file. Empty on any error."""
    try:
        if not path.is_file():
            return []
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict] = []
    for line in raw.splitlines()[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def read_since(path: Path, pos: int) -> tuple[list[dict], int]:
    """Read complete JSONL lines after byte offset `pos`. Handles truncation."""
    try:
        if not path.is_file():
            return [], 0
        size = path.stat().st_size
        if size < pos:
            pos = 0
        with open(path, "r", encoding="utf-8") as fh:
            fh.seek(pos)
            chunk = fh.read()
            new_pos = fh.tell()
    except OSError:
        return [], pos
    events: list[dict] = []
    leftover = ""
    if not chunk.endswith("\n") and "\n" in chunk:
        chunk, leftover = chunk.rsplit("\n", 1)
        new_pos -= len(leftover.encode("utf-8"))
    elif not chunk.endswith("\n"):
        return [], pos
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events, new_pos


def next_scheduled_slot(now: datetime | None = None) -> dict:
    """Next deep cycle or fast-scan grid slot (ET weekdays)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_et = now.astimezone(ET)
    deep = _next_deep(now_et)
    fast = _next_fast(now_et)
    candidates = [("deep", deep), ("fast", fast)]
    candidates = [(k, t) for k, t in candidates if t is not None]
    if not candidates:
        return {"next": None, "kind": None, "seconds": None,
                "next_deep": None, "next_fast": None}
    kind, when = min(candidates, key=lambda kv: kv[1])
    seconds = max(0, int((when - now_et).total_seconds()))
    return {
        "next": when.astimezone(timezone.utc).isoformat(),
        "kind": kind,
        "seconds": seconds,
        "next_deep": deep.astimezone(timezone.utc).isoformat() if deep else None,
        "next_fast": fast.astimezone(timezone.utc).isoformat() if fast else None,
    }


def _next_deep(now_et: datetime) -> datetime | None:
    cursor = now_et.replace(second=0, microsecond=0)
    for _ in range(10):
        if cursor.weekday() < 5:
            for hour, minute in DEEP_SLOTS_ET:
                slot = cursor.replace(hour=hour, minute=minute)
                if slot > now_et:
                    return slot
        cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
    return None


def _near_deep(hour: int, minute: int) -> bool:
    now_minutes = hour * 60 + minute
    return any(
        abs(now_minutes - (dh * 60 + dm)) <= DEEP_YIELD_MINUTES
        for dh, dm in DEEP_SLOTS_ET
    )


def _next_fast(now_et: datetime) -> datetime | None:
    start_min = FAST_START_ET[0] * 60 + FAST_START_ET[1]
    end_min = start_min + FAST_SPAN_MIN
    cursor = now_et.replace(second=0, microsecond=0)
    for _ in range(10):
        if cursor.weekday() < 5:
            m = start_min
            while m <= end_min:
                hour, minute = divmod(m, 60)
                if not _near_deep(hour, minute):
                    slot = cursor.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if slot > now_et:
                        return slot
                m += FAST_INTERVAL_MIN
        cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
    return None
