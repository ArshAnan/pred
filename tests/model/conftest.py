"""Lane B fixtures. Add them here; leave tests/conftest.py unchanged."""

import copy
from datetime import datetime, timedelta, timezone

import pytest

from airscen.factors import FactorQuote
from airscen.types import (
    Basis,
    Extraction,
    Factor,
    MarketKind,
    MarketLabel,
    MarketMeta,
    Metric,
    Snapshot,
    TrustResult,
    Venue,
)

AS_OF = datetime(2026, 10, 9, 14, 30, tzinfo=timezone.utc)
_OMITTED = object()

TRUST_CFG = {
    "weights": {"spread": 0.35, "depth": 0.25, "volume": 0.25, "time": 0.15},
    "floor": 0.40,
    "backfill_cap": 0.50,
    "spread_max": 0.10,
    "depth_ref": 1000,
    "volume_ref": {"kalshi": 10_000, "polymarket": 100_000},
    "min_days": 2,
    "stale_after_s": 300,
}


def make_market(**kwargs) -> MarketMeta:
    fields = {
        "venue": Venue.KALSHI,
        "market_id": "MKT",
        "title": "illustrative market",
        "close_time": AS_OF + timedelta(days=30),
    }
    fields.update(kwargs)
    return MarketMeta(**fields)


def make_snapshot(**kwargs) -> Snapshot:
    venue = kwargs.get("venue", Venue.KALSHI)
    fields = {
        "venue": venue,
        "market_id": "MKT",
        "ts": AS_OF,
        "bid": 0.50,
        "ask": 0.50,
        "bid_size": 1000,
        "ask_size": 1000,
        "volume_24h": 10_000 if venue == Venue.KALSHI else 100_000,
        "src": "live",
    }
    fields.update(kwargs)
    return Snapshot(**fields)


def make_trust(snapshot: Snapshot, mid: float, score: float = 0.90, excluded: str | None = None) -> TrustResult:
    return TrustResult(
        venue=snapshot.venue,
        market_id=snapshot.market_id,
        score=score,
        mid=mid,
        components={"spread": 1.0, "depth": 1.0, "volume": 1.0, "time": 1.0},
        excluded_reason=excluded,
    )


def make_label(factor: Factor, direction: str = "up") -> MarketLabel:
    return MarketLabel(
        relevant=True,
        factor=factor,
        yes_moves_factor=direction,
        mechanism="illustrative",
        label_source="hand",
    )


def make_quote(
    *,
    factor: Factor,
    mid: float,
    market_id: str,
    kind: MarketKind,
    outcome: str | None = None,
    threshold: float | None = None,
    group_key: str | None = "group",
    venue: Venue = Venue.KALSHI,
    close_time=_OMITTED,
    direction: str = "up",
    score: float = 0.90,
    excluded: str | None = None,
    src: str = "live",
) -> FactorQuote:
    if close_time is _OMITTED:
        close_time = AS_OF + timedelta(days=30)
    market = make_market(
        venue=venue,
        market_id=market_id,
        kind=kind,
        outcome=outcome,
        threshold=threshold,
        threshold_unit="usd_per_bbl" if threshold is not None else None,
        group_key=group_key,
        close_time=close_time,
    )
    snapshot = make_snapshot(
        venue=venue,
        market_id=market_id,
        bid=mid,
        ask=mid,
        src=src,
    )
    return FactorQuote(
        meta=market,
        label=make_label(factor, direction),
        snapshot=snapshot,
        trust=make_trust(snapshot, mid, score=score, excluded=excluded),
    )


def make_fact(metric: Metric, value: float, unit: str = "usd_m"):
    from airscen.types import Fact

    return Fact(
        company="DAL",
        metric=metric,
        value=value,
        unit=unit,
        basis=Basis.GAAP,
        period_end=datetime(2026, 9, 30, tzinfo=timezone.utc).date(),
        filed_at=AS_OF,
        source_url="https://www.sec.gov/Archives/edgar/data/27904/000002790426000035/deltaairlinesannouncessept.htm",
        extraction=Extraction.OVERRIDE,
        validated=True,
    )


@pytest.fixture
def trust_cfg():
    return copy.deepcopy(TRUST_CFG)


@pytest.fixture
def as_of():
    return AS_OF
