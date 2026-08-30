"""Long-running daemon that fires a decision cycle after the open and after the
close, Mon-Fri, on US Eastern time regardless of the host machine's timezone.

For actual unattended operation on Windows, a Task Scheduler entry that runs
`python -m agentic_trading.run` at fixed times is more robust than keeping this
process alive continuously — see README. This script is the simpler option for
running on a machine you're keeping on anyway.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .cycle_lock import DEEP_LOCK_WAIT_SECONDS, CycleLock
from .run import RUN_TIMES_ET, _setup_logging, run_cycle

ET = ZoneInfo("America/New_York")

POLL_SECONDS = 30


def main() -> None:
    _setup_logging()
    log = logging.getLogger("scheduler")
    log.info("Scheduler started — runs at %s ET on weekdays.", RUN_TIMES_ET)

    last_run_key: tuple | None = None
    while True:
        now = datetime.now(ET)
        if now.weekday() < 5 and (now.hour, now.minute) in RUN_TIMES_ET:
            key = (now.date(), now.hour, now.minute)
            if key != last_run_key:
                log.info("Triggering scheduled run at %s ET.", now.strftime("%H:%M"))
                try:
                    lock = CycleLock()
                    # Same priority rule as run.main: this is a deep cycle, so
                    # it waits for a fast scan rather than forfeiting the slot.
                    if lock.acquire(timeout=DEEP_LOCK_WAIT_SECONDS):
                        try:
                            run_cycle()
                        finally:
                            lock.release()
                    else:
                        log.error(
                            "DEEP CYCLE SKIPPED — waited %.0fs for the cycle lock and never "
                            "got it. No decisions were made at this firing.",
                            DEEP_LOCK_WAIT_SECONDS,
                        )
                except Exception:
                    log.exception("Scheduled run_cycle failed.")
                last_run_key = key
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
