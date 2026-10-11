"""C2: template brief and number check (LLD section 9 test map: `check_numbers` rejects an
invented number; the template contains every summary number). Inputs are the illustrative
worked example from conftest.py."""

import pytest


@pytest.fixture
def brief(types):
    return pytest.importorskip("airscen.brief")


# ---- template_brief ----

def test_template_contains_every_summary_number(brief, worked_example_run):
    md = brief.template_brief(worked_example_run)
    for text in ["29.7%", "103.16", "102.77", "7.49x", "3.47x", "2.08x"]:
        assert text in md, text


def test_template_contains_the_six_row_grid(brief, worked_example_run):
    md = brief.template_brief(worked_example_run)
    for row in worked_example_run.rows:
        assert f"{row.coverage:.2f}x" in md
        assert f"{row.wti:.2f}" in md
    assert md.count("| P10 |") == 2 and md.count("| P90 |") == 2


def test_template_states_inputs(brief, worked_example_run):
    md = brief.template_brief(worked_example_run)
    for text in ["$1,000M", "7-year", "150 bp", "6.00x", "1.00"]:
        assert text in md, text


def test_template_lists_flags_and_required_assumptions(brief, worked_example_run):
    md = brief.template_brief(worked_example_run)
    for text in ["VARIABLE_SHARE_STALE", "CRUDE_ONLY", "RESOLVE_DATE_PROXY", "independent", "tail"]:
        assert text in md, text


def test_template_says_it_is_conditional_not_a_forecast(brief, worked_example_run):
    md = brief.template_brief(worked_example_run).lower()
    assert "not a forecast" in md


def test_template_always_lists_crude_only_and_resolve_date_proxy(brief, make_run):
    md = brief.template_brief(make_run(flags=[]))
    assert "CRUDE_ONLY" in md and "RESOLVE_DATE_PROXY" in md


def test_template_shows_peers(brief, make_run, synthetic_peer):
    md = brief.template_brief(make_run(peers=[synthetic_peer]))
    assert "UAL" in md and "2026-06-30" in md and "PERIOD_MISMATCH" in md


def test_template_handles_missing_values(brief, types, make_run):
    rows = [r.model_copy(update={"probability": None}) for r in make_run().rows]
    run = make_run(
        rows=rows, fuel_passthrough=0.0,
        summary={"p_breach": None, "breakeven_wti_hold": None, "breakeven_wti_hike": None,
                 "liquidity_cover": None, "liquidity_ok": None},
        flags=["FUEL_NO_MARKET_INPUT"],
    )
    md = brief.template_brief(run)
    for bad in ["None", "nan", "inf"]:
        assert bad not in md, bad
    assert "not available" in md
    assert brief.check_numbers(md, run)


def test_template_is_deterministic(brief, worked_example_run):
    assert brief.template_brief(worked_example_run) == brief.template_brief(worked_example_run)


def test_template_passes_its_own_number_check(brief, make_run, synthetic_peer):
    run = make_run(peers=[synthetic_peer])
    assert brief.check_numbers(brief.template_brief(run), run)


# ---- check_numbers ----

def test_check_numbers_rejects_an_invented_number(brief, worked_example_run):
    md = brief.template_brief(worked_example_run) + "\nUnder a severe shock coverage falls to 4.12x.\n"
    assert not brief.check_numbers(md, worked_example_run)


@pytest.mark.parametrize("sentence", [
    "Breakeven WTI is 103.5 USD per barrel.",   # input is 103.16, which rounds to 103.2
    "There is a 35% chance of a breach.",       # input is 29.7%
    "Liquidity covers maturities 3.1 times.",   # input is 2.08
])
def test_check_numbers_rejects_wrong_values(brief, worked_example_run, sentence):
    assert not brief.check_numbers(sentence, worked_example_run)


@pytest.mark.parametrize("sentence", [
    "The breach probability is 0.297, about 29.7% or roughly 30%.",
    "Breakeven WTI is 103.16 (103.2, or about 103) under hold and 102.77 under a hike.",
    "Coverage is 9.96x at P10 and 3.47x at P90; liquidity cover is 2.08x.",
    "A $1B, 7-year note at a 150 bp spread; operating income of 1,590 at P10.",
    "The hold-state P10 scenario has probability 0.249 and the hike-state P50 scenario 6.8%.",
])
def test_check_numbers_accepts_rounded_forms_of_input_numbers(brief, worked_example_run, sentence):
    assert brief.check_numbers(sentence, worked_example_run)


def test_check_numbers_ignores_labels_ids_dates_and_list_markers(brief, worked_example_run):
    md = (
        "## DAL as of 2026-10-09T14:30:00Z (run run_7f3a9c)\n"
        "1. P10, P50 and P90 fuel states; flags VARIABLE_SHARE_STALE and CRUDE_ONLY.\n"
        "2. Data version d41a7c0e, as of 2026-10-09.\n"
    )
    assert brief.check_numbers(md, worked_example_run)


def test_check_numbers_ignores_a_hex_fingerprint_and_still_rejects_billions(brief, worked_example_run):
    assert brief.check_numbers("Data version `235b1c2d`.", worked_example_run)
    assert not brief.check_numbers("The shock costs 235b.", worked_example_run)


def test_check_numbers_rejects_a_date_not_in_the_input(brief, worked_example_run):
    assert not brief.check_numbers("As of 2026-11-15 the answer changes.", worked_example_run)
