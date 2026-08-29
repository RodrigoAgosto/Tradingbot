"""Configuration loading.

config.yaml holds tunables; secrets come only from environment variables
(sourced from a chmod-600 key file by ops/run_cycle.sh). The private key is
never stored on the Settings object — live execution loads it on demand via
load_private_key(), and the logging layer registers it for redaction.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

PAPER = "paper"
LIVE = "live"


class StrategyConfig(BaseModel):
    min_edge: float = 0.08
    # Sanity gate: an edge this large against a liquid market almost always
    # means OUR data is wrong, unless the observation has already settled
    # the outcome. Skip instead of "winning" a data-error trade.
    max_edge: float = 0.40
    min_confidence: float = 0.6
    max_lead_days: int = 3
    max_slippage: float = 0.02
    min_volume_24h_usd: float = 5000.0     # polymarket: 24h traded volume
    kalshi_min_book_usd: float = 1000.0    # kalshi: resting book notional
    kelly_multiplier: float = 0.25
    exit_edge: float = 0.10


class RiskConfig(BaseModel):
    max_position_frac: float = 0.05
    max_total_exposure_frac: float = 0.40
    max_open_positions: int = 8
    max_city_exposure_frac: float = 0.15
    max_positions_per_cycle: int = 2
    daily_loss_frac: float = 0.15
    bankroll_floor_usd: float = 25.0


class StalenessConfig(BaseModel):
    forecast_max_age_hours: float = 4.0
    market_max_age_seconds: float = 60.0


class ForecastConfig(BaseModel):
    cache_minutes: int = 30
    ensemble_models: list[str] = Field(default_factory=lambda: ["gfs025", "ecmwf_ifs025"])
    nws_user_agent: str = "weatherbot/0.1 (contact: set-your-contact-here)"


class PaperConfig(BaseModel):
    starting_bankroll: float = 1000.0


class HeartbeatConfig(BaseModel):
    url: str | None = None
    timeout_seconds: float = 10.0


class TelegramConfig(BaseModel):
    enabled: bool = False
    chat_id: str | None = None


class EmailConfig(BaseModel):
    enabled: bool = False
    smtp_host: str | None = None
    smtp_port: int = 587
    from_addr: str | None = None
    to_addr: str | None = None


class AlertsConfig(BaseModel):
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)


# Default tradeable cities: top 2 by Polymarket 24h volume per GLOBAL
# timezone (measured 2026-08-27). Hong Kong and Taipei are excluded because
# their markets resolve against CWA/HKO directly (no public METAR resolution
# source). The full supported station universe lives in forecast/stations.py;
# this list only gates which of them the bot will trade.
DEFAULT_CITIES = [
    "Philadelphia",                    # UTC-4 (Kalshi)
    "Los Angeles", "San Francisco",   # UTC-7
    "Denver",                          # UTC-6 (only listed city)
    "Dallas", "Houston",               # UTC-5
    "New York", "Atlanta",             # UTC-4
    "Buenos Aires", "Sao Paulo",       # UTC-3
    "London",                          # UTC+1 (only listed city)
    "Munich", "Paris",                 # UTC+2
    "Ankara", "Tel Aviv",              # UTC+3
    "Beijing", "Kuala Lumpur",         # UTC+8
    "Tokyo", "Seoul",                  # UTC+9
    "Wellington",                      # UTC+12
]


class Settings(BaseModel):
    mode: Literal["paper", "live"] = PAPER
    db_path: str = "weatherbot.db"
    cities: list[str] = Field(default_factory=lambda: list(DEFAULT_CITIES))
    # Market venues to evaluate. "polymarket" (global, crypto, paper-only for
    # US residents) and "kalshi" (CFTC-regulated US exchange).
    venues: list[str] = Field(default_factory=lambda: ["polymarket", "kalshi"])
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    staleness: StalenessConfig = Field(default_factory=StalenessConfig)
    forecast: ForecastConfig = Field(default_factory=ForecastConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)

    @property
    def is_live(self) -> bool:
        return self.mode == LIVE


def load_settings(config_path: str | Path = "config.yaml") -> Settings:
    """Load config.yaml, then apply environment overrides.

    TRADING_MODE defaults to paper; anything other than the exact string
    "live" is treated as paper (fail closed).
    """
    path = Path(config_path)
    raw: dict = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}

    mode = os.environ.get("TRADING_MODE", PAPER).strip().lower()
    raw["mode"] = LIVE if mode == LIVE else PAPER
    return Settings.model_validate(raw)


class KeyFileError(RuntimeError):
    pass


def load_key_env_file() -> Path | None:
    """Load KEY=VALUE lines from the key env file into os.environ.

    The launchd/Task Scheduler wrappers source this file before starting
    Python, but interactive runs (`weatherbot doctor`, `weatherbot cycle`
    by hand) don't go through a wrapper — so the CLI loads it here too.
    Existing environment variables are never overridden. Returns the path
    loaded, or None when no key file exists.
    """
    path = Path(os.environ.get("WEATHERBOT_KEY_FILE") or Path.home() / ".weatherbot.env")
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if name and value:
            os.environ.setdefault(name, value)
    return path


def load_private_key() -> str:
    """Return the trading private key from the environment.

    Only the live executor calls this. Verifies that, when the key was
    provided via a file (POLYMARKET_KEY_FILE), that file is chmod 600.
    The returned value must be passed to logutil.register_secret() by the
    caller before use.
    """
    key_file = os.environ.get("POLYMARKET_KEY_FILE")
    if key_file:
        p = Path(key_file)
        if not p.exists():
            raise KeyFileError(f"POLYMARKET_KEY_FILE does not exist: {key_file}")
        if os.name != "nt":
            perms = stat.S_IMODE(p.stat().st_mode)
            if perms & 0o077:
                raise KeyFileError(
                    f"key file {key_file} permissions are {oct(perms)}; must be 600"
                )
        # On Windows POSIX permission bits are meaningless; the launcher
        # (ops/run_cycle.ps1) verifies the file's NTFS ACL is owner-only
        # before this process starts.

    key = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    if not key:
        raise KeyFileError("POLYMARKET_PRIVATE_KEY is not set")
    return key
