"""Scenario grid, golden fixture, edges, and property checks. LLD section 6."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from airscen.factors import fuel_curve
from airscen.scenario import run_scenarios
from airscen.types import (
    DataError,
    Factor,
    FuelCurve,
    MarketKind,
    Metric,
    RateStates,
    ScenarioInputs,
)

from tests.model.conftest import make_fact, make_quote

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load_worked():
    return json.loads((FIXTURES / "worked_example.json").read_text())


def _as_of(doc):
    from datetime import datetime

    return datetime.fromisoformat(doc["as_of"].replace("Z", "+00:00"))


def _baseline(facts, *, gallons=None, operating_income=None, net_interest=None, include_liquidity=True):
    values = dict(facts)
    if gallons is not None:
        values["fuel_gallons_m"] = gallons
    if operating_income is not None:
        values["operating_income_usd_m"] = operating_income
    if net_interest is not None:
        values["net_interest_expense_usd_m"] = net_interest
    baseline = {
        Metric.OPERATING_INCOME: make_fact(Metric.OPERATING_INCOME, values["operating_income_usd_m"]),
        Metric.NET_INTEREST_EXPENSE: make_fact(
            Metric.NET_INTEREST_EXPENSE, values["net_interest_expense_usd_m"]
        ),
        Metric.FUEL_GALLONS_M: make_fact(Metric.FUEL_GALLONS_M, values["fuel_gallons_m"], unit="gallons_m"),
        Metric.DEBT_TOTAL: make_fact(Metric.DEBT_TOTAL, values["debt_total_usd_m"]),
        Metric.VARIABLE_DEBT_SHARE: make_fact(
            Metric.VARIABLE_DEBT_SHARE, values["variable_debt_share"], unit="fraction"
        ),
    }
    if include_liquidity:
        baseline[Metric.LIQUIDITY] = make_fact(Metric.LIQUIDITY, values["liquidity_usd_m"])
        baseline[Metric.DEBT_CURRENT] = make_fact(Metric.DEBT_CURRENT, values["debt_current_usd_m"])
    return baseline


def _curve(doc):
    as_of = _as_of(doc)
    quotes = [
        make_quote(
            factor=Factor.FUEL,
            mid=point["p"],
            market_id=f"WTI-{point['k']}",
            kind=MarketKind.CLOSE_ABOVE,
            threshold=point["k"],
            group_key="illustrative",
            close_time=as_of + timedelta(days=30),
        )
        for point in doc["inputs"]["ladder"]["thresholds"]
    ]
    curve, _flags = fuel_curve(quotes, as_of)
    assert curve is not None
    return curve


def _inputs(doc, **overrides):
    raw = doc["inputs"]
    fields = {
        "company": doc["company"],
        "as_of": _as_of(doc),
        "note_size_usd_m": raw["note_size_usd_m"],
        "tenor_years": raw["tenor_years"],
        "coupon": raw["coupon"],
        "coverage_threshold": raw["coverage_threshold"],
        "fuel_passthrough": raw["fuel_passthrough"],
        "baseline": _baseline(raw["facts"]),
        "wti_ref": raw["wti_ref"],
        "rates": RateStates(
            p_hike=raw["rate_quotes"]["hike"],
            delta_hike_bp=25,
            source_group="illustrative",
            venue_gap=None,
        ),
        "fuel": _curve(doc),
    }
    fields.update(overrides)
    return ScenarioInputs(**fields)


def test_scenario_golden():
    doc = _load_worked()
    rows, summary = run_scenarios(_inputs(doc))
    expected_rows = doc["expected"]["rows"]
    expected = doc["expected"]["summary"]

    assert [(row.fuel_label, row.rate_label) for row in rows] == [
        (row["fuel_label"], row["rate_label"]) for row in expected_rows
    ]
    for row, want in zip(rows, expected_rows):
        assert row.wti == want["wti"]
        assert round(row.probability, 3) == want["probability"]
        assert round(row.operating_income_usd_m, 2) == want["operating_income_usd_m"]
        assert round(row.net_interest_usd_m, 2) == want["net_interest_usd_m"]
        assert round(row.coverage, 2) == want["coverage"]
        assert row.breach is want["breach"]

    assert round(summary.p_breach, 3) == expected["p_breach"]
    assert round(summary.breakeven_wti_hold, 2) == expected["breakeven_wti_hold"]
    assert round(summary.breakeven_wti_hike, 2) == expected["breakeven_wti_hike"]
    assert round(summary.coverage_p50, 2) == expected["coverage_p50"]
    assert round(summary.coverage_p90, 2) == expected["coverage_p90"]
    assert round(summary.liquidity_cover, 2) == expected["liquidity_cover"]
    assert summary.liquidity_ok is True
    assert abs(sum(row.probability for row in rows) - 1) < 1e-9


def test_missing_required_fact_and_non_positive_gallons():
    doc = _load_worked()
    inputs = _inputs(doc)
    baseline = dict(inputs.baseline)
    del baseline[Metric.VARIABLE_DEBT_SHARE]
    with pytest.raises(DataError) as missing:
        run_scenarios(inputs.model_copy(update={"baseline": baseline}))
    assert missing.value.code == "E_MISSING_FACT"
    assert missing.value.details["missing"] == ["variable_debt_share"]

    only_income = {Metric.OPERATING_INCOME: inputs.baseline[Metric.OPERATING_INCOME]}
    with pytest.raises(DataError) as several:
        run_scenarios(inputs.model_copy(update={"baseline": only_income}))
    assert several.value.details["missing"] == [
        "net_interest_expense",
        "fuel_gallons_m",
        "debt_total",
        "variable_debt_share",
    ]

    zero_gallons = _baseline(doc["inputs"]["facts"], gallons=0)
    with pytest.raises(DataError) as gallons:
        run_scenarios(inputs.model_copy(update={"baseline": zero_gallons}))
    assert gallons.value.code == "E_MISSING_FACT"
    assert gallons.value.details["missing"] == ["fuel_gallons_m"]


def test_missing_fuel_nulls_probability_and_still_reports_breakevens():
    doc = _load_worked()
    rows, summary = run_scenarios(_inputs(doc, fuel=None))
    assert len(rows) == 6
    assert all(row.probability is None for row in rows)
    assert all(row.wti == doc["inputs"]["wti_ref"] for row in rows)
    assert summary.p_breach is None
    assert round(summary.breakeven_wti_hold, 2) == 103.16
    assert round(summary.breakeven_wti_hike, 2) == 102.77


def test_missing_rates_are_fixed_at_hold():
    doc = _load_worked()
    rows, summary = run_scenarios(_inputs(doc, rates=None))
    assert all(row.probability is None for row in rows)
    hold_ni = {row.net_interest_usd_m for row in rows if row.rate_label == "hold"}
    hike_ni = {row.net_interest_usd_m for row in rows if row.rate_label == "hike"}
    assert hold_ni == hike_ni
    assert round(summary.p_breach, 3) == 0.296
    assert round(summary.breakeven_wti_hold, 2) == 103.16
    assert round(summary.breakeven_wti_hike, 2) == 103.16


def test_zero_passthrough_has_no_finite_breakeven():
    doc = _load_worked()
    _rows, clear = run_scenarios(_inputs(doc, fuel_passthrough=0))
    assert clear.breakeven_wti_hold is None
    assert clear.breakeven_wti_hike is None
    assert clear.p_breach == 0

    _rows, certain = run_scenarios(_inputs(doc, fuel_passthrough=0, coverage_threshold=20))
    assert certain.p_breach == 1
    assert certain.breakeven_wti_hold is None


def test_non_positive_operating_income_breaches_even_when_the_ratio_looks_high():
    doc = _load_worked()
    baseline = _baseline(
        doc["inputs"]["facts"],
        operating_income=-10_000,
        net_interest=-100,
    )
    rows, _summary = run_scenarios(_inputs(doc, baseline=baseline))
    assert rows
    assert all(row.operating_income_usd_m <= 0 for row in rows)
    assert all(row.coverage > doc["inputs"]["coverage_threshold"] for row in rows)
    assert all(row.breach for row in rows)


def test_liquidity_is_null_without_both_facts_or_when_current_debt_is_zero():
    doc = _load_worked()
    _rows, summary = run_scenarios(_inputs(doc, baseline=_baseline(doc["inputs"]["facts"], include_liquidity=False)))
    assert summary.liquidity_cover is None
    assert summary.liquidity_ok is None

    baseline = _baseline(doc["inputs"]["facts"])
    baseline[Metric.DEBT_CURRENT] = make_fact(Metric.DEBT_CURRENT, 0)
    _rows, zero = run_scenarios(_inputs(doc, baseline=baseline))
    assert zero.liquidity_cover is None
    assert zero.liquidity_ok is None


def _fact_value(metric, value, unit):
    return make_fact(metric, value, unit=unit)


@st.composite
def _scenario_inputs(draw):
    oi0 = draw(st.floats(min_value=50_000, max_value=80_000, allow_nan=False, allow_infinity=False))
    ni0 = draw(st.floats(min_value=20, max_value=400, allow_nan=False, allow_infinity=False))
    gallons = draw(st.floats(min_value=200, max_value=2_000, allow_nan=False, allow_infinity=False))
    debt = draw(st.floats(min_value=1_000, max_value=40_000, allow_nan=False, allow_infinity=False))
    variable = draw(st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False))
    note = draw(st.floats(min_value=100, max_value=5_000, allow_nan=False, allow_infinity=False))
    coupon = draw(st.floats(min_value=0.01, max_value=0.12, allow_nan=False, allow_infinity=False))
    theta = draw(st.floats(min_value=0.5, max_value=12, allow_nan=False, allow_infinity=False))
    kappa = draw(st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False))
    wti_ref = draw(st.floats(min_value=50, max_value=100, allow_nan=False, allow_infinity=False))
    p_hike = draw(st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False))
    start = draw(st.floats(min_value=40, max_value=70, allow_nan=False, allow_infinity=False))
    gaps = draw(st.lists(
        st.floats(min_value=5, max_value=15, allow_nan=False, allow_infinity=False),
        min_size=4,
        max_size=4,
    ))
    levels = [start]
    for gap in gaps:
        levels.append(levels[-1] + gap)
    probs = sorted(
        draw(st.lists(
            st.floats(min_value=0.05, max_value=0.95, allow_nan=False, allow_infinity=False),
            min_size=5,
            max_size=5,
        )),
        reverse=True,
    )
    spacing = min(gaps)
    raw = [(levels[0] - spacing, 1.0)]
    previous = 1.0
    for level, prob in zip(levels, probs):
        previous = min(prob, previous)
        raw.append((level, previous))
    raw.append((levels[-1] + spacing, 0.0))
    fuel = FuelCurve(points=raw, resolve_group="hyp", n_markets=5)
    baseline = {
        Metric.OPERATING_INCOME: _fact_value(Metric.OPERATING_INCOME, oi0, "usd_m"),
        Metric.NET_INTEREST_EXPENSE: _fact_value(Metric.NET_INTEREST_EXPENSE, ni0, "usd_m"),
        Metric.FUEL_GALLONS_M: _fact_value(Metric.FUEL_GALLONS_M, gallons, "gallons_m"),
        Metric.DEBT_TOTAL: _fact_value(Metric.DEBT_TOTAL, debt, "usd_m"),
        Metric.VARIABLE_DEBT_SHARE: _fact_value(Metric.VARIABLE_DEBT_SHARE, variable, "fraction"),
    }
    return ScenarioInputs(
        company="DAL",
        as_of=_as_of({"as_of": "2026-10-09T14:30:00Z"}),
        note_size_usd_m=note,
        tenor_years=7,
        coupon=coupon,
        coverage_threshold=theta,
        fuel_passthrough=kappa,
        baseline=baseline,
        wti_ref=wti_ref,
        rates=RateStates(p_hike=p_hike, delta_hike_bp=25, source_group="hyp", venue_gap=None),
        fuel=fuel,
    )


def _assert_probabilities(rows):
    probs = [row.probability for row in rows]
    assert len(rows) == 6
    if all(prob is None for prob in probs):
        return
    assert all(prob is not None for prob in probs)
    assert abs(sum(probs) - 1) < 1e-9


@settings(max_examples=40, deadline=None, derandomize=True)
@given(_scenario_inputs())
def test_probabilities_sum_to_one_or_are_all_null(inputs):
    rows, _summary = run_scenarios(inputs)
    _assert_probabilities(rows)
    for dropped in ({"fuel": None}, {"rates": None}):
        blank, _summary = run_scenarios(inputs.model_copy(update=dropped))
        assert [row.probability for row in blank] == [None] * 6


@settings(max_examples=40, deadline=None, derandomize=True)
@given(_scenario_inputs(), st.floats(min_value=100, max_value=2_000, allow_nan=False, allow_infinity=False))
def test_coverage_never_rises_with_note_size(inputs, extra):
    larger = inputs.model_copy(update={"note_size_usd_m": inputs.note_size_usd_m + extra})
    before, _ = run_scenarios(inputs)
    after, _ = run_scenarios(larger)
    _assert_probabilities(before)
    for left, right in zip(before, after):
        assert right.coverage <= left.coverage + 1e-6


@settings(max_examples=40, deadline=None, derandomize=True)
@given(_scenario_inputs(), st.floats(min_value=1, max_value=10, allow_nan=False, allow_infinity=False))
def test_coverage_never_rises_with_wti(inputs, shift):
    moved = FuelCurve(
        points=[(strike + shift, prob) for strike, prob in inputs.fuel.points],
        resolve_group=inputs.fuel.resolve_group,
        n_markets=inputs.fuel.n_markets,
    )
    after_inputs = inputs.model_copy(update={"fuel": moved})
    before, _ = run_scenarios(inputs)
    after, _ = run_scenarios(after_inputs)
    for left, right in zip(before, after):
        assert right.wti + 1e-9 >= left.wti
        assert right.coverage <= left.coverage + 1e-6
