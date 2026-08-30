"""Provider-agnostic entry point for the qualitative analyst layer.

Two backends, same AnalystVerdict out:
  - "cli": drives a locally-installed agent CLI (Kimi Code, Claude Code) in its
    non-interactive prompt mode, spending an existing coding-plan subscription.
  - "api": calls an OpenAI-compatible endpoint with a pay-per-token key.

Either way, a failure returns None and the decision engine falls back to
quant-only rather than treating the absence of a read as a signal.
"""
from __future__ import annotations

import logging

from ..config import Settings
from ..data.market_data import NewsItem
from ..signals.technical import QuantSignal
from .schema import AnalystVerdict  # re-exported for callers

logger = logging.getLogger(__name__)

__all__ = ["AnalystVerdict", "analyze"]


def analyze(
    signal: QuantSignal,
    news: list[NewsItem],
    fundamentals: dict,
    settings: Settings,
) -> AnalystVerdict | None:
    if settings.analyst_provider == "cli":
        from .cli_provider import analyze_via_cli

        return analyze_via_cli(
            signal, news, fundamentals,
            cli_path=settings.analyst_cli_path,
            timeout=settings.analyst_cli_timeout,
            model=settings.analyst_cli_model,
            cli_home=settings.analyst_cli_home,
            extra_args=settings.analyst_cli_extra_args,
        )

    from .api_provider import analyze_via_api

    return analyze_via_api(
        signal, news, fundamentals,
        api_key=settings.moonshot_api_key,
        model=settings.analyst_model,
    )
