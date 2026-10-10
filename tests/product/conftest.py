"""Lane C fixtures.

`worked_example_run` is the Delta worked example from LLD section 6 / CLAUDE.md section 9.
ILLUSTRATIVE: the WTI ladder, rate quotes, W_bar and coupon are assumptions, not live data.
Replace with lane B's tests/fixtures/worked_example.json once it lands (CP0_DECISIONS R5).
"""

from datetime import date, datetime, timezone

import pytest

AS_OF = datetime(2026, 10, 9, 14, 30, tzinfo=timezone.utc)

# (fuel_label, wti, rate_label, probability, operating_income, net_interest, coverage, breach)
# OI is OI0 - G * (w - W_bar) / 42 with OI0 1,454, G 1,146, W_bar 85; NI and coverage as in LLD section 6.
WORKED_EXAMPLE_ROWS = [
    ("P10", 80.00, "hold", 0.249, 1590.43, 159.75, 9.96, False),
    ("P50", 94.44, "hold", 0.332, 1196.42, 159.75, 7.49, False),
    ("P90", 118.00, "hold", 0.249, 553.57, 159.75, 3.47, True),
    ("P10", 80.00, "hike", 0.051, 1590.43, 161.52, 9.85, False),
    ("P50", 94.44, "hike", 0.068, 1196.42, 161.52, 7.41, False),
    ("P90", 118.00, "hike", 0.051, 553.57, 161.52, 3.43, True),
]


@pytest.fixture
def types():
    return pytest.importorskip("airscen.types")  # arrives on this branch with the CP0 merge


@pytest.fixture
def make_run(types):
    """Build a RunResult from the worked example; keyword args override summary fields or top-level fields."""

    def build(*, summary=None, rows=None, peers=None, flags=None, **inputs):
        req = types.RunRequest(**{
            "ticker": "DAL", "as_of": AS_OF, "note_size_usd_m": 1000, "tenor_years": 7,
            "coupon_spread_bp": 150, "coverage_threshold": 6.0, "fuel_passthrough": 1.0, **inputs,
        })
        s = types.ScenarioSummary(**{
            "p_breach": 0.297, "breakeven_wti_hold": 103.16, "breakeven_wti_hike": 102.77,
            "coverage_p50": 7.49, "coverage_p90": 3.47, "liquidity_cover": 2.08, "liquidity_ok": True,
            **(summary or {}),
        })
        if rows is None:
            rows = [
                types.ScenarioRow(
                    fuel_label=f, wti=w, rate_label=r, probability=p, operating_income_usd_m=oi,
                    net_interest_usd_m=ni, coverage=c, breach=b,
                )
                for f, w, r, p, oi, ni, c, b in WORKED_EXAMPLE_ROWS
            ]
        return types.RunResult(
            run_id="run_7f3a9c", company="DAL", as_of=AS_OF, created_at=AS_OF, inputs=req,
            rows=rows, summary=s, peers=peers or [],
            flags=flags if flags is not None else ["VARIABLE_SHARE_STALE", "CRUDE_ONLY", "RESOLVE_DATE_PROXY"],
            data_version="d41a7c0e",
            brief=types.Brief(template_md="", llm_md=None, llm_status="pending"),
        )

    return build


@pytest.fixture
def worked_example_run(make_run):
    return make_run()


@pytest.fixture
def synthetic_peer(types):
    """SYNTHETIC test values, not data: reuses Delta's summary to exercise the peer table."""
    s = types.ScenarioSummary(
        p_breach=0.297, breakeven_wti_hold=103.16, breakeven_wti_hike=102.77,
        coverage_p50=7.49, coverage_p90=3.47, liquidity_cover=2.08, liquidity_ok=True,
    )
    return types.PeerResult(ticker="UAL", period_end=date(2026, 6, 30), summary=s, flags=["PERIOD_MISMATCH"])
