"""Round-trip accounting — re-exported from the shared package module.

Kept so ``from .trades import round_trips`` in api.py keeps working without
editing api.py. Implementation: ``agentic_trading.round_trips``.
"""
from __future__ import annotations

from ..round_trips import realized_pnl_timeline, round_trips

__all__ = ["round_trips", "realized_pnl_timeline"]
