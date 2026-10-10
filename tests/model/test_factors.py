"""Rate states and the WTI survival curve. LLD section 6."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from airscen.factors import fuel_curve, fuel_states, rate_states
from airscen.types import Factor, FuelCurve, MarketKind, Venue

from tests.model.conftest import AS_OF, make_quote

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _as_of(text="2026-10-09T14:30:00Z"):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _meeting(mids, group="FED-26OCT", venue=Venue.KALSHI, score=0.90, close_time=None, excluded=None):
    when = AS_OF + timedelta(days=20) if close_time is None else close_time
    return [
        make_quote(
            factor=Factor.RATES,
            mid=mid,
            market_id=f"{venue.value}-{group}-{outcome}",
            kind=MarketKind.PER_MEETING,
            outcome=outcome,
            group_key=group,
            venue=venue,
            score=score,
            close_time=when,
            excluded=excluded,
        )
        for outcome, mid in mids.items()
    ]


def _ladder(levels, probs, group="ladder", close_time=None, direction="up", score=0.90):
    when = AS_OF + timedelta(days=30) if close_time is None else close_time
    return [
        make_quote(
            factor=Factor.FUEL,
            mid=prob,
            market_id=f"{group}-{level}",
            kind=MarketKind.CLOSE_ABOVE,
            threshold=level,
            group_key=group,
            close_time=when,
            direction=direction,
            score=score,
        )
        for level, prob in zip(levels, probs)
    ]


def test_illustrative_rates_normalise_and_a_missing_hike_returns_the_flag():
    states, flags = rate_states(_meeting({"hold": 0.82, "hike": 0.17, "cut": 0.01}))
    assert states is not None
    assert states.p_hike == pytest.approx(0.17)
    assert 1 - states.p_hike == pytest.approx(0.83)
    assert states.delta_hike_bp == 25
    assert states.venue_gap is None
    assert flags == []

    assert rate_states(_meeting({"hold": 0.82, "cut": 0.01})) == (None, ["RATES_NO_MARKET_INPUT"])


def test_missing_cut_is_zero_and_prices_above_one_are_normalised():
    states, flags = rate_states(_meeting({"hold": 0.80, "hike": 0.20}))
    assert states is not None
    assert states.p_hike == pytest.approx(0.20)
    assert 1 - states.p_hike == pytest.approx(0.80)
    assert flags == []

    summed, summed_flags = rate_states(_meeting({"hold": 0.84, "hike": 0.18}))
    assert summed is not None
    assert summed.p_hike == pytest.approx(0.18 / 1.02)
    assert 1 - summed.p_hike == pytest.approx(0.84 / 1.02)
    assert summed_flags == []


def test_kalshi_wins_a_trust_tie_and_a_wide_gap_is_flagged():
    kalshi = _meeting({"hold": 0.82, "hike": 0.17, "cut": 0.01}, score=0.80)
    poly = _meeting({"hold": 0.80, "hike": 0.20}, venue=Venue.POLYMARKET, score=0.80)
    states, flags = rate_states(kalshi + poly)
    assert states is not None
    assert states.p_hike == pytest.approx(0.17, abs=1e-9)
    assert states.venue_gap == pytest.approx(0.03, abs=1e-9)
    assert flags == []

    louder = _meeting(
        {"hold": 0.20, "hike": 0.70, "cut": 0.10},
        venue=Venue.POLYMARKET,
        score=0.60,
    )
    chosen, disagree = rate_states(
        _meeting({"hold": 0.82, "hike": 0.17, "cut": 0.01}, score=0.95) + louder
    )
    assert chosen is not None
    assert chosen.p_hike == pytest.approx(0.17, abs=1e-9)
    assert chosen.venue_gap == pytest.approx(0.53, abs=1e-9)
    assert disagree == ["VENUE_DISAGREE"]


def test_earliest_meeting_is_used_and_an_excluded_hike_does_not_count():
    early = _meeting(
        {"hold": 0.50, "hike": 0.50},
        group="EARLY",
        close_time=AS_OF + timedelta(days=5),
    )
    late = _meeting(
        {"hold": 0.82, "hike": 0.17, "cut": 0.01},
        group="LATE",
        close_time=AS_OF + timedelta(days=40),
    )
    states, flags = rate_states(early + late)
    assert states is not None
    assert states.source_group == "EARLY"
    assert states.p_hike == pytest.approx(0.50)
    assert flags == []

    stale_hike = make_quote(
        factor=Factor.RATES,
        mid=0.17,
        market_id="stale-hike",
        kind=MarketKind.PER_MEETING,
        outcome="hike",
        group_key="FED-26OCT",
        excluded="stale",
    )
    assert rate_states(_meeting({"hold": 0.82, "cut": 0.01}) + [stale_hike]) == (
        None,
        ["RATES_NO_MARKET_INPUT"],
    )


def test_ladder_fixture_quantiles_and_tails():
    doc = json.loads((FIXTURES / "ladder.json").read_text())
    as_of = _as_of(doc["as_of"])
    quotes = _ladder(
        [point["k"] for point in doc["thresholds"]],
        [point["p"] for point in doc["thresholds"]],
        group=doc["resolve_group"],
        close_time=as_of + timedelta(days=30),
    )
    curve, flags = fuel_curve(quotes, as_of)
    assert curve is not None
    assert curve.n_markets == 5
    assert curve.resolve_group == "illustrative"
    expected = [(70, 1.0), (80, 0.9), (90, 0.62), (100, 0.35), (110, 0.18), (120, 0.08), (130, 0.0)]
    assert len(curve.points) == len(expected)
    for got, want in zip(curve.points, expected):
        assert got[0] == pytest.approx(want[0])
        assert got[1] == pytest.approx(want[1])
    assert flags == ["CRUDE_ONLY", "RESOLVE_DATE_PROXY"]

    states = fuel_states(curve)
    assert (states.wti_p10, states.wti_p50, states.wti_p90) == (80.0, 94.44, 118.0)
    assert states.weights == (0.3, 0.4, 0.3)


def test_monotone_fix_median_tails_and_too_few_thresholds():
    curve, flags = fuel_curve(_ladder([80, 90, 100], [0.20, 0.90, 0.40]), AS_OF)
    assert curve is not None
    probs = [point[1] for point in curve.points]
    assert probs == pytest.approx([1.0, 0.20, 0.20, 0.20, 0.0])
    assert [point[0] for point in curve.points] == pytest.approx([70, 80, 90, 100, 110])
    assert "CRUDE_ONLY" in flags and "RESOLVE_DATE_PROXY" in flags

    spaced, _ = fuel_curve(_ladder([80, 90, 110], [0.90, 0.60, 0.20]), AS_OF)
    assert spaced is not None
    assert spaced.points[0][0] == pytest.approx(65)
    assert spaced.points[-1][0] == pytest.approx(125)

    assert fuel_curve(_ladder([80, 90], [0.90, 0.60]), AS_OF) == (None, ["FUEL_NO_MARKET_INPUT"])
    down = _ladder([80, 90], [0.90, 0.60]) + _ladder([100], [0.30], direction="down")
    assert fuel_curve(down, AS_OF) == (None, ["FUEL_NO_MARKET_INPUT"])


def test_group_selection_horizon_and_touch_cross_check():
    early = _ladder([70, 80, 90], [0.8, 0.5, 0.2], group="early", close_time=AS_OF + timedelta(days=10))
    late = _ladder([80, 90, 100, 110], [0.9, 0.6, 0.3, 0.1], group="late", close_time=AS_OF + timedelta(days=40))
    curve, _ = fuel_curve(early + late, AS_OF)
    assert curve is not None
    assert curve.resolve_group == "late"

    tied_close = AS_OF + timedelta(days=20)
    small = _ladder([80, 90, 100], [0.9, 0.5, 0.2], group="small", close_time=tied_close)
    large = _ladder([80, 90, 100, 110], [0.9, 0.6, 0.4, 0.2], group="large", close_time=tied_close)
    tied, _ = fuel_curve(small + large, AS_OF)
    assert tied is not None
    assert tied.resolve_group == "large"

    on_horizon, horizon_flags = fuel_curve(
        _ladder([80, 90, 100], [0.9, 0.5, 0.2], close_time=AS_OF + timedelta(days=100)),
        AS_OF,
    )
    assert on_horizon is not None
    assert "FUEL_NO_MARKET_INPUT" not in horizon_flags
    assert fuel_curve(
        _ladder([80, 90, 100], [0.9, 0.5, 0.2], close_time=AS_OF + timedelta(days=100, seconds=1)),
        AS_OF,
    ) == (None, ["FUEL_NO_MARKET_INPUT"])

    base = _ladder([80, 90, 100], [0.90, 0.62, 0.35])
    touch = make_quote(
        factor=Factor.FUEL,
        mid=0.50,
        market_id="touch-90",
        kind=MarketKind.TOUCH,
        threshold=90,
        group_key="ladder",
        close_time=AS_OF + timedelta(days=30),
    )
    _, flagged = fuel_curve(base + [touch], AS_OF)
    assert flagged == ["CRUDE_ONLY", "RESOLVE_DATE_PROXY", "LADDER_INCONSISTENT"]

    touch_ok = make_quote(
        factor=Factor.FUEL,
        mid=0.60,
        market_id="touch-90-ok",
        kind=MarketKind.TOUCH,
        threshold=90,
        group_key="ladder",
        close_time=AS_OF + timedelta(days=30),
    )
    _, clean = fuel_curve(base + [touch_ok], AS_OF)
    assert "LADDER_INCONSISTENT" not in clean


def test_flat_survival_segment_uses_the_left_edge():
    curve = FuelCurve(
        points=[(0.0, 1.0), (10.0, 0.5), (20.0, 0.5), (30.0, 0.0)],
        resolve_group="flat",
        n_markets=2,
    )
    states = fuel_states(curve)
    assert states.wti_p10 == 2.0
    assert states.wti_p50 == 10.0
    assert states.wti_p90 == 28.0
