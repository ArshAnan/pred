"""Candidate markets and labels.

hand_labels is the function Lane A's worker calls during discovery.
label() stays unimplemented until B10.
"""

import re
from pathlib import Path

import yaml

from airscen.types import Factor, MarketKind, MarketLabel, MarketMeta, Venue

_WORD = re.compile(r"\b(?:wti|crude|brent|fomc|cpi|pce|inflation|hormuz)\b", re.IGNORECASE)
_PHRASES = (
    "oil price",
    "jet fuel",
    "federal funds",
    "fed decision",
    "federal reserve",
    "interest rate",
    "rate hike",
    "rate cut",
    "strait of hormuz",
)
_OVERRIDE_KEYS = ("kind", "outcome", "threshold", "threshold_unit", "group_key")


def candidates(markets: list[MarketMeta]) -> list[MarketMeta]:
    """Keyword prefilter. Bare 'federal' and names like FedEx stay out."""
    chosen = []
    for market in markets:
        text = f"{market.title}\n{market.rules}"
        lowered = text.lower()
        if _WORD.search(text) or any(phrase in lowered for phrase in _PHRASES):
            chosen.append(market)
    return chosen


def label(meta: MarketMeta) -> MarketLabel | None:
    """Return a label, or None when labeling fails and the market stays unlabeled."""
    raise NotImplementedError("LLM labeling is B10")


def hand_labels(path: str) -> dict[tuple[Venue, str], tuple[MarketLabel, dict]]:
    """Load hand labels plus optional metadata overrides.

    label_source is always "hand". A repeated venue and market_id raises
    ValueError. Override keys are kind, outcome, threshold, threshold_unit,
    and group_key.
    """
    loaded = yaml.safe_load(Path(path).read_text()) or []
    found: dict[tuple[Venue, str], tuple[MarketLabel, dict]] = {}
    for row in loaded:
        venue = Venue(str(row["venue"]))
        market_id = str(row["market_id"])
        key = (venue, market_id)
        if key in found:
            raise ValueError(f"duplicate hand label for {venue.value} {market_id}")
        factor = row.get("factor")
        label = MarketLabel(
            relevant=bool(row["relevant"]),
            factor=Factor(factor) if factor else None,
            yes_moves_factor=row.get("yes_moves_factor"),
            mechanism=row.get("mechanism") or "",
            citation=row.get("citation"),
            label_source="hand",
        )
        overrides = {}
        for name in _OVERRIDE_KEYS:
            if name not in row or row[name] is None:
                continue
            value = row[name]
            if name == "kind":
                value = MarketKind(value).value
            elif name == "threshold":
                value = float(value)
            overrides[name] = value
        found[key] = (label, overrides)
    return found
