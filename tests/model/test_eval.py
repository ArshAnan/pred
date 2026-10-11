"""Evaluation report. The July 10 quotes are the recorded candlestick fixture."""

from datetime import datetime, timezone

from airscen.eval.ablation import ablation_lines
from airscen.eval.calibration import brier_score, reliability_lines
from airscen.eval.checks import balance_sheet, debt_identity, number_lines
from airscen.eval.faithfulness import faithfulness_lines
from airscen.eval.replay import replay_lines
from airscen.eval.__main__ import report
from airscen.types import Brief, RunRequest, RunResult, ScenarioRow, ScenarioSummary


def test_delta_identities_and_extract_comparison():
    assert debt_identity(3_324, 9_527, 12_851)
    assert balance_sheet(33_575, 29_960, 22_513, 86_048)
    assert not debt_identity(1, 1, 10)
    lines = number_lines(
        {"DAL": {"operating_income": 1454}},
        {"DAL": {"operating_income": 1454}, "UAL": {"operating_income": 1}},
    )
    text = "\n".join(lines)
    assert "DAL debt_identity: pass" in text
    assert "DAL balance_sheet: pass" in text
    assert "DAL operating_income: pass" in text
    assert "UAL operating_income: fail (missing override)" in text
    assert "AAL extract: fail" in text


def test_july10_replay_uses_backfill_and_states_the_realised_quarter():
    lines = replay_lines()
    text = "\n".join(lines)
    assert "BACKFILL_INPUTS" in text
    assert "2026-07-10T20:00:00Z" in text
    assert "3.61" in text
    assert "3.15" in text
    assert "one case study, not a statistical result" in text
    assert "coverage grid not computed" in text
    wti = next(line for line in lines if line.startswith("WTI P10"))
    parts = wti.split()
    p10 = float(parts[parts.index("P10") + 1])
    p50 = float(parts[parts.index("P50") + 1])
    p90 = float(parts[parts.index("P90") + 1])
    assert p10 < p50 < p90


def test_ablation_prints_both_errors():
    text = "\n".join(ablation_lines())
    assert "market error" in text
    assert "assumption error" in text
    assert "crack spread is unchanged" in text


def _result() -> RunResult:
    as_of = datetime(2026, 10, 9, 14, 30, tzinfo=timezone.utc)
    row = ScenarioRow(
        fuel_label="P50",
        wti=94.44,
        rate_label="hold",
        probability=0.4,
        operating_income_usd_m=1196.42,
        net_interest_usd_m=159.71,
        coverage=7.49,
        breach=False,
    )
    return RunResult(
        run_id="run_7f3a9c",
        company="DAL",
        as_of=as_of,
        created_at=as_of,
        inputs=RunRequest(
            ticker="DAL",
            as_of=as_of,
            note_size_usd_m=1000,
            tenor_years=7,
            coupon_spread_bp=150,
            coverage_threshold=6,
        ),
        rows=[row],
        summary=ScenarioSummary(
            p_breach=0.297,
            breakeven_wti_hold=103.16,
            breakeven_wti_hike=102.77,
            coverage_p50=7.49,
            coverage_p90=3.47,
            liquidity_cover=2.08,
            liquidity_ok=True,
        ),
        peers=[],
        flags=[],
        data_version="d41a7c0e",
        brief=Brief(template_md="", llm_md=None, llm_status="pending"),
    )


def test_faithfulness_reports_a_rate_and_a_sample_size():
    result = _result()
    pairs = [("Breach probability 0.297.", result), ("Invented figure 99999.", result)]
    text = "\n".join(faithfulness_lines(pairs))
    assert "sample size 2" in text
    assert "pass rate 0.50" in text
    assert "sample size 0" in "\n".join(faithfulness_lines([]))


def test_calibration_reports_brier_against_the_coin_flip():
    rows = [(1.0, 1, 100), (0.0, 0, 20_000)]
    assert brier_score(rows) == 0.0
    text = "\n".join(reliability_lines(rows))
    assert "0.25" in text
    assert "sample size 2" in text
    assert "low:" in text and "high:" in text
    assert "sample size 0" in "\n".join(reliability_lines([]))


def test_report_ends_with_the_limits():
    text = report()
    assert text.strip().endswith("The brief is judged on faithfulness to its inputs, not on foresight.")
    assert "One replay is a case study, not a statistical result." in text
    assert "No claim of predictive edge." in text
