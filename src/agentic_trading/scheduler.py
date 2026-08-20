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

from .run import _setup_logging, run_cycle

ET = ZoneInfo("America/New_York")

# 09:45 rather than pre-market, deliberately. Notional orders only fill during
# regular hours, so a pre-market cycle leaves its buys queued — and the
# protective-stop reconciliation at the end of that cycle would then see no
# position to protect, leaving the day's new entries naked until the next run.
# Running after the open lets a buy fill and get its stop in the same cycle.
# The 16:15 run reacts to the close; its orders queue for the next session.
RUN_TIMES_ET = [(9, 45), (16, 15)]
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
                    run_cycle()
                except Exception:
                    log.exception("Scheduled run_cycle failed.")
                last_run_key = key
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
