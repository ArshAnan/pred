"""Load `.env` and `config.yaml` into one Settings object.

The model is the CP0 contract (LLD section 9, CP0_DECISIONS R4).
`load_settings()` raises ConfigError when SEC_USER_AGENT is missing.
Unknown keys inside a section are ignored so another lane can add a key
without editing this file. An unknown top-level section is an error.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from airscen.types import ConfigError


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CompanyConfig(_Section):
    ticker: str
    cik: Optional[int] = None
    name: str
    peers: list[str]


class PollConfig(_Section):
    market_s: int = 60
    filings_s: int = 1800
    macro_s: int = 21600
    stale_after_s: int = 300


class TrustWeights(_Section):
    spread: float = 0.35
    depth: float = 0.25
    volume: float = 0.25
    time: float = 0.15


class VolumeRef(_Section):
    kalshi: float = 10_000
    polymarket: float = 100_000


class TrustConfig(_Section):
    weights: TrustWeights = Field(default_factory=TrustWeights)
    floor: float = 0.40
    backfill_cap: float = 0.50
    spread_max: float = 0.10
    depth_ref: float = 1000
    volume_ref: VolumeRef = Field(default_factory=VolumeRef)
    min_days: float = 2


class FuelConfig(_Section):
    horizon_days: int = 100
    wti_ref_override: Optional[float] = None
    weights: tuple[float, float, float] = (0.3, 0.4, 0.3)


class ScenarioConfig(_Section):
    liquidity_cover_min: float = 1.0
    rate_hike_bp: int = 25


class WatchInputs(_Section):
    note_size_usd_m: float = 1000
    tenor_years: int = 7
    coupon_spread_bp: float = 150
    coverage_threshold: float = 3.0
    fuel_passthrough: float = 1.0


class AlertsConfig(_Section):
    watch: list[str] = Field(default_factory=lambda: ["DAL"])
    watch_inputs: WatchInputs = Field(default_factory=WatchInputs)
    breach_threshold: float = 0.20
    hysteresis: float = 0.02
    driver_move_pp: float = 10
    window_min: int = 60
    cooldown_min: int = 60


class LLMModels(_Section):
    snowflake: str = "claude-haiku-4-5"
    gemini: str = "<current Flash model id>"
    ollama: str = "<installed instruct model>"


class LLMConfig(_Section):
    max_calls_per_hour: int = 120
    max_input_tokens: int = 20_000
    models: LLMModels = Field(default_factory=LLMModels)


def _default_companies() -> list[CompanyConfig]:
    return [
        CompanyConfig(ticker="DAL", cik=27904, name="Delta Air Lines", peers=["UAL", "AAL"]),
        CompanyConfig(ticker="UAL", cik=100517, name="United Airlines Holdings", peers=["DAL", "AAL"]),
        CompanyConfig(ticker="AAL", cik=6201, name="American Airlines Group", peers=["DAL", "UAL"]),
    ]


class Settings(BaseModel):
    """Validated settings. Defaults match LLD section 9 and `config.yaml`."""

    model_config = ConfigDict(extra="forbid")

    sec_user_agent: str = ""
    fred_api_key: str = ""
    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    slack_webhook_url: str = ""
    snowflake_account: str = ""
    snowflake_pat: str = ""
    gemini_api_key: str = ""
    ollama_host: str = "http://127.0.0.1:11434"
    llm_mode: Literal["live", "cache_only", "off"] = "live"
    llm_chain: str = "snowflake,gemini,ollama"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_base_url: str = "http://127.0.0.1:8000/api/v1"
    api_token: str = ""
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""

    companies: list[CompanyConfig] = Field(default_factory=_default_companies)
    poll: PollConfig = Field(default_factory=PollConfig)
    trust: TrustConfig = Field(default_factory=TrustConfig)
    fuel: FuelConfig = Field(default_factory=FuelConfig)
    scenario: ScenarioConfig = Field(default_factory=ScenarioConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)


_ENV_FIELDS = {
    "SEC_USER_AGENT": "sec_user_agent",
    "FRED_API_KEY": "fred_api_key",
    "ALPACA_KEY_ID": "alpaca_key_id",
    "ALPACA_SECRET_KEY": "alpaca_secret_key",
    "SLACK_WEBHOOK_URL": "slack_webhook_url",
    "SNOWFLAKE_ACCOUNT": "snowflake_account",
    "SNOWFLAKE_PAT": "snowflake_pat",
    "GEMINI_API_KEY": "gemini_api_key",
    "OLLAMA_HOST": "ollama_host",
    "LLM_MODE": "llm_mode",
    "LLM_CHAIN": "llm_chain",
    "API_HOST": "api_host",
    "API_PORT": "api_port",
    "API_BASE_URL": "api_base_url",
    "API_TOKEN": "api_token",
    "KALSHI_API_KEY_ID": "kalshi_api_key_id",
    "KALSHI_PRIVATE_KEY_PATH": "kalshi_private_key_path",
}


def _discover_config(start: Path) -> Path:
    for directory in [start, *start.parents]:
        candidate = directory / "config.yaml"
        if candidate.is_file():
            return candidate
    raise ConfigError("config.yaml was not found; set it next to the working directory")


def _env_overlay(source: Mapping[str, str]) -> dict:
    overlay: dict = {}
    for env_name, field in _ENV_FIELDS.items():
        if env_name not in source:
            continue
        raw = source[env_name]
        if raw is None or str(raw).strip() == "":
            continue
        overlay[field] = raw
    return overlay


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err.get("loc", ())) or "settings"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "Invalid settings: " + "; ".join(parts)


def _provider_ready(name: str, settings: Settings) -> bool:
    if name == "snowflake":
        return bool(settings.snowflake_account.strip() and settings.snowflake_pat.strip())
    if name == "gemini":
        return bool(settings.gemini_api_key.strip())
    if name == "ollama":
        return bool(settings.ollama_host.strip())
    raise ConfigError(f"Unknown LLM provider in LLM_CHAIN: {name}")


def _check_llm(settings: Settings) -> None:
    if settings.llm_mode != "live":
        return
    providers = [part.strip() for part in settings.llm_chain.split(",") if part.strip()]
    if not providers:
        raise ConfigError("LLM_CHAIN is empty while LLM_MODE is live")
    if any(_provider_ready(name, settings) for name in providers):
        return
    names = ", ".join(providers)
    raise ConfigError(
        f"LLM_MODE is live but no provider in LLM_CHAIN has credentials: {names}"
    )


def load_settings(
    config_path: Path | str | None = None,
    env_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Read config.yaml and environment variables into Settings.

    `environ` is for tests. When it is omitted, values come from the process
    environment after `.env` is loaded without overriding variables already set.
    """
    path = Path(config_path) if config_path is not None else _discover_config(Path.cwd())
    if not path.is_file():
        raise ConfigError(f"config.yaml not found at {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must be a mapping")

    if environ is None:
        dotenv_path = Path(env_file) if env_file is not None else path.parent / ".env"
        if dotenv_path.is_file():
            load_dotenv(dotenv_path, override=False)
        source: Mapping[str, str] = os.environ
    else:
        source = environ

    data = dict(raw)
    data.update(_env_overlay(source))
    try:
        settings = Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_validation_message(exc)) from exc

    if not settings.sec_user_agent.strip():
        raise ConfigError("SEC_USER_AGENT is required and is not set")
    _check_llm(settings)
    return settings
