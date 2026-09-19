"""Scheduler heartbeat: proves that decision cycles are still finishing.

run_cycle stamps data/heartbeat.json at each of its normal exits (the deep
cycle and the fast-tier scan). A separate scheduled task — or a human — runs
`python -m agentic_trading.heartbeat --check`, which reads the stamp and
alerts when the pipeline has gone quiet: dead Task Scheduler entry, crashed
interpreter, a machine that stopped waking up. Purely observational — every
failure inside this module is logged and swallowed so it can never disturb
trading behaviour.

Stamp format (JSON object, one sub-entry per cycle kind):

    {"fast": {"timestamp": "<ISO UTC>", "cycle_id": 123,
              "stops_covered": 6, "positions": 6,
              "started_at": "<ISO UTC>", "scheduled_slot": "<ISO UTC>",
              "late_minutes": 1.4, "missed_slots": 0,
              "llm_status": {"state": "ok", ...}},
     "deep": {"timestamp": "<ISO UTC>", "cycle_id": 122},
     "stop_check": {"checked_at": "<ISO UTC>", "mode": "fast", "cycle_id": 123,
                    "stops_covered": 6, "positions": 6, "unknown": false}}

`stop_check` is the most recent cycle that actually ran the protective-stop
reconciliation (either mode); only such a cycle rewrites it. A cycle that ran
no check (market-closed fast exit, yielded scan) refreshes its own liveness
stamp and leaves it alone — see latest_stop_check().

Alert rules (see check_heartbeat):
    - deep-cycle stamp older than DEEP_MAX_AGE_HOURS, any hour of any day;
    - fast-scan stamp older than FAST_MAX_AGE_MINUTES on weekdays (UTC);
      weekends are exempt because no scans are scheduled then;
    - a position without a resting protective stop at the end of the most
      recent cycle that reported coverage;
    - that cycle could not verify coverage at all (`stops_unknown: true` —
      the open-orders read failed), so nothing may be assumed covered;
    - the most recent cycle started more than LATE_MAX_MINUTES after its
      Task Scheduler slot (a sleeping machine + StartWhenAvailable ran the
      2026-09-16 16:15 deep cycle at 19:19 and the stamp just said "ok");
    - a deep slot was skipped between the previous deep stamp and this one;
    - the analyst circuit was open (`llm_status.state == "circuit_open"`) in
      the deep or the fast stamp: every new entry that cycle fail-closed to
      WAIT, and 2026-09-16/17 showed nobody notices that from logs alone;
    - missing file / corrupt JSON / unusable timestamps.

The stop-coverage / lateness / llm_status keys are optional: stamps written
before they existed, cycles whose open orders were unreadable, and books whose
schedule is not in BOOK_SCHEDULES simply do not carry them. A `degraded` or
`off` llm_status is printed as an [INFO] line only.

Usage:
    python -m agentic_trading.heartbeat --check   # exit 0 ok / 1 alert
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .live_events import ET

ROOT = Path(__file__).resolve().parents[2]
HEARTBEAT_PATH = ROOT / "data" / "heartbeat.json"

# The fast tier is scheduled roughly every 20 minutes, so >35 minutes of
# silence means at least one scan was missed. The deep cycle runs around twice
# per trading day, so a full day (+2h slack) without one means the heavy loop
# is stalled.
FAST_MAX_AGE_MINUTES = 35
DEEP_MAX_AGE_HOURS = 26

# A cycle that starts this long after its slot was not "scheduled", it was
# caught up (StartWhenAvailable after sleep, or a long lock wait). Wide enough
# that a deep cycle waiting out a slow predecessor never trips it.
LATE_MAX_MINUTES = 30

# Task Scheduler triggers per book (ET, weekdays): deep = fixed times,
# fast = (first slot, interval minutes, span minutes). Lockstep with the
# registered tasks (`Get-ScheduledTask AgenticTrading*`) — P2 runs five minutes
# behind P1 so the two books never hit Alpaca at the same second. P1's grid is
# also what live_events / progress use for "next slot". Books not listed here
# (P3 briefing, P4 without credentials) get no lateness fields at all.
BOOK_SCHEDULES: dict[str, dict] = {
    "p1": {"deep": ((9, 45), (16, 15)), "fast": ((9, 35), 20, 6 * 60 + 30)},
    "p2": {"deep": ((9, 50), (16, 20)), "fast": ((10, 10), 20, 5 * 60 + 50)},
}

log = logging.getLogger("heartbeat")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _book_schedule(book_id: str | None = None) -> dict | None:
    if book_id is None:
        from .progress import infer_book_id  # lazy: progress imports live_events, not us

        book_id = infer_book_id()
    return BOOK_SCHEDULES.get(book_id)


def _slots_on(day: datetime, mode: str, schedule: dict) -> list[datetime]:
    """Every scheduled ET slot of `mode` on `day` (empty on weekends)."""
    if day.weekday() >= 5:
        return []
    base = day.replace(second=0, microsecond=0)
    if mode == "deep":
        return [base.replace(hour=h, minute=m) for h, m in schedule["deep"]]
    (h, m), interval, span = schedule["fast"]
    first = base.replace(hour=h, minute=m)
    return [first + timedelta(minutes=k) for k in range(0, span + 1, interval)]


def scheduled_slot_before(mode: str, started_at: datetime,
                          book_id: str | None = None) -> datetime | None:
    """The most recent Task Scheduler slot for `mode` at or before `started_at`
    (returned in ET), or None when this book has no known schedule."""
    schedule = _book_schedule(book_id)
    if schedule is None:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    cursor = started_at.astimezone(ET)
    for _ in range(10):  # a long weekend + holidays is still well inside 10 days
        past = [s for s in _slots_on(cursor, mode, schedule) if s <= started_at]
        if past:
            return max(past)
        cursor = (cursor - timedelta(days=1)).replace(hour=23, minute=59)
    return None


def slots_between(mode: str, earlier: datetime, later: datetime,
                  book_id: str | None = None) -> int | None:
    """How many `mode` slots fall strictly between two slots — the number of
    scheduled runs that never stamped. None when the schedule is unknown."""
    schedule = _book_schedule(book_id)
    if schedule is None or later <= earlier:
        return None if schedule is None else 0
    count = 0
    cursor = earlier.astimezone(ET)
    end = later.astimezone(ET)
    for _ in range(400):  # bounded: a year of trading days at most
        count += sum(1 for s in _slots_on(cursor, mode, schedule) if earlier < s < later)
        cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
        if cursor.date() > end.date():
            break
    return count


def _latest_cycle_id(conn: sqlite3.Connection | None) -> int | None:
    """The newest journaled cycle id, or None when nothing is readable."""
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT id FROM cycles ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row else None


def write_heartbeat(conn: sqlite3.Connection | None, mode: str,
                    path: Path | str | None = None,
                    stop_coverage: tuple[int, int] | None = None,
                    *,
                    stop_coverage_unknown: str | None = None,
                    positions: int | None = None,
                    started_at: datetime | None = None,
                    llm_status: dict | None = None) -> None:
    """Record a liveness stamp for `mode` ("deep" or "fast"), atomically.

    The previous file's other-mode entry is preserved, the new JSON lands via
    a temp file + os.replace (readers never see a half-written file). Best
    effort by contract: any failure is logged and swallowed, because a broken
    heartbeat must never break the trading cycle that is calling it.

    `stop_coverage` is the (covered, total) positions reported by the cycle's
    final stop reconciliation, so the watchdog can alert on a position left
    without a resting stop instead of that fact living only in a log line.
    `stop_coverage_unknown` is the other outcome of that reconciliation: the
    open-orders read failed, so coverage was NOT verified — the stamp then
    carries `stops_unknown: true` (+ reason, + `positions` held) so `--check`
    alerts instead of silently inheriting the previous cycle's "all covered".
    Passing neither means this cycle never ran a reconciliation (the
    market-closed fast exit), which is not the same thing and stays silent.

    `started_at` (UTC) is when the cycle began; the stamp records the slot it
    belonged to, how late it started, and how many slots of this mode were
    skipped since the previous stamp. `llm_status` is run.py's summary of the
    analyst layer this cycle (state / failure category / detail)."""
    try:
        # Resolved here, not in the signature: a default bound at import time
        # cannot be redirected, and every test that calls run_cycle() would keep
        # stamping the live watchdog file (iron rule #6). tests/conftest.py
        # points HEARTBEAT_PATH at a tmp file for the whole suite.
        path = Path(path) if path is not None else HEARTBEAT_PATH
        data: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                pass  # unreadable previous stamp — start a fresh object
        previous = data.get(mode) if isinstance(data.get(mode), dict) else {}
        _migrate_legacy_stop_check(data)
        data[mode] = {
            "timestamp": _utcnow().isoformat(),
            "cycle_id": _latest_cycle_id(conn),
        }
        # Liveness (this stamp) and "the most recent ACTUAL stop check" (the
        # top-level `stop_check` record) are separate on purpose. A cycle that
        # ran no check — market-closed fast exit, yielded scan — refreshes only
        # its liveness stamp and leaves `stop_check` exactly as it was; before
        # this split, rebuilding data[mode] wiped a failed check's
        # `stops_unknown` and the watchdog fell back to the OTHER mode's older
        # "all covered" (review round 2, R2). The per-mode copy is still written
        # for cycles that did check, for readers of the old shape.
        if stop_coverage is not None:
            covered, total = stop_coverage
            data[mode]["stops_covered"] = int(covered)
            data[mode]["positions"] = int(total)
            data["stop_check"] = {
                "checked_at": data[mode]["timestamp"],
                "mode": mode,
                "cycle_id": data[mode]["cycle_id"],
                "stops_covered": int(covered),
                "positions": int(total),
                "unknown": False,
            }
        elif stop_coverage_unknown is not None:
            reason = str(stop_coverage_unknown)[:300]
            held = None if positions is None else int(positions)
            data[mode]["stops_covered"] = None
            data[mode]["positions"] = held
            data[mode]["stops_unknown"] = True
            data[mode]["stops_unknown_reason"] = reason
            data["stop_check"] = {
                "checked_at": data[mode]["timestamp"],
                "mode": mode,
                "cycle_id": data[mode]["cycle_id"],
                "stops_covered": None,
                "positions": held,
                "unknown": True,
                "reason": reason,
            }
        if started_at is not None:
            data[mode].update(_timeliness_block(mode, started_at, previous))
        if llm_status is not None:
            data[mode]["llm_status"] = _llm_status_block(llm_status)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".heartbeat-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp_name, path)  # atomic on the same volume
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        log.info("Heartbeat written: mode=%s cycle_id=%s path=%s",
                 mode, data[mode]["cycle_id"], path)
    except Exception:
        log.warning("Heartbeat update failed for mode=%s (ignored).", mode, exc_info=True)


def _migrate_legacy_stop_check(data: dict) -> None:
    """First write over a file from before the `stop_check` record existed.

    In that format the only trace of the last check is the per-mode coverage
    keys — and the very next line rebuilds `data[mode]`, which for a cycle
    that ran no check would have erased a failed fast check and left the
    watchdog reading the older deep "all covered" (review round 3, R2). So,
    before anything is rebuilt: lift the legacy last check (same rule as the
    reader — newest per-mode stamp with coverage keys, unknown wins a tie)
    into `stop_check` with its ORIGINAL checked_at / mode / result. No check
    in the old data → nothing is invented. Idempotent: a file that already has
    a usable record is left alone."""
    record = data.get("stop_check")
    if isinstance(record, dict) and _parse_timestamp(record.get("checked_at")) is not None:
        return
    legacy = latest_stop_check({k: v for k, v in data.items() if k != "stop_check"})
    if legacy is None:
        return
    data["stop_check"] = {
        "checked_at": legacy["checked_at"],
        "mode": legacy["mode"],
        "cycle_id": legacy["cycle_id"],
        "stops_covered": legacy["stops_covered"],
        "positions": legacy["positions"],
        "unknown": legacy["unknown"],
        "migrated_from": "per-mode stamps",
    }
    if legacy["unknown"]:
        data["stop_check"]["reason"] = legacy["reason"]


def _timeliness_block(mode: str, started_at: datetime, previous: dict) -> dict:
    """started_at / scheduled_slot / late_minutes / missed_slots for a stamp.

    Never raises — an unknown schedule yields just `started_at`, and any
    surprise inside is swallowed so the base stamp still lands."""
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    block: dict = {"started_at": started_at.astimezone(timezone.utc).isoformat()}
    try:
        slot = scheduled_slot_before(mode, started_at)
        if slot is None:
            return block
        block["scheduled_slot"] = slot.astimezone(timezone.utc).isoformat()
        block["late_minutes"] = round((started_at - slot).total_seconds() / 60, 1)
        prev_slot = _parse_timestamp(previous.get("scheduled_slot")) if previous else None
        if prev_slot is not None:
            block["missed_slots"] = slots_between(mode, prev_slot, slot)
    except Exception:
        log.warning("Heartbeat timeliness fields skipped for mode=%s.", mode, exc_info=True)
    return block


def _llm_status_block(raw: dict) -> dict:
    """Only known, short, non-secret fields make it into the stamp."""
    out: dict = {"state": str(raw.get("state") or "unknown")}
    for key in ("category", "reason", "retry_hint"):
        if raw.get(key):
            out[key] = str(raw[key])[:200]
    if raw.get("detail"):
        out["detail"] = str(raw["detail"])[:400]
    for key in ("failures", "calls"):
        if raw.get(key) is not None:
            try:
                out[key] = int(raw[key])
            except (TypeError, ValueError):
                pass
    return out


def _parse_timestamp(raw: object) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _entry_age_seconds(entry: object, now: datetime) -> float | None:
    """Seconds since the sub-entry's timestamp, or None when unusable."""
    if not isinstance(entry, dict):
        return None
    ts = _parse_timestamp(entry.get("timestamp"))
    if ts is None:
        return None
    return (now - ts).total_seconds()


def check_heartbeat(now: datetime | None = None,
                    path: Path | str | None = None) -> str | None:
    """None while the pipeline looks alive, else a human-readable reason.

    Rules: the deep-cycle stamp may be at most DEEP_MAX_AGE_HOURS old at any
    time; the fast-scan stamp at most FAST_MAX_AGE_MINUTES old on weekdays
    (UTC — weekends have no scans scheduled). A missing file, corrupt JSON,
    or unusable per-mode entries always count as abnormal."""
    now = now or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    path = Path(path) if path is not None else HEARTBEAT_PATH
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"心跳文件不存在：{path} —— 从未有任何周期正常结束，或文件被删除。"
    except OSError as exc:
        return f"心跳文件不可读：{path}（{exc}）"
    try:
        data = json.loads(raw)
    except ValueError:
        return f"心跳文件损坏（不是合法 JSON）：{path}"
    if not isinstance(data, dict):
        return f"心跳文件格式异常（顶层不是对象）：{path}"

    problems: list[str] = []

    deep_age_h = _entry_age_seconds(data.get("deep"), now)
    if deep_age_h is None:
        problems.append("缺少深周期（deep）心跳，或其时间戳无法解析 —— 深周期从未正常结束过。")
    elif deep_age_h / 3600 > DEEP_MAX_AGE_HOURS:
        problems.append(
            f"距上次深周期心跳已 {deep_age_h / 3600:.1f} 小时"
            f"（阈值 {DEEP_MAX_AGE_HOURS} 小时）—— 深周期调度可能已停摆。"
        )

    if now.astimezone(timezone.utc).weekday() < 5:  # weekday gate, evaluated in UTC
        fast_age_min = _entry_age_seconds(data.get("fast"), now)
        if fast_age_min is None:
            problems.append("缺少快扫（fast）心跳，或其时间戳无法解析 —— 工作日不应出现这种情况。")
        elif fast_age_min / 60 > FAST_MAX_AGE_MINUTES:
            problems.append(
                f"距上次快扫心跳已 {fast_age_min / 60:.0f} 分钟"
                f"（阈值 {FAST_MAX_AGE_MINUTES} 分钟）—— 快扫调度可能已停摆。"
            )

    problems.extend(_stop_coverage_problems(data))
    problems.extend(_timeliness_problems(data))
    problems.extend(_llm_problems(data))

    return "；".join(problems) if problems else None


_MODE_LABELS = (("deep", "深"), ("fast", "快扫"))


def _newest_entry(data: dict, required_key: str) -> tuple[dict, str] | None:
    """The most recent per-mode stamp that carries `required_key`, with its
    label. Only the newest stamp counts: an older cycle's problem has already
    been superseded by whatever the latest one found."""
    newest: tuple[datetime, dict, str] | None = None
    for mode, label in _MODE_LABELS:
        entry = data.get(mode)
        if not isinstance(entry, dict) or required_key not in entry:
            continue
        ts = _parse_timestamp(entry.get("timestamp"))
        if ts is None:
            continue
        if newest is None or ts > newest[0]:
            newest = (ts, entry, label)
    return (newest[1], newest[2]) if newest else None


def latest_stop_check(data: dict) -> dict | None:
    """The most recent ACTUAL protective-stop check across all modes, or None
    when nothing has ever reported one. This is the single reading both the
    watchdog and the dashboard use (review round 2, R2), normalised to:

        {"checked_at": iso, "mode": "deep"|"fast", "label": "深"|"快扫",
         "cycle_id": ..., "stops_covered": int|None, "positions": int|None,
         "unknown": bool, "reason": str|None, "naked": int}

    Source of truth is the top-level `stop_check` record, which only a cycle
    that ran a reconciliation rewrites. Files written before that record
    existed fall back to the newest per-mode stamp that carries coverage keys;
    on an exact timestamp tie there, an unverified stamp wins (conservative)."""
    labels = dict(_MODE_LABELS)
    record = data.get("stop_check")
    if isinstance(record, dict) and _parse_timestamp(record.get("checked_at")) is not None:
        mode = str(record.get("mode") or "")
        return _normalise_check(
            checked_at=str(record.get("checked_at")), mode=mode, label=labels.get(mode, mode),
            cycle_id=record.get("cycle_id"), covered=record.get("stops_covered"),
            positions=record.get("positions"), unknown=bool(record.get("unknown")),
            reason=record.get("reason"),
        )
    newest: tuple[datetime, dict, str] | None = None
    for mode, label in _MODE_LABELS:
        entry = data.get(mode)
        if not isinstance(entry, dict) or "positions" not in entry:
            continue
        ts = _parse_timestamp(entry.get("timestamp"))
        if ts is None:
            continue
        if newest is None or ts > newest[0] or (ts == newest[0] and entry.get("stops_unknown")):
            newest = (ts, entry, mode)
    if newest is None:
        return None
    ts, entry, mode = newest
    return _normalise_check(
        checked_at=str(entry.get("timestamp")), mode=mode, label=labels[mode],
        cycle_id=entry.get("cycle_id"), covered=entry.get("stops_covered"),
        positions=entry.get("positions"), unknown=bool(entry.get("stops_unknown")),
        reason=entry.get("stops_unknown_reason"),
    )


def _normalise_check(*, checked_at, mode, label, cycle_id, covered, positions, unknown, reason) -> dict:
    def _int(value):
        try:
            return None if value is None else int(value)
        except (TypeError, ValueError):
            return None

    covered_i, positions_i = _int(covered), _int(positions)
    naked = 0
    if not unknown and covered_i is not None and positions_i is not None:
        naked = max(0, positions_i - covered_i)
    return {
        "checked_at": checked_at, "mode": mode, "label": label, "cycle_id": cycle_id,
        "stops_covered": None if unknown else covered_i, "positions": positions_i,
        "unknown": bool(unknown), "reason": (str(reason) if reason else None) if unknown else None,
        "naked": naked,
    }


def _stop_coverage_problems(data: dict) -> list[str]:
    """Stop-coverage alerts from the most recent actual check (latest_stop_check).

      - unverified (`unknown`)  -> alert: the open-orders read failed, so the
        previous check's "all covered" must not be inherited (review R2);
      - verified with naked > 0 -> alert;
      - verified, all covered, or no check ever recorded (older format, or
        only skipped cycles so far) -> nothing.
    A cycle that ran no check never touches the record, so a later skipped
    scan can neither hide a failed check nor resurrect a superseded one."""
    check = latest_stop_check(data)
    if check is None:
        return []
    label = check["label"]
    if check["unknown"]:
        held = check["positions"]
        held_text = f"{held} 个持仓" if held is not None else "持仓"
        reason = check["reason"] or "open orders unreadable"
        return [
            f"最近一次{label}周期没能核验止损覆盖（{reason}）—— {held_text}的保护状态未知，"
            f"上一轮的「全部覆盖」不能沿用。"
        ]
    if check["naked"] <= 0:
        return []
    return [
        f"{check['naked']}/{check['positions']} 个持仓在最近一次{label}周期结束时没有挂上保护性止损单 —— "
        f"这些仓位目前只靠每周期一次的客户端检查兜底。"
    ]


def _stop_coverage_gap(data: dict) -> tuple[int, int, str] | None:
    """(naked, total, label) from the most recent actual check, or None.

    Kept for P3's `p3/watchdog.py`, which composes its own check from these
    helpers. An unverified check yields None here — that watchdog should call
    `_stop_coverage_problems` (or `latest_stop_check`) to see that case too."""
    check = latest_stop_check(data)
    if check is None or check["unknown"] or check["naked"] <= 0:
        return None
    return check["naked"], check["positions"], check["label"]


def _hhmm_et(raw: object) -> str:
    ts = _parse_timestamp(raw)
    return ts.astimezone(ET).strftime("%m-%d %H:%M") if ts else "?"


def _timeliness_problems(data: dict) -> list[str]:
    """Late start of the newest stamp (any mode); skipped slots for deep only.

    A skipped FAST slot is routine — a fast scan that finds the deep cycle
    holding the lock exits without stamping, by design — so it is recorded in
    the stamp for the dashboard but does not page anyone. A skipped DEEP slot
    is a lost decision point and does."""
    problems: list[str] = []
    found = _newest_entry(data, "late_minutes")
    if found is not None:
        entry, label = found
        try:
            late = float(entry["late_minutes"])
        except (TypeError, ValueError):
            late = 0.0
        if late > LATE_MAX_MINUTES:
            problems.append(
                f"最近一次{label}周期比计划槽晚了 {late:.0f} 分钟启动"
                f"（计划 {_hhmm_et(entry.get('scheduled_slot'))} ET，"
                f"实际 {_hhmm_et(entry.get('started_at'))} ET，阈值 {LATE_MAX_MINUTES} 分钟）"
                f"—— 机器可能在休眠后由 StartWhenAvailable 补跑，决策用的是过时的时点。"
            )
    deep = data.get("deep")
    if isinstance(deep, dict):
        try:
            missed = int(deep.get("missed_slots") or 0)
        except (TypeError, ValueError):
            missed = 0
        if missed > 0:
            problems.append(
                f"上一次与最近一次深周期之间漏跑了 {missed} 个计划槽 —— 那些时点没有做任何进出场决策。"
            )
    return problems


def _llm_line(label: str, status: dict) -> str:
    bits = [f"最近一次{label}周期 LLM 分析师状态：{status.get('state') or 'unknown'}"]
    if status.get("category"):
        bits.append(f"类别 {status['category']}")
    if status.get("failures") is not None:
        bits.append(f"失败 {status['failures']} 次")
    tail = status.get("retry_hint") or status.get("reason") or status.get("detail")
    if tail:
        bits.append(str(tail))
    return "，".join(bits)


def _llm_problems(data: dict) -> list[str]:
    """An open analyst circuit is an alert, per mode.

    Checked per mode rather than "newest stamp only": fast scans usually never
    consult the analyst and stamp `off`, which must not mask a deep cycle that
    fail-closed every entry. The deep entry clears itself at the first deep
    cycle whose analyst answers again; the fast entry at the next scan.
    (2026-09-16/17: two days of quant-only with no new entries and nothing
    rang — the operator found out from a forensic read of the logs.)"""
    problems: list[str] = []
    for mode, label in _MODE_LABELS:
        entry = data.get(mode)
        status = entry.get("llm_status") if isinstance(entry, dict) else None
        if isinstance(status, dict) and str(status.get("state")) == "circuit_open":
            problems.append(
                _llm_line(label, status)
                + " —— 分析师熔断，本周期所有新开仓 fail-closed 为 WAIT；只有分析师恢复后的下一个周期才会解除。"
            )
    return problems


def llm_status_note(data: dict) -> str | None:
    """One informational line about the analyst layer from the newest stamp
    that reported it: `degraded` / `off` states. None when it was `ok`, never
    reported, or `circuit_open` (that one is an alert, see _llm_problems)."""
    found = _newest_entry(data, "llm_status")
    if found is None:
        return None
    entry, label = found
    status = entry.get("llm_status")
    if not isinstance(status, dict):
        return None
    if str(status.get("state") or "unknown") in ("ok", "circuit_open"):
        return None
    return _llm_line(label, status)


def _toast_command(title: str, message: str) -> list[str]:
    """The powershell argv that shows a WinRT toast (stdlib only, no modules)."""
    def ps_quote(text: str) -> str:
        return text.replace("'", "''")  # escape PowerShell single quotes

    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
        " ContentType = WindowsRuntime] | Out-Null; "
        "$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        "$texts = $xml.GetElementsByTagName('text'); "
        f"$texts.Item(0).AppendChild($xml.CreateTextNode('{ps_quote(title)}')) | Out-Null; "
        f"$texts.Item(1).AppendChild($xml.CreateTextNode('{ps_quote(message)}')) | Out-Null; "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'agentic-trading').Show([Windows.UI.Notifications.ToastNotification]::new($xml))"
    )
    # EncodedCommand takes base64 UTF-16LE: immune to quoting issues, safe
    # for non-ASCII titles/messages.
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def _notify_toast(message: str) -> None:
    """Best-effort Windows toast; any failure is logged, never raised."""
    try:
        result = subprocess.run(
            _toast_command("Trading 心跳告警", message),
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Toast notification could not be launched: %s", exc)
        return
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        log.warning("Toast notification failed (rc=%d): %s",
                    result.returncode, stderr[-400:])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Watchdog for the trading scheduler's heartbeat file.",
    )
    parser.add_argument("--check", action="store_true",
                        help="Check the heartbeat; exit 0 when fresh, 1 (plus a Windows "
                             "toast) when the pipeline has gone quiet.")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if not args.check:
        parser.print_help()
        raise SystemExit(2)

    problem = check_heartbeat()
    note = _llm_note_from_file(HEARTBEAT_PATH)
    if problem is None:
        print(f"[OK] 心跳正常：快扫与深周期心跳均在阈值内。（{HEARTBEAT_PATH}）")
        if note:
            print(f"[INFO] {note}")
        raise SystemExit(0)
    print(f"[ALERT] 心跳异常：{problem}")
    if note:
        print(f"[INFO] {note}")
    _notify_toast(problem)
    raise SystemExit(1)


def _llm_note_from_file(path: Path) -> str | None:
    """Best effort: the LLM line must never turn a readable check into a crash."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return llm_status_note(data) if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    main()
