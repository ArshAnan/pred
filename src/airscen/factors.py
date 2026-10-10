"""Market odds to fuel and rate states.

Pure functions. Import only airscen.types, the standard library, and numpy.
No network, database, or clock reads.
"""

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from airscen.types import (
    Factor,
    FuelCurve,
    MarketKind,
    MarketLabel,
    MarketMeta,
    RateStates,
    Snapshot,
    TrustResult,
    Venue,
)

_VENUE_GAP = 0.10
_HIKE_BP = 25


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


@dataclass(frozen=True)
class _Strike:
    k: float
    mid: float
    trust: float
    close_time: datetime | None
    group_key: str


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _mid(quote: FactorQuote) -> float | None:
    if quote.trust.mid is not None:
        return quote.trust.mid
    bid, ask = quote.snapshot.bid, quote.snapshot.ask
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def _survival(points: list[tuple[float, float]], wti: float) -> float:
    ordered = sorted(points, key=lambda point: point[0])
    if not ordered:
        return 0.0
    if wti <= ordered[0][0]:
        return ordered[0][1]
    if wti >= ordered[-1][0]:
        return ordered[-1][1]
    for (k0, p0), (k1, p1) in zip(ordered, ordered[1:]):
        if k0 <= wti <= k1:
            if k1 == k0:
                return p0
            span = k1 - k0
            return p0 + (wti - k0) / span * (p1 - p0)
    return ordered[-1][1]


def _quantile(points: list[tuple[float, float]], target: float) -> float:
    """Inverse survival. An exact or flat hit uses the left strike."""
    ordered = sorted(points, key=lambda point: point[0])
    if not ordered:
        return 0.0
    for strike, prob in ordered:
        if prob == target:
            return strike
    if target >= ordered[0][1]:
        return ordered[0][0]
    if target <= ordered[-1][1]:
        return ordered[-1][0]
    for (k0, p0), (k1, p1) in zip(ordered, ordered[1:]):
        if p0 > target >= p1 and p0 != p1:
            return k0 + (p0 - target) / (p0 - p1) * (k1 - k0)
    return ordered[-1][0]


def rate_states(quotes: list[FactorQuote]) -> tuple[RateStates | None, list[str]]:
    """Normalise one Fed meeting.

    A missing hold or hike market returns (None, ["RATES_NO_MARKET_INPUT"]).
    The meeting is the qualifying group with the earliest close time. This
    function takes no as_of; callers pass the meetings they want considered.
    """
    grouped: dict[str, list[FactorQuote]] = {}
    for quote in quotes:
        if not _rate_candidate(quote):
            continue
        grouped.setdefault(quote.meta.group_key or "", []).append(quote)

    candidates = []
    for key, group in grouped.items():
        ready = _venues(group)
        if not ready:
            continue
        times = [q.meta.close_time for q in group if q.meta.close_time is not None]
        if times:
            candidates.append((0, min(_aware(ts) for ts in times), key, ready))
        else:
            candidates.append((1, datetime.min.replace(tzinfo=timezone.utc), key, ready))
    if not candidates:
        return None, ["RATES_NO_MARKET_INPUT"]

    _, _, key, ready = min(candidates, key=lambda row: (row[0], row[1], row[2]))
    venue = max(ready, key=lambda name: _venue_rank(name, ready[name]))
    pi_hike = _pi_hike(ready[venue])
    if pi_hike is None:
        return None, ["RATES_NO_MARKET_INPUT"]

    gap = None
    flags: list[str] = []
    others = [name for name in ready if name != venue]
    if others:
        other = _pi_hike(ready[others[0]])
        if other is not None:
            gap = abs(pi_hike - other)
            if gap > _VENUE_GAP:
                flags.append("VENUE_DISAGREE")

    return RateStates(
        p_hike=pi_hike,
        delta_hike_bp=_HIKE_BP,
        source_group=key,
        venue_gap=gap,
    ), flags


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
    strikes: dict[str, list[_Strike]] = {}
    touches: dict[str, list[_Strike]] = {}
    for quote in ladder:
        parsed = _fuel_strike(quote)
        if parsed is None:
            continue
        kind, strike = parsed
        bucket = strikes if kind == MarketKind.CLOSE_ABOVE else touches
        bucket.setdefault(strike.group_key, []).append(strike)

    horizon = _aware(as_of) + timedelta(days=horizon_days)
    eligible = []
    for key, group in strikes.items():
        by_k: dict[float, _Strike] = {}
        for strike in group:
            current = by_k.get(strike.k)
            if current is None or strike.trust > current.trust:
                by_k[strike.k] = strike
        if len(by_k) < 3:
            continue
        times = [strike.close_time for strike in group if strike.close_time is not None]
        if not times:
            continue
        resolve = max(_aware(ts) for ts in times)
        if resolve > horizon:
            continue
        eligible.append((resolve, len(group), key, by_k))
    if not eligible:
        return None, ["FUEL_NO_MARKET_INPUT"]

    _, _, key, by_k = max(eligible, key=lambda row: (row[0], row[1], row[2]))
    ordered = [by_k[k] for k in sorted(by_k)]
    probs: list[float] = []
    previous = 1.0
    for strike in ordered:
        probability = min(max(strike.mid, 0.0), 1.0, previous)
        probs.append(probability)
        previous = probability
    levels = [strike.k for strike in ordered]
    spacing = float(statistics.median([b - a for a, b in zip(levels, levels[1:])]))
    points = [(levels[0] - spacing, 1.0)]
    points.extend(zip(levels, probs))
    points.append((levels[-1] + spacing, 0.0))

    raw = {strike.k: strike.mid for strike in ordered}
    inconsistent = False
    for touch in touches.get(key, []):
        close_p = raw.get(touch.k)
        if close_p is not None and touch.mid < close_p - 0.02:
            inconsistent = True
            break
    flags = ["CRUDE_ONLY", "RESOLVE_DATE_PROXY"]
    if inconsistent:
        flags.append("LADDER_INCONSISTENT")
    return FuelCurve(points=points, resolve_group=key, n_markets=len(by_k)), flags


def fuel_states(
    curve: FuelCurve,
    weights: tuple[float, float, float] = (0.3, 0.4, 0.3),
) -> FuelStates:
    """Invert the survival curve at P10, P50, and P90.

    Quantiles are rounded to the cent so the presentation grid matches the
    worked example. S(w) = P(WTI >= w), so P10 is S inverse of 0.90.
    """
    points = list(curve.points)
    return FuelStates(
        wti_p10=round(_quantile(points, 0.90), 2),
        wti_p50=round(_quantile(points, 0.50), 2),
        wti_p90=round(_quantile(points, 0.10), 2),
        weights=weights,
    )


def _rate_candidate(quote: FactorQuote) -> bool:
    if quote.trust.excluded_reason is not None:
        return False
    if quote.label.factor != Factor.RATES:
        return False
    if quote.meta.kind != MarketKind.PER_MEETING:
        return False
    if not quote.meta.group_key or quote.meta.outcome not in {"hold", "hike", "cut"}:
        return False
    return _mid(quote) is not None


def _venues(group: list[FactorQuote]) -> dict[Venue, dict[str, FactorQuote]]:
    by_venue: dict[Venue, list[FactorQuote]] = {}
    for quote in group:
        by_venue.setdefault(quote.meta.venue, []).append(quote)
    ready = {}
    for venue, venue_quotes in by_venue.items():
        best: dict[str, FactorQuote] = {}
        for quote in venue_quotes:
            outcome = quote.meta.outcome or ""
            current = best.get(outcome)
            if current is None or quote.trust.score > current.trust.score:
                best[outcome] = quote
        if "hold" in best and "hike" in best:
            ready[venue] = best
    return ready


def _venue_rank(venue: Venue, best: dict[str, FactorQuote]) -> tuple[float, int]:
    score = min(best["hold"].trust.score, best["hike"].trust.score)
    return (score, 1 if venue == Venue.KALSHI else 0)


def _pi_hike(best: dict[str, FactorQuote]) -> float | None:
    hold = _mid(best["hold"])
    hike = _mid(best["hike"])
    cut = _mid(best["cut"]) if "cut" in best else 0.0
    if hold is None or hike is None or cut is None:
        return None
    total = hold + hike + cut
    if total <= 0:
        return None
    return hike / total


def _fuel_strike(quote: FactorQuote) -> tuple[MarketKind, _Strike] | None:
    if quote.trust.excluded_reason is not None:
        return None
    if quote.label.factor != Factor.FUEL or quote.label.yes_moves_factor != "up":
        return None
    if quote.meta.kind not in (MarketKind.CLOSE_ABOVE, MarketKind.TOUCH):
        return None
    if quote.meta.threshold is None or not quote.meta.group_key:
        return None
    mid = _mid(quote)
    if mid is None:
        return None
    return quote.meta.kind, _Strike(
        k=float(quote.meta.threshold),
        mid=mid,
        trust=quote.trust.score,
        close_time=quote.meta.close_time,
        group_key=quote.meta.group_key,
    )

