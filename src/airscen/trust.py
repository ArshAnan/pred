"""Quote cleaning and trust score.

Pure functions. Import only airscen.types, the standard library, and numpy.
No network, database, or clock reads.
"""

import math
from datetime import datetime, timezone
from typing import Any, Mapping

from airscen.types import MarketMeta, Snapshot, TrustResult

_BACKFILL = {"backfill_kalshi", "backfill_polymarket"}
_DEFAULT_WEIGHTS = {"spread": 0.35, "depth": 0.25, "volume": 0.25, "time": 0.15}
_DEFAULT_VOLUME_REF = {"kalshi": 10_000, "polymarket": 100_000}


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _clip(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if value < lo else hi if value > hi else value


def _cfg(cfg: Mapping[str, Any], key: str, default: Any) -> Any:
    if key not in cfg or cfg[key] is None:
        return default
    return cfg[key]


def clean(snapshot: Snapshot, now: datetime) -> Snapshot | None:
    """Return the snapshot, or None when the recorder must not insert it.

    None means the bid or ask is missing, the bid is above the ask, bid,
    ask, or last is outside [0, 1], or a present size is negative. A stale
    quote is still returned; trust_score marks it. ``now`` is part of the
    signature so the recorder can share one clock with trust_score.
    """
    del now
    if snapshot.bid is None or snapshot.ask is None:
        return None
    if snapshot.bid > snapshot.ask:
        return None
    for value in (snapshot.bid, snapshot.ask, snapshot.last):
        if value is not None and not 0 <= value <= 1:
            return None
    for size in (snapshot.bid_size, snapshot.ask_size):
        if size is not None and size < 0:
            return None
    return snapshot


def trust_score(
    market: MarketMeta,
    snapshot: Snapshot,
    now: datetime,
    cfg: Mapping[str, Any],
) -> TrustResult:
    """Score one quote.

    cfg is the trust section of config.yaml (weights, floor, backfill_cap,
    spread_max, depth_ref, volume_ref, min_days), not Settings. Pass
    stale_after_s in that mapping when it should differ from 300; it lives
    in the poll section, which this function does not read. A snapshot
    older than stale_after_s sets excluded_reason to "stale" and still
    returns a score. A backfill src forces the score to backfill_cap.
    BACKFILL_INPUTS is a run-level flag set by the service.
    """
    weights = _cfg(cfg, "weights", _DEFAULT_WEIGHTS)
    if not isinstance(weights, Mapping):
        weights = _DEFAULT_WEIGHTS
    floor = float(_cfg(cfg, "floor", 0.40))
    backfill_cap = float(_cfg(cfg, "backfill_cap", 0.50))
    spread_max = float(_cfg(cfg, "spread_max", 0.10))
    depth_ref = float(_cfg(cfg, "depth_ref", 1000))
    min_days = float(_cfg(cfg, "min_days", 2))
    stale_after_s = float(_cfg(cfg, "stale_after_s", 300))
    volume_ref = _cfg(cfg, "volume_ref", _DEFAULT_VOLUME_REF)

    if snapshot.bid is None or snapshot.ask is None or spread_max <= 0:
        mid = None
        f_spread = 0.0
    else:
        mid = (snapshot.bid + snapshot.ask) / 2
        f_spread = _clip(1 - (snapshot.ask - snapshot.bid) / spread_max)

    if snapshot.bid_size is None or snapshot.ask_size is None or depth_ref <= 0:
        f_depth = 0.0
    else:
        depth = min(snapshot.bid_size, snapshot.ask_size)
        f_depth = 0.0 if depth <= 0 else _clip(math.log10(1 + depth) / math.log10(1 + depth_ref))

    venue = market.venue.value
    ref = 0.0
    if isinstance(volume_ref, Mapping) and venue in volume_ref and volume_ref[venue] is not None:
        ref = float(volume_ref[venue])
    if snapshot.volume_24h is None or ref <= 0:
        f_volume = 0.0
    else:
        volume = snapshot.volume_24h if snapshot.volume_24h > 0 else 0.0
        f_volume = _clip(math.log10(1 + volume) / math.log10(1 + ref))

    if market.close_time is None or min_days <= 0:
        f_time = 0.0
    else:
        days = (_aware(market.close_time) - _aware(now)).total_seconds() / 86400
        f_time = _clip(days / min_days)

    components = {"spread": f_spread, "depth": f_depth, "volume": f_volume, "time": f_time}
    score = (
        float(weights.get("spread", 0.35)) * f_spread
        + float(weights.get("depth", 0.25)) * f_depth
        + float(weights.get("volume", 0.25)) * f_volume
        + float(weights.get("time", 0.15)) * f_time
    )
    if snapshot.src in _BACKFILL:
        score = backfill_cap

    age_s = (_aware(now) - _aware(snapshot.ts)).total_seconds()
    if age_s > stale_after_s:
        excluded = "stale"
    elif score < floor:
        excluded = "below_floor"
    else:
        excluded = None

    return TrustResult(
        venue=market.venue,
        market_id=market.market_id,
        score=score,
        mid=mid,
        components=components,
        excluded_reason=excluded,
    )
