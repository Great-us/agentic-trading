"""Operator handoff card: what this wakeup did, and the exact next job.

Overwrite `{book_root}/data/progress.json` after every cycle/round — including
failures and no-trade exits — so the next process (and the dashboard) can
orient without reading a chat thread.

Heartbeat answers "alive?". live_events.jsonl is an in-flight cockpit tail.
This file answers "what just happened, what is still broken, what is next".

Writes are best-effort: a failed emit must never abort trading. Path is
resolved at call time (PROGRESS_PATH is a module attribute) so tests can
redirect it the same way they redirect HEARTBEAT_PATH.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .live_events import ET, next_scheduled_slot

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
PROGRESS_PATH = ROOT / "data" / "progress.json"

SCHEMA_VERSION = 1

_SECRET_KEYS = frozenset({
    "api_key", "secret", "secret_key", "password", "token", "prompt",
    "authorization", "alpaca_api_key", "alpaca_secret_key", "moonshot_api_key",
    "raw_json", "stage1_json", "stage2_json",
})

P3_MIDDAY = (10, 30)
P3_EVENING = (16, 30)

_P1_FAST_INSTRUCTION = "快扫：flush 意图并棘轮止损，不调用 LLM"
_P1_DEEP_INSTRUCTION = "深周期：分析 + 开仓窗内允许新开仓"
_P3_EVENING_INSTRUCTION = "evening：只卖/持有，禁止新开仓"
_P3_MIDDAY_INSTRUCTION = "midday：开仓窗内允许新开仓"
_P3_FLATTEN_RECOVERY = "恢复清仓：禁止新开仓，继续 flatten 直到空仓"
_P3_AFTER_FLAT = "下一交易日 midday 之前禁止新开仓；midday 起恢复开仓窗"

STATUSES = frozenset({"ok", "failed", "no_trade", "incomplete"})


def progress_path(book_root: Path | None = None) -> Path:
    if book_root is not None:
        return Path(book_root) / "data" / "progress.json"
    return PROGRESS_PATH


def infer_book_id(root: Path | None = None) -> str:
    name = (root or ROOT).resolve().name.lower()
    if name.endswith("-p2"):
        return "p2"
    if name.endswith("-p3"):
        return "p3"
    if name.endswith("-p4"):
        return "p4"
    return "p1"


def read_progress(path: Path | None = None) -> dict | None:
    """Return the card or None when missing/unreadable. Never raises."""
    target = Path(path) if path is not None else PROGRESS_PATH
    try:
        if not target.is_file():
            return None
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_progress(card: dict, path: Path | None = None) -> None:
    """Overwrite the card. Never raises."""
    target = Path(path) if path is not None else PROGRESS_PATH
    try:
        payload = _sanitize(card)
        payload["schema_version"] = SCHEMA_VERSION
        payload.setdefault("written_at", datetime.now(timezone.utc).isoformat())
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
        tmp.replace(target)
    except Exception:
        log.exception("progress write failed — continuing without it.")


def emit_progress(
    *,
    book_id: str,
    round_name: str,
    status: str,
    round_id: str = "",
    asof: str | None = None,
    did: list[str] | None = None,
    did_not: list[str] | None = None,
    broker: dict | None = None,
    risk: dict | None = None,
    unresolved: list | None = None,
    now: datetime | None = None,
    path: Path | None = None,
) -> dict:
    """Build, stamp next_job, write. Returns the card (empty dict on write skip)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if status not in STATUSES:
        status = "failed"
    asof = asof or now.astimezone(ET).date().isoformat()
    if book_id == "p3":
        nxt = next_job_p3(now, last_round=round_name, status=status)
    else:
        nxt = next_job_p1(now, last_round=round_name)
    card = {
        "book_id": book_id,
        "written_at": now.isoformat(),
        "round": {
            "name": round_name,
            "round_id": str(round_id or ""),
            "asof": asof,
            "status": status,
        },
        "did": [str(x) for x in (did or []) if x],
        "did_not": [str(x) for x in (did_not or []) if x],
        "broker": _broker_block(broker),
        "risk": _risk_block(risk),
        "unresolved": _unresolved_block(unresolved),
        "next_job": nxt,
    }
    write_progress(card, path=path)
    return card


def next_job_p1(now: datetime | None = None, *, last_round: str = "deep") -> dict:
    """Next P1/P2 Task Scheduler slot. Instruction depends on that slot, not the LLM."""
    del last_round  # the calendar, not the previous name, decides what fires next
    slot = next_scheduled_slot(now)
    kind = slot.get("kind") or "deep"
    when_iso = slot.get("next")
    when_et = _hhmm(when_iso)
    if kind == "fast":
        instruction = _P1_FAST_INSTRUCTION
    else:
        instruction = _P1_DEEP_INSTRUCTION
    return {
        "slot": kind,
        "when_et": when_et,
        "when": when_iso,
        "instruction": instruction,
    }


def next_job_p3(
    now: datetime | None = None,
    *,
    last_round: str,
    status: str = "ok",
) -> dict:
    """Next P3 slot on the existing midday/evening calendar. Flatten recovery
    never suggests opening risk."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_et = now.astimezone(ET)

    failed_flat = last_round == "flatten" and status in {"failed", "incomplete"}
    if failed_flat:
        return {
            "slot": "flatten",
            "when_et": "now",
            "when": now.isoformat(),
            "instruction": _P3_FLATTEN_RECOVERY,
        }

    if last_round == "flatten" and status == "ok":
        when = _next_weekday_hm(now_et, *P3_MIDDAY)
        return _p3_job("midday", when, _P3_AFTER_FLAT)

    if last_round == "midday":
        when = _next_weekday_hm(now_et, *P3_EVENING)
        return _p3_job("evening", when, _P3_EVENING_INSTRUCTION)

    when = _next_weekday_hm(now_et, *P3_MIDDAY)
    return _p3_job("midday", when, _P3_MIDDAY_INSTRUCTION)


def _p3_job(slot: str, when: datetime | None, instruction: str) -> dict:
    when_iso = when.astimezone(timezone.utc).isoformat() if when is not None else None
    return {
        "slot": slot,
        "when_et": _hhmm(when_iso) if when_iso else None,
        "when": when_iso,
        "instruction": instruction,
    }


def _next_weekday_hm(now_et: datetime, hour: int, minute: int,
                     *, skip_today: bool = False) -> datetime | None:
    cursor = now_et.replace(second=0, microsecond=0)
    if skip_today:
        cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
    for _ in range(10):
        if cursor.weekday() < 5:
            slot = cursor.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if slot > now_et:
                return slot
        cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
    return None


def _hhmm(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(ET).strftime("%H:%M")


def _broker_block(raw: dict | None) -> dict:
    raw = raw or {}
    return {
        "equity": _num(raw.get("equity")),
        "cash": _num(raw.get("cash")),
        "n_positions": int(raw.get("n_positions") or 0),
        "n_open_orders": int(raw.get("n_open_orders") or 0),
        "degraded": bool(raw.get("degraded")),
        "reason": raw.get("reason"),
    }


def _risk_block(raw: dict | None) -> dict:
    raw = raw or {}
    locks = raw.get("locks") or []
    if isinstance(locks, str):
        locks = [locks]
    return {
        "exposure_pct": _num(raw.get("exposure_pct")),
        "stops_covered": raw.get("stops_covered"),
        "stops_total": raw.get("stops_total"),
        "locks": [str(x) for x in locks if x],
    }


def _unresolved_block(raw: list | None) -> list[dict]:
    out: list[dict] = []
    for item in raw or []:
        if isinstance(item, str):
            out.append({"kind": "note", "detail": item})
        elif isinstance(item, dict):
            kind = str(item.get("kind") or "note")
            detail = item.get("detail")
            out.append({"kind": kind, "detail": None if detail is None else str(detail)})
    return out


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sanitize(obj):
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items() if k not in _SECRET_KEYS}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj
