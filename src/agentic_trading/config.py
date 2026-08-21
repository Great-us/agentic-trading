"""Loads environment variables and YAML config into a single Settings object."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
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


@dataclass
class Settings:
    alpaca_api_key: str | None
    alpaca_secret_key: str | None
    alpaca_base_url: str
    moonshot_api_key: str | None
    analyst_provider: str          # "cli" (local agent CLI) or "api" (pay-per-token HTTP)
    analyst_cli_path: str | None   # only used when analyst_provider == "cli"
    analyst_cli_timeout: int
    analyst_cli_home: str | None   # KIMI_CODE_HOME override that pins reasoning effort
    analyst_cli_model: str | None  # CLI-side alias, e.g. "kimi-code/k3-max"
    analyst_model: str             # API-side model id, e.g. "kimi-k3"
    grok_enabled: bool = False
    grok_cli_path: str | None = None
    grok_timeout_seconds: int = 60
    grok_reasoning_effort: str = "low"
    watchlist: list[str] = field(default_factory=list)          # core + research, deduped
    core_watchlist: list[str] = field(default_factory=list)     # hand-maintained list only
    research_symbols: list[str] = field(default_factory=list)   # vault-sourced, may overlap core
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


def load_settings(config_dir: Path | None = None, *, include_research: bool = True) -> Settings:
    config_dir = config_dir or (ROOT / "config")

    with open(config_dir / "watchlist.yaml", "r", encoding="utf-8") as f:
        watchlist_raw = yaml.safe_load(f)
    core_watchlist = list(watchlist_raw.get("symbols", []))

    with open(config_dir / "risk.yaml", "r", encoding="utf-8") as f:
        risk_raw = yaml.safe_load(f)
    risk = RiskConfig(**risk_raw)

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

    watchlist = list(core_watchlist)
    for symbol in research_symbols:
        if symbol not in watchlist:
            watchlist.append(symbol)

    return Settings(
        alpaca_api_key=os.getenv("ALPACA_API_KEY") or None,
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY") or None,
        alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        moonshot_api_key=os.getenv("MOONSHOT_API_KEY") or None,
        analyst_provider=os.getenv("ANALYST_PROVIDER", "cli").strip().lower(),
        analyst_cli_path=os.getenv("ANALYST_CLI_PATH") or None,
        analyst_cli_timeout=int(os.getenv("ANALYST_CLI_TIMEOUT", "180")),
        analyst_cli_home=os.getenv("ANALYST_CLI_HOME") or None,
        analyst_cli_model=os.getenv("ANALYST_CLI_MODEL") or None,
        analyst_model=os.getenv("ANALYST_MODEL", "kimi-k3"),
        grok_enabled=os.getenv("GROK_ENABLED", "false").strip().lower() == "true",
        grok_cli_path=os.getenv("GROK_CLI_PATH") or None,
        grok_timeout_seconds=int(os.getenv("GROK_TIMEOUT_SECONDS", "60")),
        grok_reasoning_effort=os.getenv("GROK_REASONING_EFFORT", "low"),
        watchlist=watchlist,
        core_watchlist=core_watchlist,
        research_symbols=research_symbols,
        risk=risk,
    )
