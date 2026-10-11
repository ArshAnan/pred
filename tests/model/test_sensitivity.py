"""One-at-a-time assumption sweeps on the illustrative worked example."""

import pytest

from airscen.sensitivity import sweep_assumptions
from airscen.types import RateStates, ScenarioInputs
from tests.model.test_scenario import _as_of, _baseline, _curve, _load_worked


def _base():
    doc = _load_worked()
    inputs = doc["inputs"]
    return ScenarioInputs(
        company="DAL",
        as_of=_as_of(doc),
        note_size_usd_m=inputs["note_size_usd_m"],
        tenor_years=inputs["tenor_years"],
        coupon=inputs["coupon"],
        coverage_threshold=inputs["coverage_threshold"],
        fuel_passthrough=inputs["fuel_passthrough"],
        baseline=_baseline(inputs["facts"]),
        wti_ref=inputs["wti_ref"],
        rates=RateStates(p_hike=doc["expected"]["pi_hike"], source_group="illustrative", venue_gap=None),
        fuel=_curve(doc),
    )


def _report():
    # DGS10 is 4.00 in the worked-example coupon: 0.055 = (4.00 + 1.50) / 100.
    return sweep_assumptions(_base(), dgs10=4.0, spread_bp=150.0, line=0.20, run_id="run_test", company="DAL")


def _lever(report, name):
    return next(lever for lever in report.levers if lever.name == name)


def _probs(lever):
    return [point.p_breach for point in lever.points]


def test_baseline_matches_the_worked_example():
    report = _report()
    assert report.baseline_p_breach == pytest.approx(0.297, abs=0.0005)
    threshold = _lever(report, "coverage_threshold")
    stated = next(point for point in threshold.points if point.is_baseline)
    assert stated.value == 6.0
    assert stated.p_breach == pytest.approx(0.297, abs=0.0005)
    assert stated.delta_p_breach == pytest.approx(0.0, abs=1e-12)


def test_larger_note_spread_and_threshold_raise_breach_probability():
    report = _report()
    for name in ("note_size_usd_m", "coupon_spread_bp", "coverage_threshold"):
        probs = _probs(_lever(report, name))
        assert all(p is not None for p in probs)
        assert probs == sorted(probs)


def test_threshold_crosses_the_alert_line_between_3x_and_6x():
    """The demo flip uses 3.0 (about 7%) and 6.0 (about 30%). The sweep must land between them."""
    report = _report()
    lever = _lever(report, "coverage_threshold")
    by_value = {point.value: point.p_breach for point in lever.points}
    assert by_value[3.0] < 0.20 < by_value[6.0]
    assert lever.flip_value == pytest.approx(4.5, abs=1.0)
    assert 3.0 < lever.flip_value < 6.0
    assert report.call_stable is False
    assert "other side of the alert line" in report.reading


def test_zero_note_is_below_the_stated_note():
    report = _report()
    lever = _lever(report, "note_size_usd_m")
    zero = next(point for point in lever.points if point.value == 0)
    stated = next(point for point in lever.points if point.is_baseline)
    assert zero.p_breach < stated.p_breach


def test_most_sensitive_is_the_largest_swing():
    report = _report()
    swings = {lever.name: lever.swing for lever in report.levers}
    assert report.most_sensitive == max(swings, key=swings.get)
    assert report.most_sensitive in report.reading or _lever(report, report.most_sensitive).label in report.reading


def test_missing_fuel_curve_has_no_breach_probability():
    base = _base().model_copy(update={"fuel": None})
    report = sweep_assumptions(base, dgs10=4.0, spread_bp=150.0, line=0.20, run_id="run_test", company="DAL")
    assert report.baseline_p_breach is None
    assert report.call_stable is False
    assert report.most_sensitive is None
    assert "no breach probability" in report.reading
