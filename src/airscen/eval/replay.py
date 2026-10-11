"""July 10, 2026 replay from recorded Kalshi candlesticks.

The quotes are the last 60-minute bar at or before 2026-07-10T20:00:00Z.
They are stored with a backfill source, so trust is capped and the run
carries BACKFILL_INPUTS. June-quarter financials are not in overrides.yaml,
so this module reports the fuel and rate range and does not invent coverage.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from airscen.factors import FactorQuote, fuel_curve, fuel_states, rate_states
from airscen.trust import trust_score
from airscen.types import Factor, MarketKind, MarketLabel, MarketMeta, Snapshot, Venue

_TRUST = {
    "weights": {"spread": 0.35, "depth": 0.25, "volume": 0.25, "time": 0.15},
    "floor": 0.40,
    "backfill_cap": 0.50,
    "spread_max": 0.10,
    "depth_ref": 1000,
    "volume_ref": {"kalshi": 10_000, "polymarket": 100_000},
    "min_days": 2,
    "stale_after_s": 300,
}
GUIDE_GAL = 3.15
REALIZED_GAL = 3.61
REALIZED_COVERAGE = 10.0


def default_fixture() -> Path:
    return Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "july10_replay.json"


def load_replay(path: Path | None = None) -> dict:
    return json.loads((path or default_fixture()).read_text())


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _quote(meta: MarketMeta, label: MarketLabel, bid: float, ask: float, volume: float, ts: datetime) -> FactorQuote:
    snapshot = Snapshot(
        venue=meta.venue,
        market_id=meta.market_id,
        ts=ts,
        bid=bid,
        ask=ask,
        volume_24h=volume,
        src="backfill_kalshi",
    )
    trust = trust_score(meta, snapshot, ts, _TRUST)
    return FactorQuote(meta=meta, label=label, snapshot=snapshot, trust=trust)


def july10_inputs(doc: dict | None = None):
    """Trusted July 10 quotes. Backfill caps every score at 0.50."""
    doc = doc or load_replay()
    as_of = _when(doc["as_of"])
    quotes = []
    for row in doc["fed"]:
        meta = MarketMeta(
            venue=Venue.KALSHI,
            market_id=row["ticker"],
            title=row.get("title") or row["ticker"],
            close_time=_when(row["close_time"]) if row.get("close_time") else None,
            kind=MarketKind.PER_MEETING,
            outcome=row["outcome"],
            group_key="KXFEDDECISION-26JUL",
        )
        direction = "up" if row["outcome"] == "hike" else "down"
        label = MarketLabel(
            relevant=True,
            factor=Factor.RATES,
            yes_moves_factor=direction,
            mechanism="July 2026 Fed decision contract, from the recorded candlestick.",
            label_source="hand",
        )
        quotes.append(_quote(meta, label, row["bid"], row["ask"], row.get("volume") or 0.0, as_of))
    for row in doc["wti"]:
        meta = MarketMeta(
            venue=Venue.KALSHI,
            market_id=row["ticker"],
            title=row.get("title") or row["ticker"],
            close_time=_when(row["close_time"]),
            kind=MarketKind.CLOSE_ABOVE,
            threshold=float(row["threshold"]),
            threshold_unit="usd_per_bbl",
            group_key=doc["wti_event"],
        )
        label = MarketLabel(
            relevant=True,
            factor=Factor.FUEL,
            yes_moves_factor="up",
            mechanism="July 13, 2026 WTI close-above contract, from the recorded candlestick.",
            label_source="hand",
        )
        quotes.append(_quote(meta, label, row["bid"], row["ask"], row.get("volume") or 0.0, as_of))
    return as_of, quotes, doc


def replay_lines(doc: dict | None = None) -> list[str]:
    as_of, quotes, doc = july10_inputs(doc)
    rates, rate_flags = rate_states(quotes)
    curve, fuel_flags = fuel_curve(quotes, as_of)
    flags = ["BACKFILL_INPUTS", *rate_flags, *fuel_flags]
    lines = [
        f"as_of {as_of.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "flags: " + ", ".join(flags),
        "source: " + doc["source"],
    ]
    if rates is None or rates.p_hike is None:
        lines.append("rates: none")
    else:
        lines.append(f"p_hike {rates.p_hike:.4f} group {rates.source_group}")
    if curve is None:
        lines.append("fuel: none")
    else:
        states = fuel_states(curve)
        lines.append(
            f"WTI P10 {states.wti_p10:.2f} P50 {states.wti_p50:.2f} P90 {states.wti_p90:.2f} "
            f"n {curve.n_markets} group {curve.resolve_group}"
        )
        ref = doc["wti_ref_q2"]
        lines.append(
            f"Q2 mean WTI {ref['mean']:.2f} from {ref['series']} "
            f"{ref['start']} to {ref['end']} (n={ref['n']})"
        )
    lines.append(
        f"company guide on 2026-07-10: {GUIDE_GAL:.2f} USD/gal. "
        f"September quarter realized: adjusted fuel {REALIZED_GAL:.2f} USD/gal, "
        f"coverage about {REALIZED_COVERAGE:.0f}x."
    )
    lines.append(
        "coverage grid not computed: June-quarter operating income is not in overrides.yaml"
    )
    lines.append("one case study, not a statistical result")
    return lines
