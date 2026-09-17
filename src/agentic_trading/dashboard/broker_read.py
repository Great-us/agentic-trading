"""GET-only Alpaca reader — re-exported from the shared package module.

Kept so ``from .broker_read import BookBrokerReader`` in api.py keeps working
without editing api.py. Implementation: ``agentic_trading.broker_read``.
"""
from __future__ import annotations

from ..broker_read import BookBrokerReader, BrokerError, _load_credentials

__all__ = ["BookBrokerReader", "BrokerError", "_load_credentials"]
