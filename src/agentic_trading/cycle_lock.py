"""Cross-process mutual exclusion for live cycles.

The deep twice-daily task and the 20-minute fast scan are separate Windows
scheduled tasks; `MultipleInstances IgnoreNew` only stops a task from
overlapping *itself*, so two run_cycle processes can genuinely collide (a slow
LLM-heavy 9:45 cycle still running at 9:55, for example). They share one
journal and one account, and neither order placement nor TradeIntent
consumption is safe under concurrency.

The lock is an OS-level file lock rather than a lockfile's existence: the
kernel releases it automatically when the owning process dies, so a killed or
timed-out cycle can never leave a stale lock behind. Guarding happens at the
entry points (`run.main`, `scheduler.main`) — programmatic callers of
`run_cycle` (backtests, tests) are sequential by construction and skip it.
"""
from __future__ import annotations

import logging
from pathlib import Path
from time import monotonic as _monotonic, sleep as _sleep

logger = logging.getLogger(__name__)

DEFAULT_LOCK_PATH = Path(__file__).resolve().parents[2] / "data" / "cycle.lock"

# How long a deep cycle waits for a fast scan to finish before giving up. A
# fast scan is seconds of work (batched Alpaca bars, no LLM unless something
# crossed a threshold), so three minutes is many times the expected wait.
DEEP_LOCK_WAIT_SECONDS = 180.0


class CycleLock:
    """Holds an exclusive OS lock on a small file for the duration of one cycle."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else DEFAULT_LOCK_PATH
        self._fh = None

    def acquire(self, timeout: float = 0.0, poll_seconds: float = 2.0) -> bool:
        """Returns False if another process still holds the lock.

        `timeout=0` is the original non-blocking behaviour: fail immediately.
        A positive timeout retries until it elapses, which is what the deep
        cycle wants — it runs twice a day and is the only path that can open a
        position, so it must not lose a race to a 20-minute fast scan that by
        design places no entries. The fast scan keeps the non-blocking form:
        the frequent, expendable side is the one that yields.
        """
        deadline = _monotonic() + max(0.0, timeout)
        while True:
            if self._try_acquire():
                return True
            if _monotonic() >= deadline:
                return False
            _sleep(min(poll_seconds, max(0.0, deadline - _monotonic())))

    def _try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            _lock_file(fh)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            _unlock_file(self._fh)
        except OSError:
            logger.debug("Releasing the cycle lock failed; closing anyway.", exc_info=True)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "CycleLock":
        acquired = self.acquire()
        if not acquired:
            raise BlockingIOError(f"cycle lock already held: {self.path}")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


try:
    import msvcrt
except ImportError:  # non-Windows development fallback
    msvcrt = None

if msvcrt is not None:

    def _lock_file(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(fh) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
