"""Loads environment variables and YAML config into a single Settings object."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


@dataclass
class RiskConfig:
    risk_per_trade_pct: float
    max_position_pct: float
    min_position_pct: float
    max_total_exposure_pct: float
    max_open_positions: int
    max_new_orders_per_cycle: int
    min_quant_score_to_consider: float
    atr_stop_multiple: float
    min_stop_pct: float
    max_stop_pct: float
    trailing_stop_pct: float
    risk_off_size_multiplier: float
    risk_off_score_penalty: float
    escalation_cooldown_minutes: float
    escalation_cooldown_score_delta: float
    min_intraday_confirm_scans: int = 2
    allow_intraday_entries: bool = False
    take_profit_pct: float | None = None  # None disables the hard target
    buy_threshold: float = 0.25
    sell_threshold: float = -0.25
    min_adv_usd: float = 20_000_000.0
    max_entry_gap_atr: float = 0.75
    # 0 disables. Caps sum(position_value × stop_distance) as a fraction of equity.
    max_portfolio_stop_risk_pct: float = 0.06
    corr_lookback: int = 60
    corr_penalty_threshold: float = 0.75  # average 60d return corr vs holdings
    corr_size_multiplier: float = 0.5     # 1.0 disables the haircut
    # Regime-dependent cap on invested / equity for NEW entries. TRIM of the
    # open book is separate and only runs for labels in trim_regimes.
    regime_max_exposure: dict[str, float] = field(default_factory=lambda: {
        "risk_on": 1.00, "neutral": 0.80, "risk_off": 0.55, "unknown": 0.00,
    })
    max_sector_pct: dict[str, float] = field(default_factory=lambda: {
        "Technology": 0.35,
        "Communication Services": 0.30,
        "Healthcare": 0.30,
        "Energy": 0.25,
        "Financial Services": 0.30,
        "Consumer Cyclical": 0.30,
        "Consumer Defensive": 0.25,
    })
    default_max_sector_pct: float = 0.35
    trim_regimes: list[str] = field(default_factory=lambda: ["risk_off"])
    # Optional cross-sector basket cap used by research pressure tests (for
    # example the Magnificent Seven).  Empty symbols + a 100% cap make this a
    # no-op for the live strategy unless explicitly overridden.
    theme_symbols: list[str] = field(default_factory=list)
    max_theme_pct: float = 1.0
    max_theme_positions: int = 1_000_000
    # Permanent cash reserve: effective exposure cap = min(regime cap, 1 - buffer).
    # The account is never deployed past this, whatever the regime table says.
    min_cash_buffer_pct: float = 0.05
    # Entry execution window (US/Eastern, HH:MM). BUY decisions queue as
    # TradeIntents and only execute inside this window, skipping the open's
    # first half hour — the widest-spread, most chaotic tape of the day.
    entry_window_start_et: str = "10:00"
    entry_window_end_et: str = "15:30"
    # Chase guards applied when an intent executes: live price vs today's open,
    # and live price vs the signal-time price. Above either, the intent waits
    # for a later scan instead of buying the top of the move.
    max_chase_vs_open_pct: float = 0.015
    max_chase_vs_signal_pct: float = 0.010
    # A BUY whose LLM verdict is missing (analyst crashed mid-cycle) is
    # downgraded to WAIT. --skip-llm is an explicit operator choice and still
    # allows quant-only entries.
    require_llm_for_entry: bool = True
    # TRIM sell buffers: the regime must be in trim_regimes AND its score
    # beyond trim_trigger_score for trim_confirm_cycles consecutive deep-cycle
    # readings before the existing book is sold toward the regime target.
    # Gates dispositions only — new-entry risk-off tightening stays immediate.
    trim_trigger_score: float = -0.30
    trim_confirm_cycles: int = 2
    # Exit bar for holdings that are no longer on the core watchlist (left over
    # from a pool swap). They are not part of the book under validation, so they
    # hold their capital on a stricter bar than sell_threshold: a name that has
    # stopped earning its exposure leaves instead of parking the risk budget.
    # Applied only on the off-watchlist path, where the LLM is not consulted, so
    # combined == quant there and this reads as either.
    legacy_sell_threshold: float = 0.10


@dataclass
class Settings:
    alpaca_api_key: str | None
    alpaca_secret_key: str | None
    alpaca_base_url: str
    moonshot_api_key: str | None
    analyst_provider: str          # "cli" (local agent CLI) or "api" (pay-per-token HTTP)
    analyst_cli_path: str | None   # only used when analyst_provider == "cli"
    analyst_cli_timeout: int
    analyst_cli_home: str | None   # KIMI_CODE_HOME override that pins reasoning effort (Kimi Code only)
    analyst_cli_model: str | None  # CLI-side alias, e.g. "kimi-code/k3-max" or "opus"
    analyst_model: str             # API-side model id, e.g. "kimi-k3"
    analyst_cli_extra_args: str | None = None  # raw CLI-specific flags
    grok_enabled: bool = False
    grok_cli_path: str | None = None
    grok_timeout_seconds: int = 60
    grok_reasoning_effort: str = "low"
    watchlist: list[str] = field(default_factory=list)          # tradeable: core + research, deduped
    core_watchlist: list[str] = field(default_factory=list)     # hand-maintained tradeable list
    context_symbols: list[str] = field(default_factory=list)    # regime refs, never bought
    research_symbols: list[str] = field(default_factory=list)   # vault-sourced, may overlap core
    sectors: dict[str, str] = field(default_factory=dict)       # symbol → Yahoo-style sector
    risk: RiskConfig = None

    @property
    def has_alpaca_credentials(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @property
    def analyst_available(self) -> tuple[bool, str]:
        """(usable, reason-if-not) for the configured analyst backend."""
        if self.analyst_provider == "cli":
            if not self.analyst_cli_path:
                return False, "ANALYST_CLI_PATH is not set in .env"
            if not Path(self.analyst_cli_path).exists():
                return False, f"analyst CLI not found at {self.analyst_cli_path}"
            return True, ""
        if not self.moonshot_api_key:
            return False, "MOONSHOT_API_KEY is not set in .env"
        return True, ""

    @property
    def grok_available(self) -> tuple[bool, str]:
        """(usable, reason-if-not) for the Grok sentiment-confirmation gate."""
        if not self.grok_enabled:
            return False, "GROK_ENABLED is not true in .env"
        if not self.grok_cli_path:
            return False, "GROK_CLI_PATH is not set in .env"
        if not Path(self.grok_cli_path).exists():
            return False, f"Grok CLI not found at {self.grok_cli_path}"
        return True, ""

    @property
    def is_paper(self) -> bool:
        # Safety: this project only ever trades through Alpaca's paper endpoint.
        # Going live would require deliberately changing this check AND the
        # broker client, not just editing .env.
        return "paper" in self.alpaca_base_url


def validate_risk(risk: RiskConfig) -> None:
    """Rejects nonsensical risk.yaml values instead of silently amplifying size.

    A typo like `risk_per_trade_pct: 2` (meaning 2%) would otherwise set the
    per-trade budget to 200% of equity — every downstream cap is itself just
    config, so nothing would catch it. Fail loudly at load time."""
    def _fail(field: str, value, why: str) -> None:
        raise ValueError(f"risk.yaml: {field}={value!r} {why}")

    fractions = [
        "risk_per_trade_pct", "max_position_pct", "min_position_pct",
        "max_total_exposure_pct", "min_stop_pct", "max_stop_pct",
        "trailing_stop_pct", "default_max_sector_pct",
    ]
    for field in fractions:
        value = float(getattr(risk, field))
        if not 0.0 < value <= 1.0:
            _fail(field, getattr(risk, field), "must be a fraction of equity in (0, 1] — write 2% as 0.02")
    if risk.min_position_pct > risk.max_position_pct:
        _fail("min_position_pct", risk.min_position_pct, "cannot exceed max_position_pct")
    if risk.min_stop_pct > risk.max_stop_pct:
        _fail("min_stop_pct", risk.min_stop_pct, "cannot exceed max_stop_pct")

    if risk.take_profit_pct is not None and not 0.0 < float(risk.take_profit_pct) <= 1.0:
        _fail("take_profit_pct", risk.take_profit_pct, "must be null or a fraction in (0, 1]")

    if not 0.0 <= risk.buy_threshold <= 1.0:
        _fail("buy_threshold", risk.buy_threshold, "must be in [0, 1]")
    if not -1.0 <= risk.sell_threshold <= 0.0:
        _fail("sell_threshold", risk.sell_threshold, "must be in [-1, 0]")
    legacy_sell = float(getattr(risk, "legacy_sell_threshold", 0.0))
    if not -1.0 <= legacy_sell <= 1.0:
        _fail("legacy_sell_threshold", risk.legacy_sell_threshold, "must be a score in [-1, 1]")
    if legacy_sell < risk.sell_threshold:
        _fail("legacy_sell_threshold", risk.legacy_sell_threshold,
              "cannot be below sell_threshold — off-watchlist holdings exit on a stricter bar, not a looser one")
    if risk.min_quant_score_to_consider < 0.0 or risk.min_quant_score_to_consider > risk.buy_threshold:
        _fail("min_quant_score_to_consider", risk.min_quant_score_to_consider,
              "must be >= 0 and not above buy_threshold")

    for field, minimum in (("max_open_positions", 1), ("max_new_orders_per_cycle", 1),
                           ("min_intraday_confirm_scans", 1)):
        if int(getattr(risk, field)) < minimum:
            _fail(field, getattr(risk, field), f"must be an integer >= {minimum}")
    if float(risk.escalation_cooldown_minutes) <= 0 or float(risk.escalation_cooldown_score_delta) <= 0:
        _fail("escalation_cooldown_minutes", risk.escalation_cooldown_minutes, "cooldown fields must be positive")

    if not 0.0 < float(risk.risk_off_size_multiplier) <= 1.0:
        _fail("risk_off_size_multiplier", risk.risk_off_size_multiplier, "must be in (0, 1]")
    if float(risk.risk_off_score_penalty) < 0.0:
        _fail("risk_off_score_penalty", risk.risk_off_score_penalty, "cannot be negative")
    if not 0.0 < float(risk.corr_size_multiplier) <= 1.0:
        _fail("corr_size_multiplier", risk.corr_size_multiplier, "must be in (0, 1]; use 1.0 to disable")
    if not -1.0 <= float(risk.corr_penalty_threshold) <= 1.0:
        _fail("corr_penalty_threshold", risk.corr_penalty_threshold, "must be a correlation in [-1, 1]")
    if int(risk.corr_lookback) < 10:
        _fail("corr_lookback", risk.corr_lookback, "too short to estimate correlation")

    port_cap = float(getattr(risk, "max_portfolio_stop_risk_pct", 0.0))
    if not 0.0 <= port_cap <= 1.0:
        _fail("max_portfolio_stop_risk_pct", risk.max_portfolio_stop_risk_pct, "must be a fraction in [0, 1]")
    if float(getattr(risk, "max_entry_gap_atr", 1.0)) <= 0.0:
        _fail("max_entry_gap_atr", risk.max_entry_gap_atr, "must be positive")
    if float(getattr(risk, "min_adv_usd", 0.0)) < 0.0:
        _fail("min_adv_usd", risk.min_adv_usd, "cannot be negative")

    for label, cap in (getattr(risk, "regime_max_exposure", {}) or {}).items():
        if not 0.0 <= float(cap) <= 1.0:
            _fail(f"regime_max_exposure.{label}", cap, "must be a fraction in [0, 1]")
    for sector, cap in (getattr(risk, "max_sector_pct", {}) or {}).items():
        if not 0.0 <= float(cap) <= 1.0:
            _fail(f"max_sector_pct.{sector}", cap, "must be a fraction in [0, 1]")

    buffer = float(getattr(risk, "min_cash_buffer_pct", 0.0))
    if not 0.0 <= buffer <= 0.5:
        _fail("min_cash_buffer_pct", risk.min_cash_buffer_pct, "must be a fraction in [0, 0.5]")
    trigger = float(getattr(risk, "trim_trigger_score", -1.0))
    if not -1.0 <= trigger < 0.0:
        _fail("trim_trigger_score", risk.trim_trigger_score, "must be a negative regime score in [-1, 0)")
    if int(getattr(risk, "trim_confirm_cycles", 1)) < 1:
        _fail("trim_confirm_cycles", risk.trim_confirm_cycles, "must be an integer >= 1")
    for field_name in ("max_chase_vs_open_pct", "max_chase_vs_signal_pct"):
        value = float(getattr(risk, field_name, 0.0))
        if not 0.0 < value <= 0.2:
            _fail(field_name, getattr(risk, field_name), "must be a fraction in (0, 0.2]")
    try:
        window_start = datetime.strptime(risk.entry_window_start_et, "%H:%M").time()
        window_end = datetime.strptime(risk.entry_window_end_et, "%H:%M").time()
    except (TypeError, ValueError):
        _fail("entry_window_start_et", risk.entry_window_start_et,
              "and entry_window_end_et must be 24-hour HH:MM times")
    else:
        if window_start >= window_end:
            _fail("entry_window_start_et", risk.entry_window_start_et,
                  "must be earlier than entry_window_end_et")


def load_settings(config_dir: Path | None = None, *, include_research: bool = True) -> Settings:
    config_dir = config_dir or (ROOT / "config")

    def _norm_symbols(raw: list) -> list[str]:
        # A stray lowercase or padded symbol silently matches nothing upstream;
        # normalize here so every consumer sees canonical tickers.
        return [str(s).strip().upper() for s in raw if s]

    with open(config_dir / "watchlist.yaml", "r", encoding="utf-8") as f:
        watchlist_raw = yaml.safe_load(f)
    raw_context = _norm_symbols(watchlist_raw.get("context_symbols", []) or [])
    context_set = set(raw_context)
    core_watchlist = [s for s in _norm_symbols(watchlist_raw.get("symbols", []) or []) if s not in context_set]
    context_symbols = raw_context

    with open(config_dir / "risk.yaml", "r", encoding="utf-8") as f:
        risk_raw = yaml.safe_load(f)
    risk = RiskConfig(**risk_raw)
    validate_risk(risk)

    sectors: dict[str, str] = {}
    sectors_path = config_dir / "sectors.yaml"
    if sectors_path.exists():
        with open(sectors_path, "r", encoding="utf-8") as f:
            raw_sectors = yaml.safe_load(f) or {}
        sectors = {str(k): str(v) for k, v in raw_sectors.items() if k and v}

    research_symbols: list[str] = []
    research_path = config_dir / "research.yaml"
    if include_research and research_path.exists():
        with open(research_path, "r", encoding="utf-8") as f:
            research_raw = yaml.safe_load(f) or {}
        if research_raw.get("enabled", False):
            from .research.obsidian_scanner import scan_vault

            vault_dir = Path(research_raw["obsidian_vault_path"])
            result = scan_vault(vault_dir, research_raw.get("obsidian_subfolders"))
            excluded = set(research_raw.get("exclude_symbols", []))
            research_symbols = [s for s in result.symbols if s not in excluded]

    context_set = set(context_symbols)
    research_symbols = [s for s in research_symbols if s not in context_set]
    watchlist = list(core_watchlist)
    for symbol in research_symbols:
        if symbol not in watchlist:
            watchlist.append(symbol)

    def _env_int(name: str, default: str) -> int:
        raw = os.getenv(name, default)
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ValueError(f".env: {name}={raw!r} is not an integer") from None

    return Settings(
        alpaca_api_key=os.getenv("ALPACA_API_KEY") or None,
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY") or None,
        alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        moonshot_api_key=os.getenv("MOONSHOT_API_KEY") or None,
        analyst_provider=os.getenv("ANALYST_PROVIDER", "cli").strip().lower(),
        analyst_cli_path=os.getenv("ANALYST_CLI_PATH") or None,
        analyst_cli_timeout=_env_int("ANALYST_CLI_TIMEOUT", "180"),
        analyst_cli_home=os.getenv("ANALYST_CLI_HOME") or None,
        analyst_cli_model=os.getenv("ANALYST_CLI_MODEL") or None,
        analyst_cli_extra_args=os.getenv("ANALYST_CLI_EXTRA_ARGS") or None,
        analyst_model=os.getenv("ANALYST_MODEL", "kimi-k3"),
        grok_enabled=os.getenv("GROK_ENABLED", "false").strip().lower() == "true",
        grok_cli_path=os.getenv("GROK_CLI_PATH") or None,
        grok_timeout_seconds=_env_int("GROK_TIMEOUT_SECONDS", "60"),
        grok_reasoning_effort=os.getenv("GROK_REASONING_EFFORT", "low"),
        watchlist=watchlist,
        core_watchlist=core_watchlist,
        context_symbols=context_symbols,
        research_symbols=research_symbols,
        sectors=sectors,
        risk=risk,
    )
