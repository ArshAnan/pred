"""Candidate markets and labels.

hand_labels is the function Lane A's worker calls during discovery.
label() asks the LLM for markets that are not in the hand-label file.
A hand label always wins. An LLM failure leaves the market unlabeled.
"""

import hashlib
import json
import re
from datetime import timezone
from pathlib import Path

import yaml

from airscen.types import Factor, LLMError, MarketKind, MarketLabel, MarketMeta, Venue

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


_LABEL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relevant", "mechanism"],
    "properties": {
        "relevant": {"type": "boolean"},
        "factor": {
            "type": ["string", "null"],
            "enum": ["fuel", "rates", "inflation", "demand", "geopolitics", None],
        },
        "yes_moves_factor": {"type": ["string", "null"], "enum": ["up", "down", None]},
        "mechanism": {"type": "string"},
        "citation": {"type": ["string", "null"]},
    },
}
_LABEL_SYSTEM = (
    "Label one prediction market for an airline credit scenario. "
    "Return only JSON. relevant is true only for fuel, rates, inflation, demand, or geopolitics. "
    "mechanism is one sentence of at most 200 characters."
)


def content_hash(meta: MarketMeta) -> str:
    """Hash of the title, rules, and close time. A new hash means the market changed."""
    close = ""
    if meta.close_time is not None:
        ts = meta.close_time
        if ts.tzinfo is None or ts.utcoffset() is None:
            ts = ts.replace(tzinfo=timezone.utc)
        close = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = json.dumps(
        {"close_time": close, "rules": meta.rules, "title": meta.title},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def label(meta: MarketMeta, llm=None, hands=None) -> MarketLabel | None:
    """Return a label, or None when labeling fails and the market stays unlabeled.

    A hand label for this venue and market id is returned without a model call.
    Otherwise the model is asked for a MarketLabel. The prompt is the hashed
    content, so a repeat of the same title, rules, and close time hits the
    LLM cache. label_source on a model label is llm.
    """
    hand = _hand_for(hands, meta)
    if hand is not None:
        return hand
    if llm is None:
        return None
    user = json.dumps(
        {
            "title": meta.title,
            "rules": meta.rules,
            "close_time": "" if meta.close_time is None else meta.close_time.isoformat(),
        },
        sort_keys=True,
    )
    try:
        raw = llm.complete_json("label_market", _LABEL_SYSTEM, user, _LABEL_SCHEMA, content_hash(meta))
    except LLMError:
        return None
    return _as_label(raw)


def _hand_for(hands, meta: MarketMeta) -> MarketLabel | None:
    if not hands:
        return None
    row = hands.get((meta.venue, meta.market_id))
    if row is None:
        return None
    if isinstance(row, tuple):
        return row[0]
    return row


def _as_label(raw) -> MarketLabel | None:
    if not isinstance(raw, dict):
        return None
    factor = raw.get("factor") or None
    direction = raw.get("yes_moves_factor") or None
    citation = raw.get("citation") or None
    try:
        return MarketLabel(
            relevant=bool(raw.get("relevant")),
            factor=Factor(factor) if factor else None,
            yes_moves_factor=direction if direction in ("up", "down") else None,
            mechanism=str(raw.get("mechanism") or "")[:200],
            citation=str(citation) if citation else None,
            label_source="llm",
        )
    except (ValueError, TypeError):
        return None


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
