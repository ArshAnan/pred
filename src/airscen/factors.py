"""Market odds to fuel and rate states. CP0 stub; behavior lands in B2 and B3.

Pure functions. Import only airscen.types, the standard library, and numpy.
No network, database, or clock reads.
"""

from dataclasses import dataclass
from datetime import datetime

from airscen.types import FuelCurve, MarketLabel, MarketMeta, RateStates, Snapshot, TrustResult


@dataclass(frozen=True)
class FactorQuote:
    """One trusted market passed into the factor functions."""

    meta: MarketMeta
    label: MarketLabel
    snapshot: Snapshot
    trust: TrustResult


@dataclass(frozen=True)
class FuelStates:
    """Presentation WTI levels. Weights are Swanson's 0.3, 0.4, 0.3 by default."""

    wti_p10: float
    wti_p50: float
    wti_p90: float
    weights: tuple[float, float, float]


def rate_states(quotes: list[FactorQuote]) -> tuple[RateStates | None, list[str]]:
    """Normalise one Fed meeting.

    A missing hold or hike market returns (None, ["RATES_NO_MARKET_INPUT"]).
    """
    raise NotImplementedError


def fuel_curve(
    ladder: list[FactorQuote],
    as_of: datetime,
    horizon_days: int = 100,
) -> tuple[FuelCurve | None, list[str]]:
    """Build the WTI survival curve.

    Group selection uses as_of plus horizon_days. Fewer than 3 trusted
    thresholds returns (None, ["FUEL_NO_MARKET_INPUT"]). A curve's flags
    always include CRUDE_ONLY and RESOLVE_DATE_PROXY.
    """
    raise NotImplementedError


def fuel_states(
    curve: FuelCurve,
    weights: tuple[float, float, float] = (0.3, 0.4, 0.3),
) -> FuelStates:
    """Invert the survival curve at P10, P50, and P90."""
    raise NotImplementedError
