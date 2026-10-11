"""Candidate markets and hand labels.

`candidates` and `hand_labels` are B9. `label` stays for B10: an LLM label
cached by content hash, with hand labels always winning. This module does not
open the database and does not call the network. Lane A's worker loads
`hand_labels` during discovery and writes the rows.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from airscen.types import Factor, MarketKind, MarketLabel, MarketMeta, Venue

# Keyword prefilter for the airline decision: fuel, the next Fed decision,
# CPI, and Hormuz. Short tokens use a word boundary so "FedEx" and "principle"
# are not candidates. The hand shortlist is narrower than this filter.
_CANDIDATE = re.compile(
    r"\b(?:wti|brent|fomc|cpi|pce|inflation|hormuz)\b"
    r"|crude oil|oil price|jet fuel"
    r"|federal funds|federal reserve|fed decision|interest rate|rate hike|rate cut",
    re.IGNORECASE,
)

_OVERRIDE_KEYS = ("kind", "threshold", "threshold_unit", "group_key", "outcome")


def candidates(markets: list[MarketMeta]) -> list[MarketMeta]:
    """Keep markets whose title or rules match a modeled or mapped factor."""
    return [market for market in markets if _CANDIDATE.search(f"{market.title}\n{market.rules}")]


def label(meta: MarketMeta) -> MarketLabel | None:
    """Return a label, or None when labeling fails and the market stays unlabeled."""
    raise NotImplementedError


def hand_labels(path: str) -> dict[tuple[Venue, str], tuple[MarketLabel, dict]]:
    """Load hand labels plus optional metadata overrides.

    The override dict may contain kind, threshold, threshold_unit, group_key
    and outcome. A key set to null is included so a worker can clear a bad
    parse. label_source is always "hand".
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        raise ValueError("hand labels must be a list")
    loaded: dict[tuple[Venue, str], tuple[MarketLabel, dict]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("each hand label must be a mapping")
        venue = Venue(entry["venue"])
        market_id = str(entry["market_id"])
        factor = entry.get("factor")
        label_row = MarketLabel(
            relevant=bool(entry["relevant"]),
            factor=Factor(factor) if factor else None,
            yes_moves_factor=entry.get("yes_moves_factor"),
            mechanism=entry.get("mechanism") or "",
            citation=entry.get("citation"),
            label_source="hand",
        )
        overrides = {}
        for key in _OVERRIDE_KEYS:
            if key not in entry:
                continue
            value = entry[key]
            if key == "kind" and value is not None:
                value = MarketKind(value)
            overrides[key] = value
        key = (venue, market_id)
        if key in loaded:
            raise ValueError(f"duplicate hand label for {venue.value} {market_id}")
        loaded[key] = (label_row, overrides)
    return loaded
