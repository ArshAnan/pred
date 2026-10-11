"""Scenario grid and summary.

Pure functions. Import only airscen.types, the standard library, and numpy.
No network, database, or clock reads. Survival math is duplicated here
because this module cannot import factors.
"""

import math
from dataclasses import dataclass

from airscen.types import (
    Basis,
    DataError,
    FuelCurve,
    Metric,
    ScenarioInputs,
    ScenarioRow,
    ScenarioSummary,
)

_REQUIRED = (
    Metric.OPERATING_INCOME,
    Metric.NET_INTEREST_EXPENSE,
    Metric.FUEL_GALLONS_M,
    Metric.DEBT_TOTAL,
    Metric.VARIABLE_DEBT_SHARE,
)
_FUEL_LABELS = ("P10", "P50", "P90")
_WEIGHTS = (0.3, 0.4, 0.3)
_BARREL = 42.0
_LIQUIDITY_MIN = 1.0


def _survival(points: list[tuple[float, float]], wti: float) -> float:
    ordered = sorted(points, key=lambda point: point[0])
    if not ordered:
        return 0.0
    if wti <= ordered[0][0]:
        return ordered[0][1]
    if wti >= ordered[-1][0]:
        return ordered[-1][1]
    for (k0, p0), (k1, p1) in zip(ordered, ordered[1:]):
        if k0 <= wti <= k1:
            if k1 == k0:
                return p0
            return p0 + (wti - k0) / (k1 - k0) * (p1 - p0)
    return ordered[-1][1]


def _quantile(points: list[tuple[float, float]], target: float) -> float:
    ordered = sorted(points, key=lambda point: point[0])
    if not ordered:
        return 0.0
    for strike, prob in ordered:
        if prob == target:
            return strike
    if target >= ordered[0][1]:
        return ordered[0][0]
    if target <= ordered[-1][1]:
        return ordered[-1][0]
    for (k0, p0), (k1, p1) in zip(ordered, ordered[1:]):
        if p0 > target >= p1 and p0 != p1:
            return k0 + (p0 - target) / (p0 - p1) * (k1 - k0)
    return ordered[-1][0]


def _presentation_wti(fuel: FuelCurve | None, wti_ref: float) -> tuple[float, float, float]:
    if fuel is None:
        return (wti_ref, wti_ref, wti_ref)
    points = list(fuel.points)
    return (
        round(_quantile(points, 0.90), 2),
        round(_quantile(points, 0.50), 2),
        round(_quantile(points, 0.10), 2),
    )


def _net_interest(ni0: float, variable: float, debt: float, d_r: float, note: float, coupon: float) -> float:
    return ni0 + variable * debt * d_r / 4 + note * coupon / 4


def _breakeven(oi0: float, theta: float, ni: float, kappa: float, gallons: float, wti_ref: float) -> float | None:
    if kappa == 0:
        return None
    value = wti_ref + _BARREL * (oi0 - theta * ni) / (kappa * gallons)
    return value if math.isfinite(value) else None


def _missing_facts(baseline: dict) -> list[str]:
    missing = [metric.value for metric in _REQUIRED if metric not in baseline]
    gallons = baseline.get(Metric.FUEL_GALLONS_M)
    if gallons is not None and gallons.value <= 0 and Metric.FUEL_GALLONS_M.value not in missing:
        missing.append(Metric.FUEL_GALLONS_M.value)
    return missing


def run_scenarios(inputs: ScenarioInputs) -> tuple[list[ScenarioRow], ScenarioSummary]:
    """Build the six-row grid.

    Raises DataError with code E_MISSING_FACT when operating income, net
    interest, gallons, debt, or the variable-rate share is missing, and
    when gallons are not positive. Fuel and rate gaps leave probabilities
    null instead of raising. A zero fuel pass-through stores breakeven WTI
    as None.
    """
    missing = _missing_facts(inputs.baseline)
    if missing:
        raise DataError(
            "Missing required facts: " + ", ".join(missing),
            code="E_MISSING_FACT",
            details={"missing": missing},
        )

    baseline = inputs.baseline
    oi0 = baseline[Metric.OPERATING_INCOME].value
    ni0 = baseline[Metric.NET_INTEREST_EXPENSE].value
    gallons = baseline[Metric.FUEL_GALLONS_M].value
    debt = baseline[Metric.DEBT_TOTAL].value
    variable = baseline[Metric.VARIABLE_DEBT_SHARE].value
    theta = inputs.coverage_threshold
    kappa = inputs.fuel_passthrough

    rates = inputs.rates
    rates_missing = rates is None or rates.p_hike is None
    if rates is None or rates.p_hike is None:
        pi_hike = 0.0
        pi_hold = 1.0
        hike_decimal = 0.0
    else:
        pi_hike = rates.p_hike
        pi_hold = 1.0 - pi_hike
        hike_decimal = rates.delta_hike_bp / 10_000

    fuel_missing = inputs.fuel is None
    wtis = _presentation_wti(inputs.fuel, inputs.wti_ref)
    ni_hold = _net_interest(ni0, variable, debt, 0.0, inputs.note_size_usd_m, inputs.coupon)
    ni_hike = _net_interest(ni0, variable, debt, hike_decimal, inputs.note_size_usd_m, inputs.coupon)
    breakeven_hold = _breakeven(oi0, theta, ni_hold, kappa, gallons, inputs.wti_ref)
    breakeven_hike = _breakeven(oi0, theta, ni_hike, kappa, gallons, inputs.wti_ref)

    rows: list[ScenarioRow] = []
    for rate_label, pi, ni in (
        ("hold", pi_hold, ni_hold),
        ("hike", pi_hike, ni_hike),
    ):
        for fuel_label, wti, weight in zip(_FUEL_LABELS, wtis, _WEIGHTS):
            d_p = (wti - inputs.wti_ref) / _BARREL
            oi = oi0 - kappa * gallons * d_p
            if ni == 0:
                coverage = math.inf if oi > 0 else 0.0
            else:
                coverage = oi / ni
            probability = None if fuel_missing or rates_missing else weight * pi
            rows.append(ScenarioRow(
                fuel_label=fuel_label,
                rate_label=rate_label,
                wti=wti,
                probability=probability,
                operating_income_usd_m=oi,
                net_interest_usd_m=ni,
                coverage=coverage,
                breach=oi <= 0 or coverage < theta,
            ))

    if fuel_missing:
        p_breach = None
    elif kappa == 0:
        p_breach = pi_hold * (0.0 if oi0 > theta * ni_hold else 1.0)
        p_breach += pi_hike * (0.0 if oi0 > theta * ni_hike else 1.0)
    else:
        points = list(inputs.fuel.points) if inputs.fuel is not None else []
        w_hold = inputs.wti_ref if breakeven_hold is None else breakeven_hold
        w_hike = inputs.wti_ref if breakeven_hike is None else breakeven_hike
        p_breach = pi_hold * _survival(points, w_hold) + pi_hike * _survival(points, w_hike)

    hold_rows = [row for row in rows if row.rate_label == "hold"]
    liquidity = baseline.get(Metric.LIQUIDITY)
    current = baseline.get(Metric.DEBT_CURRENT)
    if liquidity is None or current is None or current.value == 0:
        liquidity_cover = None
        liquidity_ok = None
    else:
        liquidity_cover = liquidity.value / current.value
        liquidity_ok = liquidity_cover >= _LIQUIDITY_MIN

    summary = ScenarioSummary(
        p_breach=p_breach,
        breakeven_wti_hold=breakeven_hold,
        breakeven_wti_hike=breakeven_hike,
        coverage_p50=hold_rows[1].coverage,
        coverage_p90=hold_rows[2].coverage,
        liquidity_cover=liquidity_cover,
        liquidity_ok=liquidity_ok,
    )
    return rows, summary


@dataclass(frozen=True)
class MarginComparison:
    """Operating margin for each company, on one shared revenue basis."""

    basis: Basis
    margins: dict[str, float]
    flags: list[str]


def compare_margins(companies: list[tuple[str, dict]]) -> MarginComparison:
    """Compare operating income over revenue.

    Adjusted revenue is used when every company in the comparison has it.
    Otherwise the comparison is GAAP and the result carries
    MARGIN_BASIS_MISMATCH. Margin is operating income divided by that revenue.
    """
    have_adjusted = bool(companies) and all(
        Metric.REVENUE_ADJUSTED in book for _, book in companies
    )
    basis = Basis.ADJUSTED if have_adjusted else Basis.GAAP
    revenue_metric = Metric.REVENUE_ADJUSTED if have_adjusted else Metric.REVENUE_GAAP
    flags = [] if have_adjusted else ["MARGIN_BASIS_MISMATCH"]
    margins: dict[str, float] = {}
    for ticker, book in companies:
        revenue = book.get(revenue_metric)
        income = book.get(Metric.OPERATING_INCOME)
        if revenue is None or income is None or revenue.value == 0:
            continue
        margins[ticker] = income.value / revenue.value
    return MarginComparison(basis=basis, margins=margins, flags=flags)
