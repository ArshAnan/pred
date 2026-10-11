"""One-at-a-time sweeps of the assumptions a user states on a run.

Pure aside from scenario.run_scenarios. No network, database, or clock.
The fuel curve and rate odds stay at the run; only the named assumption moves.
"""

from __future__ import annotations

from airscen.scenario import run_scenarios
from airscen.types import ScenarioInputs, SensitivityLever, SensitivityPoint, SensitivityReport

LINE_NAMES = (
    "note_size_usd_m",
    "coupon_spread_bp",
    "coverage_threshold",
    "fuel_passthrough",
)

_LABELS = {
    "note_size_usd_m": "New note size",
    "coupon_spread_bp": "Coupon spread",
    "coverage_threshold": "Coverage threshold",
    "fuel_passthrough": "Fuel pass-through",
}
_UNITS = {
    "note_size_usd_m": "USD m",
    "coupon_spread_bp": "bp",
    "coverage_threshold": "x",
    "fuel_passthrough": "pass-through",
}
_NOTE_GRID = (0.0, 500.0, 1000.0, 1500.0, 2000.0, 3000.0, 4000.0, 5000.0)
_SPREAD_GRID = (0.0, 50.0, 100.0, 150.0, 250.0, 400.0, 600.0, 1000.0)
_THRESHOLD_GRID = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 12.0)
_PASS_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)


def sweep_assumptions(
    base: ScenarioInputs,
    *,
    dgs10: float,
    spread_bp: float,
    line: float,
    run_id: str,
    company: str,
) -> SensitivityReport:
    """Sweep note size, coupon spread, coverage threshold, and fuel pass-through.

    `dgs10` is the 10-year yield, in percent, already used to price `base.coupon`.
    `line` is the breach-probability alert line (config `alerts.breach_threshold`).
    The flip value is the sweep point closest to the stated assumption where the
    probability crosses that line, interpolated between the two samples around it.
    """
    baselines = {
        "note_size_usd_m": base.note_size_usd_m,
        "coupon_spread_bp": spread_bp,
        "coverage_threshold": base.coverage_threshold,
        "fuel_passthrough": base.fuel_passthrough,
    }
    grids = {
        "note_size_usd_m": _NOTE_GRID,
        "coupon_spread_bp": _SPREAD_GRID,
        "coverage_threshold": _THRESHOLD_GRID,
        "fuel_passthrough": _PASS_GRID,
    }
    base_summary = run_scenarios(base)[1]
    baseline_p = base_summary.p_breach
    levers = [
        _lever(base, name, baselines[name], grids[name], dgs10, line, baseline_p)
        for name in LINE_NAMES
    ]
    levers.sort(key=lambda lever: (-(lever.swing if lever.swing is not None else -1.0), lever.name))
    most = next((lever.name for lever in levers if lever.swing is not None), None)
    call = None if baseline_p is None else baseline_p >= line
    stable = call is not None and all(
        point.above_line is call for lever in levers for point in lever.points
    )
    ordered = {lever.name: lever for lever in levers}
    report_levers = [ordered[name] for name in sorted(ordered, key=lambda name: (
        -(ordered[name].swing if ordered[name].swing is not None else -1.0), name,
    ))]
    return SensitivityReport(
        run_id=run_id,
        company=company,
        as_of=base.as_of,
        line=line,
        baseline_p_breach=baseline_p,
        most_sensitive=most,
        call_stable=stable,
        reading=_reading(baseline_p, line, most, report_levers, stable),
        levers=report_levers,
    )


def _grid(baseline: float, values: tuple[float, ...]) -> list[float]:
    return sorted({baseline, *values})


def _apply(base: ScenarioInputs, name: str, value: float, dgs10: float) -> ScenarioInputs:
    if name == "coupon_spread_bp":
        return base.model_copy(update={"coupon": (dgs10 + value / 100) / 100})
    return base.model_copy(update={name: value})


def _lever(
    base: ScenarioInputs,
    name: str,
    baseline: float,
    grid: tuple[float, ...],
    dgs10: float,
    line: float,
    baseline_p: float | None,
) -> SensitivityLever:
    points: list[SensitivityPoint] = []
    for value in _grid(baseline, grid):
        summary = run_scenarios(_apply(base, name, value, dgs10))[1]
        p = summary.p_breach
        points.append(SensitivityPoint(
            value=value,
            is_baseline=value == baseline,
            p_breach=p,
            delta_p_breach=None if p is None or baseline_p is None else p - baseline_p,
            coverage_p50=summary.coverage_p50,
            breakeven_wti_hold=summary.breakeven_wti_hold,
            above_line=None if p is None else p >= line,
        ))
    finite = [point.p_breach for point in points if point.p_breach is not None]
    swing = max(finite) - min(finite) if len(finite) >= 2 else None
    return SensitivityLever(
        name=name,
        label=_LABELS[name],
        unit=_UNITS[name],
        baseline_value=baseline,
        swing=swing,
        flip_value=_flip(points, baseline, line),
        points=points,
    )


def _flip(points: list[SensitivityPoint], baseline: float, line: float) -> float | None:
    """Lever value closest to the stated assumption where breach probability crosses `line`."""
    best: float | None = None
    best_dist: float | None = None
    for left, right in zip(points, points[1:]):
        if left.p_breach is None or right.p_breach is None:
            continue
        if left.p_breach == line:
            candidate = left.value
        elif right.p_breach == line:
            candidate = right.value
        elif (left.p_breach - line) * (right.p_breach - line) < 0:
            t = (line - left.p_breach) / (right.p_breach - left.p_breach)
            candidate = left.value + t * (right.value - left.value)
        else:
            continue
        dist = abs(candidate - baseline)
        if best_dist is None or dist < best_dist:
            best, best_dist = candidate, dist
    return best


def _reading(
    baseline_p: float | None,
    line: float,
    most: str | None,
    levers: list[SensitivityLever],
    stable: bool,
) -> str:
    if baseline_p is None:
        return (
            "This run has no breach probability, so the sweep cannot say whether the call would flip. "
            "That happens when the fuel curve is missing."
        )
    side = "at or above" if baseline_p >= line else "below"
    sentences = [
        f"At the stated assumptions, the breach probability is {baseline_p * 100:.1f}%, "
        f"{side} the {line * 100:.0f}% alert line.",
        "Each lever moves one assumption and leaves the fuel curve, the Fed odds, and the other assumptions at this run.",
    ]
    chosen = next((lever for lever in levers if lever.name == most), None)
    if chosen is None or chosen.swing is None:
        sentences.append("No lever produced a sweep with a breach probability.")
    else:
        sentences.append(
            f"{chosen.label} moves the breach probability the most, "
            f"by {chosen.swing * 100:.1f} percentage points across the sweep."
        )
        if chosen.flip_value is None:
            sentences.append("It does not cross the alert line anywhere in the sweep.")
        else:
            sentences.append(
                f"It crosses the alert line at {chosen.flip_value:.2f} {chosen.unit}."
            )
    if stable:
        sentences.append("Every sampled assumption keeps the same side of the alert line as the stated question.")
    else:
        sentences.append("At least one sampled assumption puts the call on the other side of the alert line.")
    return " ".join(sentences)
