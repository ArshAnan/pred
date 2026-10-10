"""Lock the CP0 golden fixtures. Standard library only; does not import airscen."""

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def test_worked_example_matches_lld_section_6():
    doc = _load("worked_example.json")
    assert doc["illustrative"] is True
    assert doc["company"] == "DAL"
    assert doc["as_of"] == "2026-10-09T14:30:00Z"

    facts = doc["inputs"]["facts"]
    assert facts["operating_income_usd_m"] == 1454
    assert facts["net_interest_expense_usd_m"] == 146
    assert facts["fuel_gallons_m"] == 1146
    assert facts["debt_total_usd_m"] == 12851
    assert facts["variable_debt_share"] == 0.22
    assert facts["liquidity_usd_m"] == 6900
    assert facts["debt_current_usd_m"] == 3324
    assert doc["inputs"]["note_size_usd_m"] == 1000
    assert doc["inputs"]["tenor_years"] == 7
    assert doc["inputs"]["coupon"] == 0.055
    assert doc["inputs"]["coverage_threshold"] == 6.0
    assert doc["inputs"]["fuel_passthrough"] == 1.0
    assert doc["inputs"]["wti_ref"] == 85
    assert doc["inputs"]["rate_quotes"] == {"hold": 0.82, "hike": 0.17, "cut": 0.01}

    expected = doc["expected"]
    assert expected["pi_hike"] == 0.17
    assert expected["pi_hold"] == 0.83
    assert expected["net_interest_hold"] == 159.75
    assert expected["net_interest_hike"] == 161.52
    assert (expected["wti_p10"], expected["wti_p50"], expected["wti_p90"]) == (80.0, 94.44, 118.0)
    assert expected["summary"] == {
        "p_breach": 0.297,
        "breakeven_wti_hold": 103.16,
        "breakeven_wti_hike": 102.77,
        "coverage_p50": 7.49,
        "coverage_p90": 3.47,
        "liquidity_cover": 2.08,
        "liquidity_ok": True,
    }
    assert expected["flags"] == ["VARIABLE_SHARE_STALE", "CRUDE_ONLY", "RESOLVE_DATE_PROXY"]

    rows = expected["rows"]
    assert len(rows) == 6
    assert [(row["fuel_label"], row["rate_label"]) for row in rows] == [
        ("P10", "hold"),
        ("P50", "hold"),
        ("P90", "hold"),
        ("P10", "hike"),
        ("P50", "hike"),
        ("P90", "hike"),
    ]
    assert [row["probability"] for row in rows] == [0.249, 0.332, 0.249, 0.051, 0.068, 0.051]
    assert [row["coverage"] for row in rows] == [9.96, 7.49, 3.47, 9.85, 7.41, 3.43]
    assert [row["operating_income_usd_m"] for row in rows] == [
        1590.43,
        1196.42,
        553.57,
        1590.43,
        1196.42,
        553.57,
    ]
    assert [row["breach"] for row in rows] == [False, False, True, False, False, True]
    assert abs(sum(row["probability"] for row in rows) - 1.0) < 1e-9


def test_ladder_quantiles_and_tails():
    ladder = _load("ladder.json")
    assert ladder["illustrative"] is True
    assert ladder["median_spacing"] == 10
    assert ladder["thresholds"] == [
        {"k": 80, "p": 0.9},
        {"k": 90, "p": 0.62},
        {"k": 100, "p": 0.35},
        {"k": 110, "p": 0.18},
        {"k": 120, "p": 0.08},
    ]
    assert ladder["tails"] == [{"k": 70, "p": 1.0}, {"k": 130, "p": 0.0}]
    assert ladder["expected"] == {"p10": 80.0, "p50": 94.44, "p90": 118.0}

    worked = _load("worked_example.json")
    assert worked["inputs"]["ladder"]["thresholds"] == ladder["thresholds"]
    assert worked["inputs"]["ladder"]["tails"] == ladder["tails"]
    assert (
        worked["expected"]["wti_p10"],
        worked["expected"]["wti_p50"],
        worked["expected"]["wti_p90"],
    ) == (ladder["expected"]["p10"], ladder["expected"]["p50"], ladder["expected"]["p90"])
