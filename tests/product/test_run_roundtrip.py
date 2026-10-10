"""A stored run must load back unchanged, or GET /runs/{id} fails with a 500.

Edge case from the LLD test map: fuel_passthrough (kappa) = 0. The breakeven formula
W* = W_bar + 42 * (OI0 - theta * NI_r) / (kappa * G) then divides by zero, so there is
no breakeven WTI: fuel price cannot move coverage.

Numbers are the illustrative worked example from CLAUDE.md section 9, not live data.
"""

import math
from datetime import date, datetime, timezone

import pytest

types = pytest.importorskip("airscen.types")  # arrives on this branch with the CP0 merge

AS_OF = datetime(2026, 10, 9, 14, 30, tzinfo=timezone.utc)
NO_BREAKEVEN = [float("inf"), float("-inf"), float("nan")]  # x/0 > 0, x/0 < 0, 0/0


def summary(breakeven: float) -> "types.ScenarioSummary":
    return types.ScenarioSummary(
        p_breach=0.297, breakeven_wti_hold=breakeven, breakeven_wti_hike=breakeven,
        coverage_p50=7.49, coverage_p90=3.47, liquidity_cover=2.08, liquidity_ok=True,
    )


def run(s: "types.ScenarioSummary") -> "types.RunResult":
    req = types.RunRequest(
        ticker="DAL", as_of=AS_OF, note_size_usd_m=1000, tenor_years=7,
        coupon_spread_bp=150, coverage_threshold=6.0, fuel_passthrough=0.0,
    )
    return types.RunResult(
        run_id="run_test", company="DAL", as_of=AS_OF, created_at=AS_OF, inputs=req,
        rows=[], summary=s,
        peers=[types.PeerResult(ticker="UAL", period_end=date(2026, 6, 30), summary=s, flags=[])],
        flags=["CRUDE_ONLY"], data_version="test",
        brief=types.Brief(template_md="", llm_md=None, llm_status="skipped"),
    )


@pytest.mark.parametrize("breakeven", NO_BREAKEVEN, ids=["inf", "-inf", "nan"])
def test_run_without_breakeven_survives_json_roundtrip(breakeven):
    original = run(summary(breakeven))
    reloaded = types.RunResult.model_validate_json(original.model_dump_json())
    assert reloaded == original


@pytest.mark.parametrize("breakeven", NO_BREAKEVEN, ids=["inf", "-inf", "nan"])
def test_no_breakeven_is_null_not_a_non_finite_number(breakeven):
    # The template brief and check_numbers must never see "inf" or "nan".
    s = summary(breakeven)
    assert s.breakeven_wti_hold is None and s.breakeven_wti_hike is None


def test_finite_breakeven_is_kept_exactly():
    s = summary(103.16)
    reloaded = types.ScenarioSummary.model_validate_json(s.model_dump_json())
    assert reloaded.breakeven_wti_hold == 103.16
    assert math.isfinite(reloaded.breakeven_wti_hike)
