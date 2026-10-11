"""Scout prefilter and hand labels."""

from pathlib import Path

import pytest
import yaml

from airscen.scout import candidates, hand_labels, label
from airscen.types import Factor, MarketKind, MarketMeta, Venue

ROOT = Path(__file__).resolve().parents[2]


def _market(market_id, title, rules=""):
    return MarketMeta(venue=Venue.KALSHI, market_id=market_id, title=title, rules=rules)


def test_prefilter_keeps_factor_words_and_drops_lookalikes():
    markets = [
        _market("fedex", "FedEx shares jump"),
        _market("court", "Federal court ruling on a merger"),
        _market("jet", "Jet fuel crack spreads widen"),
        _market("wti", "WTI crude settlement above 90"),
        _market("bare", "A federal holiday"),
        _market("funds", "Federal funds rate decision", "The FOMC sets the rate"),
    ]
    kept = [market.market_id for market in candidates(markets)]
    assert kept == ["jet", "wti", "funds"]


def test_hand_labels_force_hand_source_and_overrides():
    path = ROOT / "hand_labels.yaml"
    loaded = hand_labels(str(path))
    label_row, overrides = loaded[(Venue.KALSHI, "KXFEDDECISION-26OCT-H25")]
    assert label_row.label_source == "hand"
    assert label_row.factor == Factor.RATES
    assert overrides["kind"] == MarketKind.PER_MEETING.value
    assert overrides["outcome"] == "hike"
    assert overrides["group_key"] == "KXFEDDECISION-26OCT"
    assert overrides["threshold"] == 25


def test_duplicate_hand_label_is_rejected(tmp_path):
    path = tmp_path / "labels.yaml"
    path.write_text(
        yaml.dump(
            [
                {"venue": "kalshi", "market_id": "M", "relevant": False, "group_key": "2026-10-13"},
                {"venue": "kalshi", "market_id": "M", "relevant": False, "group_key": "2026-10-13"},
            ]
        )
    )
    with pytest.raises(ValueError, match="duplicate"):
        hand_labels(str(path))


def test_checked_in_shortlist_covers_the_fed_meeting_and_wti_ladder():
    loaded = hand_labels(str(ROOT / "hand_labels.yaml"))
    assert 15 <= len(loaded) <= 30
    by_outcome = {}
    wti = []
    for (venue, market_id), (row, overrides) in loaded.items():
        assert venue == Venue.KALSHI
        assert row.label_source == "hand"
        assert len(row.mechanism) <= 200
        if overrides.get("outcome"):
            by_outcome[overrides["outcome"]] = market_id
        if overrides.get("group_key") == "KXWTI-26OCT1314" and overrides.get("kind") == "close_above":
            assert row.factor == Factor.FUEL
            assert row.yes_moves_factor == "up"
            wti.append(market_id)
    assert by_outcome["hold"] == "KXFEDDECISION-26OCT-H0"
    assert by_outcome["hike"] == "KXFEDDECISION-26OCT-H25"
    assert by_outcome["cut"] == "KXFEDDECISION-26OCT-C25"
    assert len(wti) >= 3
    assert "KXWTI-26OCT1314-T100.99" in wti
    assert "KXFEDDECISION-26OCT-H25" in {market_id for _, market_id in loaded}


def test_llm_labeling_is_still_b10():
    with pytest.raises(NotImplementedError, match="B10"):
        label(_market("M", "WTI"))
