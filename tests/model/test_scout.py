"""Keyword prefilter and hand labels. LLD section 5 and CP0_DECISIONS D4."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from airscen.scout import candidates, hand_labels
from airscen.types import Factor, MarketKind, Venue
from tests.model.conftest import make_market

ROOT = Path(__file__).resolve().parents[2]
HAND_LABELS = ROOT / "hand_labels.yaml"


def test_candidates_keep_fuel_rates_cpi_and_hormuz_only():
    markets = [
        make_market(market_id="wti", title="WTI crude oil settlement above 90"),
        make_market(market_id="fed", title="Federal Reserve rate decision", rules="FOMC"),
        make_market(market_id="cpi", title="CPI month over month"),
        make_market(market_id="hormuz", title="Strait of Hormuz transits"),
        make_market(market_id="fedex", title="FedEx earnings call mention"),
        make_market(market_id="coffee", title="Coffee prices in Brazil"),
    ]
    assert [market.market_id for market in candidates(markets)] == ["wti", "fed", "cpi", "hormuz"]


def test_hand_labels_force_hand_source_and_keep_overrides(tmp_path):
    path = tmp_path / "hand_labels.yaml"
    path.write_text(
        """
- venue: kalshi
  market_id: M1
  relevant: true
  factor: fuel
  yes_moves_factor: up
  mechanism: "Higher WTI raises jet fuel cost."
  label_source: llm
  kind: close_above
  threshold: 90
  threshold_unit: usd_per_bbl
  group_key: 2026-10-13
  outcome: null
""",
        encoding="utf-8",
    )
    loaded = hand_labels(str(path))
    label, overrides = loaded[(Venue.KALSHI, "M1")]
    assert label.label_source == "hand"
    assert label.factor == Factor.FUEL
    assert overrides["kind"] == MarketKind.CLOSE_ABOVE
    assert overrides["threshold"] == 90
    assert overrides["outcome"] is None
    assert "label_source" not in overrides


def test_duplicate_hand_label_is_rejected(tmp_path):
    path = tmp_path / "hand_labels.yaml"
    path.write_text(
        """
- {venue: kalshi, market_id: M1, relevant: false, mechanism: ""}
- {venue: kalshi, market_id: M1, relevant: false, mechanism: ""}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        hand_labels(str(path))


def test_a_relevant_label_without_a_factor_is_rejected(tmp_path):
    path = tmp_path / "hand_labels.yaml"
    path.write_text(
        """
- {venue: polymarket, market_id: M2, relevant: true, mechanism: "missing factor"}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        hand_labels(str(path))


def test_checked_in_shortlist_covers_the_decision_factors():
    loaded = hand_labels(str(HAND_LABELS))
    assert 15 <= len(loaded) <= 30
    assert all(label.label_source == "hand" for label, _ in loaded.values())

    fed = {
        overrides.get("outcome"): market_id
        for (venue, market_id), (_, overrides) in loaded.items()
        if overrides.get("group_key") == "KXFEDDECISION-26OCT"
    }
    assert fed["hold"] == "KXFEDDECISION-26OCT-H0"
    assert fed["hike"] == "KXFEDDECISION-26OCT-H25"
    assert fed["cut"] == "KXFEDDECISION-26OCT-C25"
    assert fed[None]

    fuel = [
        overrides
        for (_, _), (label, overrides) in loaded.items()
        if label.factor == Factor.FUEL and overrides.get("kind") == MarketKind.CLOSE_ABOVE
    ]
    assert len(fuel) >= 3
    assert len({row["group_key"] for row in fuel}) == 1
    assert any(overrides.get("kind") == MarketKind.TOUCH for _, overrides in loaded.values())
    assert any(label.factor == Factor.INFLATION for label, _ in loaded.values())
    assert any(label.factor == Factor.GEOPOLITICS for label, _ in loaded.values())
