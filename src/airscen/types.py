# airscen/types.py -- every model is a frozen pydantic v2 BaseModel
from enum import Enum
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)

class Venue(str, Enum):      KALSHI = "kalshi"; POLYMARKET = "polymarket"
class Factor(str, Enum):     FUEL = "fuel"; RATES = "rates"; INFLATION = "inflation"; DEMAND = "demand"; GEOPOLITICS = "geopolitics"
class MarketKind(str, Enum): CLOSE_ABOVE = "close_above"; TOUCH = "touch"; PER_MEETING = "per_meeting"; LEVEL_AFTER = "level_after"; OTHER = "other"
class Basis(str, Enum):      GAAP = "GAAP"; ADJUSTED = "ADJUSTED"
class Extraction(str, Enum): XBRL = "xbrl"; PARSER = "parser"; LLM = "llm"; OVERRIDE = "override"

class Metric(str, Enum):
    OPERATING_INCOME = "operating_income"          # usd_m, one quarter
    NET_INTEREST_EXPENSE = "net_interest_expense"  # usd_m, one quarter
    FUEL_GALLONS_M = "fuel_gallons_m"              # millions of gallons, one quarter
    FUEL_PRICE_PER_GAL = "fuel_price_per_gal"      # usd per gallon
    DEBT_TOTAL = "debt_total"                      # usd_m, includes finance leases
    DEBT_CURRENT = "debt_current"                  # usd_m
    VARIABLE_DEBT_SHARE = "variable_debt_share"    # fraction 0..1
    LIQUIDITY = "liquidity"                        # usd_m
    ADJUSTED_NET_DEBT = "adjusted_net_debt"        # usd_m
    REVENUE_GAAP = "revenue_gaap"; REVENUE_ADJUSTED = "revenue_adjusted"          # usd_m
    TOTAL_ASSETS = "total_assets"; TOTAL_LIABILITIES = "total_liabilities"; EQUITY = "equity"  # usd_m, identity checks

class Company(FrozenModel): ticker: str; cik: int; name: str; peers: list[str]

class Fact(FrozenModel):
    company: str; metric: Metric; value: float; unit: str; basis: Basis
    period_end: date; filed_at: datetime; source_url: str
    extraction: Extraction; validated: bool = False

    @model_validator(mode="after")
    def check_fact(self):
        if self.filed_at.date() < self.period_end:
            raise ValueError("filed_at must be on or after period_end")
        if self.metric == Metric.VARIABLE_DEBT_SHARE and not 0 <= self.value <= 1:
            raise ValueError("variable_debt_share must be between 0 and 1")
        money_metrics = {
            Metric.OPERATING_INCOME, Metric.NET_INTEREST_EXPENSE,
            Metric.DEBT_TOTAL, Metric.DEBT_CURRENT, Metric.LIQUIDITY,
            Metric.ADJUSTED_NET_DEBT, Metric.REVENUE_GAAP,
            Metric.REVENUE_ADJUSTED, Metric.TOTAL_ASSETS,
            Metric.TOTAL_LIABILITIES, Metric.EQUITY,
        }
        if self.metric in money_metrics and self.unit != "usd_m":
            raise ValueError("monetary facts must use unit usd_m")
        return self

class MarketMeta(FrozenModel):
    venue: Venue; market_id: str; token_id: Optional[str] = None
    title: str; rules: str = ""; close_time: Optional[datetime] = None
    kind: MarketKind = MarketKind.OTHER
    outcome: Optional[Literal["hold", "hike", "cut"]] = None        # per-meeting markets only
    threshold: Optional[float] = None; threshold_unit: Optional[str] = None   # "usd_per_bbl", "bp"
    group_key: Optional[str] = None   # same resolve date or same meeting, e.g. "KXFEDDECISION-26OCT"

class MarketLabel(FrozenModel):
    relevant: bool; factor: Optional[Factor] = None
    yes_moves_factor: Optional[Literal["up", "down"]] = None
    mechanism: str = Field("", max_length=200); citation: Optional[str] = None
    label_source: Literal["hand", "llm"]

    @model_validator(mode="after")
    def check_relevant_label(self):
        if self.relevant and (self.factor is None or self.yes_moves_factor is None):
            raise ValueError("relevant labels require factor and yes_moves_factor")
        return self

class Snapshot(FrozenModel):
    venue: Venue; market_id: str; ts: datetime
    bid: Optional[float] = None; ask: Optional[float] = None         # probability units, 0..1
    bid_size: Optional[float] = None; ask_size: Optional[float] = None
    last: Optional[float] = None; volume_24h: Optional[float] = None
    open_interest: Optional[float] = None; liquidity: Optional[float] = None
    src: Literal["live", "backfill_kalshi", "backfill_polymarket"] = "live"

class MacroObs(FrozenModel): series_id: str; obs_date: date; value: float; realtime_start: date

# ---- engine and API models ----
class TrustResult(FrozenModel):
    venue: Venue; market_id: str; score: float; mid: Optional[float]
    components: dict[str, float]; excluded_reason: Optional[str] = None

class RateStates(FrozenModel):
    p_hike: Optional[float]; delta_hike_bp: int = 25; source_group: Optional[str]; venue_gap: Optional[float]

class FuelCurve(FrozenModel):
    points: list[tuple[float, float]]   # (wti usd/bbl, P(WTI >= w)); tails included; non-increasing
    resolve_group: str; n_markets: int

class ScenarioInputs(FrozenModel):
    company: str; as_of: datetime
    note_size_usd_m: float; tenor_years: int; coupon: float          # coupon as a decimal, e.g. 0.055
    coverage_threshold: float; fuel_passthrough: float = 1.0
    baseline: dict[Metric, Fact]; wti_ref: float                     # mean WTI over the baseline quarter
    rates: Optional[RateStates]; fuel: Optional[FuelCurve]

class ScenarioRow(FrozenModel):
    fuel_label: Literal["P10", "P50", "P90"]; rate_label: Literal["hold", "hike"]
    wti: float; probability: Optional[float]
    operating_income_usd_m: float; net_interest_usd_m: float
    coverage: float; breach: bool

class ScenarioSummary(FrozenModel):
    p_breach: Optional[float]
    breakeven_wti_hold: float; breakeven_wti_hike: float
    coverage_p50: float; coverage_p90: float
    liquidity_cover: Optional[float]; liquidity_ok: Optional[bool]

class PeerResult(FrozenModel): ticker: str; period_end: date; summary: ScenarioSummary; flags: list[str]

class RunRequest(FrozenModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str; as_of: Optional[datetime] = None
    note_size_usd_m: float = Field(gt=0, le=50_000)
    tenor_years: int = Field(ge=1, le=40)
    coupon_spread_bp: float = Field(ge=0, le=1_000)
    coverage_threshold: float = Field(default=3.0, gt=0, le=50)
    fuel_passthrough: float = Field(default=1.0, ge=0, le=1)
    include_peers: bool = True

    @model_validator(mode="after")
    def check_as_of(self):
        if self.as_of is not None:
            if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
                raise ValueError("as_of must include a timezone")
            if self.as_of > datetime.now(timezone.utc) + timedelta(minutes=1):
                raise ValueError("as_of cannot be more than one minute in the future")
        return self

class Brief(FrozenModel):
    template_md: str; llm_md: Optional[str]
    llm_status: Literal["pending", "ready", "failed", "rejected", "skipped"]

class RunResult(FrozenModel):
    schema_version: int = 1; run_id: str; company: str; as_of: datetime; created_at: datetime
    inputs: RunRequest; rows: list[ScenarioRow]; summary: ScenarioSummary
    peers: list[PeerResult]; flags: list[str]; data_version: str; brief: Brief

class Alert(FrozenModel):
    alert_id: str; ts: datetime; company: str; run_id: Optional[str]
    rule: Literal["BREACH_FLIP", "DRIVER_MOVE", "DATA_STALE"]
    dedup_key: str; severity: Literal["high", "medium", "low"]; payload: dict
    status: Literal["pending", "sent", "failed", "suppressed"]


class AirScenError(Exception):
    code = "E_INTERNAL"

    def __init__(self, message: str, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.details = details or {}


class ConfigError(AirScenError): code = "E_CONFIG"
class RequestError(AirScenError): code = "E_VALIDATION"
class DataError(AirScenError): code = "E_MISSING_FACT"
class SourceError(AirScenError): code = "E_SOURCE_UNAVAILABLE"
class LLMError(AirScenError): code = "E_LLM_UNAVAILABLE"
class StoreError(AirScenError): code = "E_STORE_UNAVAILABLE"


class SourceStatus(FrozenModel):
    status: Literal["ok", "degraded", "down"]
    last_ok_at: Optional[datetime] = None
    fail_streak: int = 0


class LLMStatus(FrozenModel):
    mode: Literal["live", "cache_only", "off"]
    last_provider: Optional[str] = None
    calls_last_hour: int = 0
    cache_hit_rate: Optional[float] = None


class WorkerStatus(FrozenModel):
    last_tick_at: Optional[datetime] = None
    degraded: bool = False


class HealthStatus(FrozenModel):
    status: Literal["ok", "degraded", "down"]
    db: Literal["ok", "down"]
    sources: dict[str, SourceStatus]
    last_snapshot_age_s: Optional[float] = None
    llm: LLMStatus
    worker: WorkerStatus


class MarketView(FrozenModel):
    meta: MarketMeta
    label: Optional[MarketLabel] = None
    latest: Optional[Snapshot] = None
    trust: Optional[TrustResult] = None
    stale: bool = False


class RunSummary(FrozenModel):
    run_id: str; company: str; as_of: datetime; created_at: datetime
    p_breach: Optional[float]; flags: list[str]
    llm_status: Literal["pending", "ready", "failed", "rejected", "skipped"]
