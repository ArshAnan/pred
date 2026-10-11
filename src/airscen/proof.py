"""Prove a stored decision from the inputs that were visible at its as-of time.

The scenario engine writes the numbers. This module applies the published quarterly
formulas a second time (CLAUDE.md section 9), checks that every number in the brief
already appears in the run or its facts, and records which filings, yields and quotes
entered the math. It imports only `types` and `brief`, so the formula check is not
the engine calling itself.

A language model may label a market or write the brief. The formulas read facts and
quotes. `decision_numbers_from_llm` is true only when a model-extracted fact was an
input, and that fact still has to be validated or the proof fails.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from airscen.brief import check_numbers
from airscen.types import (
    DecisionProof,
    Fact,
    InputTrace,
    Metric,
    ProofCheck,
    RunResult,
    ScenarioRow,
    ScenarioSummary,
)

_BARREL = 42.0
_WEIGHTS = (("P10", 0.3, 0.90), ("P50", 0.4, 0.50), ("P90", 0.3, 0.10))
_MONEY = 0.02
_COVER = 0.02
_PROB = 0.001
_SHEET = 0.005

STATEMENT = (
    "The breach probability, breakeven oil price, coverage and liquidity cover come from the "
    "quarterly formulas. The inputs are the filing figures, Treasury yield, baseline oil price "
    "and trusted market quotes visible at the as-of time. A language model may label a market or "
    "write the brief. The formulas read those quotes and the filing figures, and a brief is shown "
    "only when every number in it already appears in the run or in a sourced fact. Quotes that "
    "fail the trust bar are left out before the formulas run."
)


@dataclass(frozen=True)
class MathBasis:
    """The numbers the formulas actually multiply. Built by the service from one as-of read."""

    oi0: float
    ni0: float
    gallons: float
    debt: float
    variable: float
    wti_ref: float
    coupon: float
    dgs10: float
    hike_bp: int
    rates_missing: bool
    pi_hike: float
    fuel_points: tuple[tuple[float, float], ...] | None
    liquidity: float | None
    debt_current: float | None


def decide(
    stored: RunResult,
    fresh: RunResult,
    basis: MathBasis,
    traces: Sequence[InputTrace],
    facts: Sequence[Fact],
) -> DecisionProof:
    """Proof of `stored` against a fresh engine pass and against the published formulas."""
    replay_ok = not _replay_problems(stored, fresh)
    checks = [
        _coupon(stored, basis),
        _scenario_rows(stored, basis),
        _breakeven(stored, basis),
        _liquidity(stored, basis),
        _breach_probability(stored, basis),
        _probability_mass(stored, basis),
        _summary_grid(stored),
        _replay(stored, fresh),
        _fingerprint(stored, fresh, replay_ok),
        _no_lookahead(traces),
        _validated_facts(traces),
        _llm_boundary(traces),
        *_brief_checks(stored, facts),
        _balance_sheet(facts),
    ]
    return _assemble(stored, checks, traces)


def undecidable(stored: RunResult, facts: Sequence[Fact], reason: str) -> DecisionProof:
    """The stored run is still there, but the inputs visible now cannot rebuild it."""
    checks = [
        ProofCheck(
            code="engine_replay",
            title="Fresh engine run",
            passed=False,
            detail=f"The inputs visible at this as-of time no longer support a run: {reason}",
        ),
        *_brief_checks(stored, facts),
    ]
    return _assemble(stored, checks, [])


def _assemble(stored: RunResult, checks: list[ProofCheck], traces: Sequence[InputTrace]) -> DecisionProof:
    model_facts = _model_facts(traces)
    n_facts = sum(1 for t in traces if t.kind == "fact")
    n_macro = sum(1 for t in traces if t.kind == "macro")
    n_markets = sum(1 for t in traces if t.kind == "market")
    n_used = sum(1 for t in traces if t.used_in_decision)
    statement = (
        f"{STATEMENT} This run read {n_facts} filing figures, {n_macro} macro series and "
        f"{n_markets} market quotes; {n_used} of those inputs enter the formulas."
    )
    return DecisionProof(
        run_id=stored.run_id,
        company=stored.company,
        as_of=stored.as_of,
        verdict="proved" if all(c.passed for c in checks) else "not_proved",
        statement=statement,
        checks=checks,
        inputs=list(traces),
        llm_status=stored.brief.llm_status,
        decision_numbers_from_llm=bool(model_facts),
    )


# ---- published formulas (CLAUDE.md section 9) ----

def _rates(basis: MathBasis) -> tuple[float, float, float]:
    """Return (pi_hold, pi_hike, hike as a decimal). Missing markets fix the rate at hold."""
    if basis.rates_missing:
        return 1.0, 0.0, 0.0
    return 1.0 - basis.pi_hike, basis.pi_hike, basis.hike_bp / 10_000


def _net_interest(basis: MathBasis, d_r: float, note: float) -> float:
    return basis.ni0 + basis.variable * basis.debt * d_r / 4 + note * basis.coupon / 4


def _operating_income(basis: MathBasis, wti: float, kappa: float) -> float:
    return basis.oi0 - kappa * basis.gallons * (wti - basis.wti_ref) / _BARREL


def _coverage(oi: float, ni: float) -> float:
    if ni == 0:
        return math.inf if oi > 0 else 0.0
    return oi / ni


def _breakeven_wti(basis: MathBasis, theta: float, ni: float, kappa: float) -> float | None:
    if kappa == 0:
        return None
    value = basis.wti_ref + _BARREL * (basis.oi0 - theta * ni) / (kappa * basis.gallons)
    return value if math.isfinite(value) else None


def _wtis(basis: MathBasis) -> dict[str, float]:
    if basis.fuel_points is None:
        return {label: basis.wti_ref for label, _, _ in _WEIGHTS}
    points = list(basis.fuel_points)
    return {label: round(_quantile(points, target), 2) for label, _, target in _WEIGHTS}


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


def _expected_p_breach(basis: MathBasis, theta: float, kappa: float, ni_hold: float, ni_hike: float,
                       be_hold: float | None, be_hike: float | None) -> float | None:
    pi_hold, pi_hike, _ = _rates(basis)
    if basis.fuel_points is None:
        return None
    if kappa == 0:
        hold = 0.0 if basis.oi0 > theta * ni_hold else 1.0
        hike = 0.0 if basis.oi0 > theta * ni_hike else 1.0
        return pi_hold * hold + pi_hike * hike
    points = list(basis.fuel_points)
    w_hold = basis.wti_ref if be_hold is None else be_hold
    w_hike = basis.wti_ref if be_hike is None else be_hike
    return pi_hold * _survival(points, w_hold) + pi_hike * _survival(points, w_hike)


# ---- checks ----

def _coupon(stored: RunResult, basis: MathBasis) -> ProofCheck:
    spread = stored.inputs.coupon_spread_bp
    expected = (basis.dgs10 + spread / 100) / 100
    ok = _near(basis.coupon, expected, 1e-9)
    detail = (
        f"Coupon {basis.coupon * 100:.3f}% = (DGS10 {basis.dgs10:.3f}% + {spread:g} bp / 100) / 100."
        if ok else
        f"Coupon {basis.coupon * 100:.3f}% does not equal (DGS10 {basis.dgs10:.3f}% + {spread:g} bp / 100) / 100."
    )
    return ProofCheck(code="coupon", title="Coupon from the Treasury yield", passed=ok, detail=detail)


def _scenario_rows(stored: RunResult, basis: MathBasis) -> ProofCheck:
    expected = _expected_rows(stored, basis)
    if len(stored.rows) != len(expected):
        return ProofCheck(
            code="scenario_rows", title="Quarterly formulas match every scenario row", passed=False,
            detail=f"The run has {len(stored.rows)} scenario rows; the formulas produce {len(expected)}.",
        )
    for got, want in zip(stored.rows, expected):
        mismatch = _row_mismatch(got, want, wti=_MONEY, money=_MONEY, cover=_COVER, prob=_PROB)
        if mismatch:
            return ProofCheck(
                code="scenario_rows", title="Quarterly formulas match every scenario row", passed=False,
                detail=f"{got.fuel_label} / {got.rate_label}: {mismatch}",
            )
    return ProofCheck(
        code="scenario_rows", title="Quarterly formulas match every scenario row", passed=True,
        detail=_derivation(stored, basis),
    )


def _expected_rows(stored: RunResult, basis: MathBasis) -> list[ScenarioRow]:
    req = stored.inputs
    pi_hold, pi_hike, hike_decimal = _rates(basis)
    wtis = _wtis(basis)
    fuel_missing = basis.fuel_points is None
    rows = []
    for rate_label, pi, d_r in (("hold", pi_hold, 0.0), ("hike", pi_hike, hike_decimal)):
        ni = _net_interest(basis, d_r, req.note_size_usd_m)
        for label, weight, _ in _WEIGHTS:
            wti = wtis[label]
            oi = _operating_income(basis, wti, req.fuel_passthrough)
            coverage = _coverage(oi, ni)
            probability = None if fuel_missing or basis.rates_missing else weight * pi
            rows.append(ScenarioRow(
                fuel_label=label, rate_label=rate_label, wti=wti, probability=probability,
                operating_income_usd_m=oi, net_interest_usd_m=ni, coverage=coverage,
                breach=oi <= 0 or coverage < req.coverage_threshold,
            ))
    return rows


def _derivation(stored: RunResult, basis: MathBasis) -> str:
    req = stored.inputs
    wti = _wtis(basis)["P50"]
    oi = _operating_income(basis, wti, req.fuel_passthrough)
    ni = _net_interest(basis, 0.0, req.note_size_usd_m)
    cover = _coverage(oi, ni)
    return (
        f"Hold at the median fuel state: operating income {oi:,.2f} = {basis.oi0:,.2f} "
        f"- {req.fuel_passthrough:.2f} * {basis.gallons:,.2f} * ({wti:.2f} - {basis.wti_ref:.2f}) / 42; "
        f"net interest {ni:,.2f} = {basis.ni0:,.2f} + {basis.variable:.2f} * {basis.debt:,.2f} * 0 / 4 "
        f"+ {req.note_size_usd_m:,.2f} * {basis.coupon:.4f} / 4; coverage {cover:.2f}. "
        "All six rows satisfy the same formulas."
    )


def _breakeven(stored: RunResult, basis: MathBasis) -> ProofCheck:
    req = stored.inputs
    _, _, hike_decimal = _rates(basis)
    ni_hold = _net_interest(basis, 0.0, req.note_size_usd_m)
    ni_hike = _net_interest(basis, hike_decimal, req.note_size_usd_m)
    hold = _breakeven_wti(basis, req.coverage_threshold, ni_hold, req.fuel_passthrough)
    hike = _breakeven_wti(basis, req.coverage_threshold, ni_hike, req.fuel_passthrough)
    ok = _near(stored.summary.breakeven_wti_hold, hold, _MONEY) and _near(
        stored.summary.breakeven_wti_hike, hike, _MONEY)
    if ok and hold is not None:
        detail = (
            f"Breakeven WTI {hold:.2f} (hold) and {hike:.2f} (hike) = baseline {basis.wti_ref:.2f} "
            f"+ 42 * (operating income - threshold * net interest) / (pass-through * gallons)."
        )
    elif ok:
        detail = "Fuel pass-through is zero, so there is no breakeven oil price, and the run records none."
    else:
        detail = (
            f"Stored breakeven {stored.summary.breakeven_wti_hold} / {stored.summary.breakeven_wti_hike} "
            f"differs from the formula {hold} / {hike}."
        )
    return ProofCheck(code="breakeven", title="Breakeven oil price", passed=ok, detail=detail)


def _liquidity(stored: RunResult, basis: MathBasis) -> ProofCheck:
    if basis.liquidity is None or basis.debt_current is None or basis.debt_current == 0:
        cover, ok_flag = None, None
    else:
        cover = basis.liquidity / basis.debt_current
        ok_flag = cover >= 1.0
    summary = stored.summary
    ok = _near(summary.liquidity_cover, cover, _MONEY) and summary.liquidity_ok == ok_flag
    if ok and cover is not None:
        detail = (
            f"Liquidity cover {cover:.2f} = liquidity {basis.liquidity:,.2f} / debt due within a year "
            f"{basis.debt_current:,.2f}. It clears 1.0: {ok_flag}."
        )
    elif ok:
        detail = "Liquidity or debt due within a year is missing, and the run leaves the cover blank."
    else:
        detail = "Stored liquidity cover does not equal liquidity divided by debt due within a year."
    return ProofCheck(code="liquidity", title="Liquidity cover", passed=ok, detail=detail)


def _breach_probability(stored: RunResult, basis: MathBasis) -> ProofCheck:
    req = stored.inputs
    _, _, hike_decimal = _rates(basis)
    ni_hold = _net_interest(basis, 0.0, req.note_size_usd_m)
    ni_hike = _net_interest(basis, hike_decimal, req.note_size_usd_m)
    be_hold = _breakeven_wti(basis, req.coverage_threshold, ni_hold, req.fuel_passthrough)
    be_hike = _breakeven_wti(basis, req.coverage_threshold, ni_hike, req.fuel_passthrough)
    expected = _expected_p_breach(basis, req.coverage_threshold, req.fuel_passthrough, ni_hold, ni_hike, be_hold, be_hike)
    ok = _near(stored.summary.p_breach, expected, _PROB)
    if ok and expected is None:
        detail = "No trusted WTI ladder was visible, so the breach probability is blank in the run and in the formula."
    elif ok:
        pi_hold, pi_hike, _ = _rates(basis)
        detail = (
            f"Breach probability {expected:.3f} = {pi_hold:.3f} * S(breakeven hold) + "
            f"{pi_hike:.3f} * S(breakeven hike), with S the WTI survival curve."
        )
    else:
        detail = f"Stored breach probability {stored.summary.p_breach} differs from the formula {expected}."
    return ProofCheck(code="breach_probability", title="Breach probability from the survival curve", passed=ok, detail=detail)


def _probability_mass(stored: RunResult, basis: MathBasis) -> ProofCheck:
    probs = [row.probability for row in stored.rows]
    title = "Scenario probabilities"
    if any(p is None for p in probs):
        flags = set(stored.flags)
        named = "FUEL_NO_MARKET_INPUT" in flags or "RATES_NO_MARKET_INPUT" in flags
        ok = all(p is None for p in probs) and named and (basis.fuel_points is None or basis.rates_missing)
        detail = (
            "Probabilities are omitted because a required market input is missing, and the run says so."
            if ok else
            "Some scenario probabilities are missing without a fuel or rates flag."
        )
        return ProofCheck(code="probability_mass", title=title, passed=ok, detail=detail)
    total = sum(p for p in probs if p is not None)
    inside = all(0 <= p <= 1 for p in probs if p is not None)
    ok = inside and _near(total, 1.0, _PROB)
    detail = (
        f"The six presentation probabilities sum to {total:.3f}. Weights are 0.3, 0.4 and 0.3 "
        "times the hold and hike probabilities."
        if ok else
        f"The scenario probabilities sum to {total:.3f}, not 1."
    )
    return ProofCheck(code="probability_mass", title=title, passed=ok, detail=detail)


def _summary_grid(stored: RunResult) -> ProofCheck:
    hold = {row.fuel_label: row for row in stored.rows if row.rate_label == "hold"}
    title = "Summary matches the grid"
    if "P50" not in hold or "P90" not in hold:
        return ProofCheck(code="summary_grid", title=title, passed=False,
                          detail="The hold grid is missing its P50 or P90 row.")
    ok = _near(stored.summary.coverage_p50, hold["P50"].coverage, 1e-6) and _near(
        stored.summary.coverage_p90, hold["P90"].coverage, 1e-6)
    detail = (
        "The summary's hold coverage at P50 and P90 is the coverage on those grid rows."
        if ok else
        "The summary coverage does not match the hold P50 and P90 rows."
    )
    return ProofCheck(code="summary_grid", title=title, passed=ok, detail=detail)


def _replay(stored: RunResult, fresh: RunResult) -> ProofCheck:
    problems = _replay_problems(stored, fresh)
    return ProofCheck(
        code="engine_replay",
        title="Fresh engine run",
        passed=not problems,
        detail=(
            "A second engine run from the inputs visible at this as-of time reproduces the stored "
            "rows, summary, peers and flags."
            if not problems else
            "A second engine run does not reproduce the stored answer: " + "; ".join(problems) + "."
        ),
    )


def _fingerprint(stored: RunResult, fresh: RunResult, replay_ok: bool) -> ProofCheck:
    same = stored.data_version == fresh.data_version
    if same:
        detail = "The input fingerprint matches the one stored with the run."
        ok = True
    elif replay_ok:
        detail = (
            "Newer rows are visible at this as-of time. A fresh run still produces the stored decision numbers."
        )
        ok = True
    else:
        detail = (
            "The inputs visible at this as-of time differ from the bundle recorded on the run, "
            "and the decision numbers differ with them."
        )
        ok = False
    return ProofCheck(code="input_fingerprint", title="Input fingerprint", passed=ok, detail=detail)


def _no_lookahead(traces: Sequence[InputTrace]) -> ProofCheck:
    hidden = [t.name for t in traces if t.used_in_decision and not t.as_of_visible]
    ok = not hidden
    detail = (
        "Every filing, yield and quote used in the formulas was visible at the as-of time."
        if ok else
        "These formula inputs were not visible at the as-of time: " + ", ".join(hidden) + "."
    )
    return ProofCheck(code="no_lookahead", title="No look-ahead", passed=ok, detail=detail)


def _validated_facts(traces: Sequence[InputTrace]) -> ProofCheck:
    bad = [t.name for t in traces if t.kind == "fact" and t.used_in_decision and t.validated is not True]
    ok = not bad
    detail = (
        "Every filing figure in the formulas is marked validated."
        if ok else
        "These formula inputs are not validated: " + ", ".join(bad) + "."
    )
    return ProofCheck(code="validated_facts", title="Facts are validated", passed=ok, detail=detail)


def _llm_boundary(traces: Sequence[InputTrace]) -> ProofCheck:
    model_facts = _model_facts(traces)
    unvalidated = [t.name for t in model_facts if t.validated is not True]
    title = "Language model did not write the decision"
    if unvalidated:
        detail = (
            "These formula inputs were extracted by a language model and are not validated: "
            + ", ".join(unvalidated) + "."
        )
        return ProofCheck(code="llm_boundary", title=title, passed=False, detail=detail)
    if model_facts:
        names = ", ".join(t.name for t in model_facts)
        detail = (
            f"A language model extracted {names}. Each of those figures is marked validated. "
            "Market labels are classifications, and the brief is prose checked against the inputs."
        )
        return ProofCheck(code="llm_boundary", title=title, passed=True, detail=detail)
    detail = (
        "Every number in the formulas comes from a filing figure, FRED or a market quote. "
        "Market labels are classifications. The brief is prose."
    )
    return ProofCheck(code="llm_boundary", title=title, passed=True, detail=detail)


def _model_facts(traces: Sequence[InputTrace]) -> list[InputTrace]:
    return [t for t in traces if t.kind == "fact" and t.used_in_decision and t.extraction == "llm"]


def _brief_checks(stored: RunResult, facts: Sequence[Fact]) -> list[ProofCheck]:
    template = stored.brief.template_md
    template_ok = bool(template.strip()) and check_numbers(template, stored, facts)
    checks = [ProofCheck(
        code="template_numbers",
        title="Template brief numbers",
        passed=template_ok,
        detail=(
            "Every number in the template brief appears in the run or in a sourced fact."
            if template_ok else
            "The template brief is missing or contains a number that is not in the run."
        ),
    )]
    status = stored.brief.llm_status
    md = stored.brief.llm_md
    if status == "ready":
        ok = bool(md) and check_numbers(md or "", stored, facts)
        detail = (
            "The written brief passed the number check, so it may stand in for the template."
            if ok else
            "The written brief is marked ready and contains a number that is not in the inputs."
        )
    elif status == "rejected":
        ok = md is None
        detail = (
            "The written brief failed the number check. The template is the brief that is shown."
            if ok else
            "A rejected brief is still stored as the displayed prose."
        )
    elif status == "pending":
        ok = True
        detail = (
            "The written brief is still being checked. The decision numbers are already final. "
            "The brief replaces the template only when every number in it is in the inputs."
        )
    else:
        ok = md is None
        detail = (
            f"No written brief is attached (status {status}). The template, built from the run, is the brief."
            if ok else
            f"Status is {status} and a written brief is still attached."
        )
    checks.append(ProofCheck(code="llm_brief", title="Written brief numbers", passed=ok, detail=detail))
    return checks


def _balance_sheet(facts: Sequence[Fact]) -> ProofCheck:
    book: dict[Metric, float] = {}
    for fact in facts:
        if fact.metric not in book or fact.basis.value == "GAAP":
            book[fact.metric] = fact.value
    needed = (Metric.TOTAL_ASSETS, Metric.TOTAL_LIABILITIES, Metric.EQUITY)
    title = "Balance-sheet identity"
    if any(metric not in book for metric in needed):
        return ProofCheck(
            code="balance_sheet", title=title, passed=True,
            detail="Balance-sheet identity was not checked: assets, liabilities or equity is not among the facts visible at this time.",
        )
    assets, liabilities, equity = (book[metric] for metric in needed)
    scale = max(abs(assets), 1.0)
    ok = abs(assets - (liabilities + equity)) / scale <= _SHEET
    detail = (
        f"Assets {assets:,.0f} equal liabilities {liabilities:,.0f} plus equity {equity:,.0f} within 0.5%."
        if ok else
        f"Assets {assets:,.0f} do not equal liabilities {liabilities:,.0f} plus equity {equity:,.0f}."
    )
    return ProofCheck(code="balance_sheet", title=title, passed=ok, detail=detail)


# ---- comparisons ----

def _replay_problems(stored: RunResult, fresh: RunResult) -> list[str]:
    problems = []
    if stored.company != fresh.company or stored.as_of != fresh.as_of:
        problems.append("company or as-of time differs")
    if set(stored.flags) != set(fresh.flags):
        problems.append("flags differ")
    if not _summary_near(stored.summary, fresh.summary, 1e-6):
        problems.append("summary differs")
    if not _near(stored.operating_margin, fresh.operating_margin, 1e-6):
        problems.append("operating margin differs")
    if len(stored.rows) != len(fresh.rows) or any(
        _row_mismatch(a, b, wti=1e-6, money=1e-6, cover=1e-6, prob=1e-6) for a, b in zip(stored.rows, fresh.rows)
    ):
        problems.append("scenario rows differ")
    if not _peers_near(stored.peers, fresh.peers):
        problems.append("peers differ")
    return problems


def _peers_near(stored, fresh) -> bool:
    if len(stored) != len(fresh):
        return False
    pairs = zip(sorted(stored, key=lambda p: p.ticker), sorted(fresh, key=lambda p: p.ticker))
    for left, right in pairs:
        if left.ticker != right.ticker or left.period_end != right.period_end:
            return False
        if set(left.flags) != set(right.flags):
            return False
        if not _summary_near(left.summary, right.summary, 1e-6):
            return False
        if not _near(left.operating_margin, right.operating_margin, 1e-6):
            return False
    return True


def _summary_near(left: ScenarioSummary, right: ScenarioSummary, tol: float) -> bool:
    return (
        _near(left.p_breach, right.p_breach, tol)
        and _near(left.breakeven_wti_hold, right.breakeven_wti_hold, tol)
        and _near(left.breakeven_wti_hike, right.breakeven_wti_hike, tol)
        and _near(left.coverage_p50, right.coverage_p50, tol)
        and _near(left.coverage_p90, right.coverage_p90, tol)
        and _near(left.liquidity_cover, right.liquidity_cover, tol)
        and left.liquidity_ok == right.liquidity_ok
    )


def _row_mismatch(got: ScenarioRow, want: ScenarioRow, *, wti: float, money: float, cover: float, prob: float) -> str:
    if (got.fuel_label, got.rate_label) != (want.fuel_label, want.rate_label):
        return f"expected {want.fuel_label} / {want.rate_label}"
    checks = (
        ("WTI", got.wti, want.wti, wti),
        ("operating income", got.operating_income_usd_m, want.operating_income_usd_m, money),
        ("net interest", got.net_interest_usd_m, want.net_interest_usd_m, money),
        ("coverage", got.coverage, want.coverage, cover),
        ("probability", got.probability, want.probability, prob),
    )
    for name, left, right, tol in checks:
        if not _near(left, right, tol):
            return f"stored {name} {left} differs from the formula {right}"
    if got.breach != want.breach:
        return f"stored breach {got.breach} differs from the formula {want.breach}"
    return ""


def _near(got, expected, tol: float) -> bool:
    if got is None or expected is None:
        return got is expected
    if isinstance(got, bool) or isinstance(expected, bool):
        return got == expected
    left, right = float(got), float(expected)
    if math.isnan(left) or math.isnan(right):
        return False
    if math.isinf(left) or math.isinf(right):
        return left == right
    return abs(left - right) <= tol
