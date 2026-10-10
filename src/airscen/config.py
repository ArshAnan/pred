"""Load `.env` and `config.yaml` into one Settings object.

The model is the CP0 contract (LLD section 9, CP0_DECISIONS R4).
`load_settings()` itself is lane A item A1: it must raise ConfigError
when SEC_USER_AGENT is missing. Unknown keys inside a section are ignored
so another lane can add a key without editing this file.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


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
        CompanyConfig(ticker="UAL", cik=None, name="United Airlines Holdings", peers=["DAL", "AAL"]),
        CompanyConfig(ticker="AAL", cik=None, name="American Airlines Group", peers=["DAL", "UAL"]),
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


def load_settings() -> Settings:
    raise NotImplementedError
