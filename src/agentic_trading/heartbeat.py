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
              "stops_covered": 6, "positions": 6},
     "deep": {"timestamp": "<ISO UTC>", "cycle_id": 122}}

Alert rules (see check_heartbeat):
    - deep-cycle stamp older than DEEP_MAX_AGE_HOURS, any hour of any day;
    - fast-scan stamp older than FAST_MAX_AGE_MINUTES on weekdays (UTC);
      weekends are exempt because no scans are scheduled then;
    - a position without a resting protective stop at the end of the most
      recent cycle that reported coverage;
    - missing file / corrupt JSON / unusable timestamps.

The stop-coverage keys are optional: stamps written before they existed, and
cycles whose open orders were unreadable, simply do not carry them.

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
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HEARTBEAT_PATH = ROOT / "data" / "heartbeat.json"

# The fast tier is scheduled roughly every 20 minutes, so >35 minutes of
# silence means at least one scan was missed. The deep cycle runs around twice
# per trading day, so a full day (+2h slack) without one means the heavy loop
# is stalled.
FAST_MAX_AGE_MINUTES = 35
DEEP_MAX_AGE_HOURS = 26

log = logging.getLogger("heartbeat")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
                    stop_coverage: tuple[int, int] | None = None) -> None:
    """Record a liveness stamp for `mode` ("deep" or "fast"), atomically.

    The previous file's other-mode entry is preserved, the new JSON lands via
    a temp file + os.replace (readers never see a half-written file). Best
    effort by contract: any failure is logged and swallowed, because a broken
    heartbeat must never break the trading cycle that is calling it.

    `stop_coverage` is the (covered, total) positions reported by the cycle's
    final stop reconciliation, so the watchdog can alert on a position left
    without a resting stop instead of that fact living only in a log line."""
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
        data[mode] = {
            "timestamp": _utcnow().isoformat(),
            "cycle_id": _latest_cycle_id(conn),
        }
        if stop_coverage is not None:
            covered, total = stop_coverage
            data[mode]["stops_covered"] = int(covered)
            data[mode]["positions"] = int(total)
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

    gap = _stop_coverage_gap(data)
    if gap is not None:
        naked, total, mode = gap
        problems.append(
            f"{naked}/{total} 个持仓在最近一次{mode}周期结束时没有挂上保护性止损单 —— "
            f"这些仓位目前只靠每周期一次的客户端检查兜底。"
        )

    return "；".join(problems) if problems else None


def _stop_coverage_gap(data: dict) -> tuple[int, int, str] | None:
    """(naked, total, label) from the most recent stamp that reported coverage.

    Only the newest stamp counts: an older cycle's gap has already been
    superseded by whatever the latest reconciliation found."""
    newest: tuple[datetime, dict, str] | None = None
    for mode, label in (("deep", "深"), ("fast", "快扫")):
        entry = data.get(mode)
        if not isinstance(entry, dict) or "positions" not in entry:
            continue
        ts = _parse_timestamp(entry.get("timestamp"))
        if ts is None:
            continue
        if newest is None or ts > newest[0]:
            newest = (ts, entry, label)
    if newest is None:
        return None  # nothing has reported coverage yet (older stamp format)
    _, entry, label = newest
    try:
        total = int(entry["positions"])
        covered = int(entry.get("stops_covered", 0))
    except (TypeError, ValueError):
        return None
    naked = total - covered
    return (naked, total, label) if naked > 0 else None


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
    if problem is None:
        print(f"[OK] 心跳正常：快扫与深周期心跳均在阈值内。（{HEARTBEAT_PATH}）")
        raise SystemExit(0)
    print(f"[ALERT] 心跳异常：{problem}")
    _notify_toast(problem)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
