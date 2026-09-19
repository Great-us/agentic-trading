"""Suite-wide isolation from live state.

Iron rule #6 (AGENTS.md): tests and backtests must never write to the real
`data/` paths. `run_cycle(asof=None)` stamps the watchdog heartbeat at both of
its normal exits, and most execution tests call it with an in-memory journal but
no path override — so before this fixture existed, running the suite rewrote the
live `data/heartbeat.json`. That is worse than untidy: the stamp is what
`heartbeat --check` trusts, so a test run left the watchdog believing a deep
cycle had just finished (with `positions: 0`) and would have suppressed a real
staleness alert for the next 26 hours.
"""
from __future__ import annotations

import pytest

from agentic_trading import heartbeat as heartbeat_mod
from agentic_trading import live_events as live_events_mod
from agentic_trading import progress as progress_mod
from agentic_trading import run as run_mod


@pytest.fixture(autouse=True)
def isolate_heartbeat(tmp_path, monkeypatch):
    """Point the heartbeat file at a per-test tmp path.

    Both writer and checker resolve HEARTBEAT_PATH at call time, so patching the
    module attribute is enough; tests that pass an explicit path are unaffected.
    """
    monkeypatch.setattr(heartbeat_mod, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(live_events_mod, "LIVE_EVENTS_PATH", tmp_path / "live_events.jsonl")
    monkeypatch.setattr(progress_mod, "PROGRESS_PATH", tmp_path / "progress.json")


class _NoNetworkBrokerReader:
    """Stand-in for BookBrokerReader so the fills-vs-positions reconciliation
    added to _stamp_cycle_progress (PROGRESS.md P0-A-3) never reaches the real
    Alpaca paper API from a test. Any test exercising that reconciliation
    specifically should monkeypatch agentic_trading.run.BookBrokerReader
    itself, which simply overrides this fixture's patch."""

    def __init__(self, _book_root):
        pass

    def fills(self):
        return None, "network disabled in tests"


@pytest.fixture(autouse=True)
def isolate_broker_reads(monkeypatch):
    monkeypatch.setattr(run_mod, "BookBrokerReader", _NoNetworkBrokerReader)
