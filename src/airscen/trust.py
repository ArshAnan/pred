"""Quote cleaning and trust score. CP0 stub; behavior lands in B1.

Pure functions. Import only airscen.types, the standard library, and numpy.
No network, database, or clock reads.
"""

from datetime import datetime
from typing import Any, Mapping

from airscen.types import MarketMeta, Snapshot, TrustResult


def clean(snapshot: Snapshot, now: datetime) -> Snapshot | None:
    """Return the snapshot, or None when the recorder must not insert it.

    None means the bid or ask is missing, the bid is above the ask, or bid,
    ask, or last is outside [0, 1]. A stale quote is still returned.
    """
    raise NotImplementedError


def trust_score(
    market: MarketMeta,
    snapshot: Snapshot,
    now: datetime,
    cfg: Mapping[str, Any],
) -> TrustResult:
    """Score one quote.

    cfg is the trust section of config.yaml (weights, floor, backfill_cap,
    spread_max, depth_ref, volume_ref, min_days), not Settings. A snapshot
    older than stale_after_s (default 300) sets excluded_reason to "stale"
    and still returns a score. A backfill src forces the score to backfill_cap
    (0.50). BACKFILL_INPUTS is a run-level flag set by the service.
    """
    raise NotImplementedError
