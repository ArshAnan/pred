"""Candidate markets and labels. CP0 stub; labeling lands in B9 and B10.

hand_labels is the function Lane A's worker calls during discovery.
The hand-label file itself is not filled in at CP0.
"""

from airscen.types import MarketLabel, MarketMeta, Venue


def candidates(markets: list[MarketMeta]) -> list[MarketMeta]:
    raise NotImplementedError


def label(meta: MarketMeta) -> MarketLabel | None:
    """Return a label, or None when labeling fails and the market stays unlabeled."""
    raise NotImplementedError


def hand_labels(path: str) -> dict[tuple[Venue, str], tuple[MarketLabel, dict]]:
    """Load hand labels plus optional metadata overrides. Not filled in at CP0."""
    raise NotImplementedError
