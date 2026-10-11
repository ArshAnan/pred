"""C5: RunService.create_run end to end on the worked example (LLD sections 5 and 6).

Done when a fake repo seeded from the worked example returns the golden numbers end to end,
and a missing variable_debt_share gives 422. The store is the TEMPORARY stand-in in
fake_store.py; numbers are ILLUSTRATIVE (see fake_store.py and worked_example.json).
"""

from datetime import timedelta

import pytest

from airscen.config import Settings
from airscen.service import RunService
from airscen.types import DataError, RequestError, RunRequest, Snapshot
from fake_store import AS_OF, FakeStore, seed_worked_example


def request(**changes) -> RunRequest:
    return RunRequest(**{"ticker": "DAL", "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150,
                         "coverage_threshold": 6.0, "fuel_passthrough": 1.0, **changes})


@pytest.fixture
def store():
    s = FakeStore()
    seed_worked_example(s)
    return s


def service(store, settings=None, now=AS_OF) -> RunService:
    return RunService(repo=store, settings=settings or Settings(), clock=lambda: now)


# ---- golden ----

def test_worked_example_end_to_end_gives_the_golden_numbers(store):
    expected = seed_worked_example(FakeStore())["expected"]
    run = service(store).create_run(request())

    s, e = run.summary, expected["summary"]
    assert s.p_breach == pytest.approx(e["p_breach"], abs=0.0005)
    assert s.breakeven_wti_hold == pytest.approx(e["breakeven_wti_hold"], abs=0.005)
    assert s.breakeven_wti_hike == pytest.approx(e["breakeven_wti_hike"], abs=0.005)
    assert s.coverage_p50 == pytest.approx(e["coverage_p50"], abs=0.005)
    assert s.coverage_p90 == pytest.approx(e["coverage_p90"], abs=0.005)
    assert s.liquidity_cover == pytest.approx(e["liquidity_cover"], abs=0.005)
    assert s.liquidity_ok is True

    assert len(run.rows) == 6
    for got, want in zip(run.rows, expected["rows"]):
        assert (got.fuel_label, got.rate_label, got.breach) == (want["fuel_label"], want["rate_label"], want["breach"])
        assert got.wti == pytest.approx(want["wti"], abs=0.005)
        assert got.probability == pytest.approx(want["probability"], abs=0.0005)
        assert got.operating_income_usd_m == pytest.approx(want["operating_income_usd_m"], abs=0.01)
        assert got.net_interest_usd_m == pytest.approx(want["net_interest_usd_m"], abs=0.005)
        assert got.coverage == pytest.approx(want["coverage"], abs=0.005)

    assert set(run.flags) == set(expected["flags"])  # VARIABLE_SHARE_STALE, CRUDE_ONLY, RESOLVE_DATE_PROXY


def test_run_is_saved_with_template_brief_and_data_version(store):
    run = service(store).create_run(request())
    assert store.get_run(run.run_id) == run
    assert run.as_of == AS_OF and run.inputs.as_of == AS_OF and run.created_at == AS_OF
    assert run.data_version == store.data_version(AS_OF)
    assert run.brief.llm_status == "skipped" and run.brief.llm_md is None
    assert "103.16" in run.brief.template_md and "29.7%" in run.brief.template_md
    assert run.peers == []  # C10


def test_explicit_as_of_is_used_for_every_read(store):
    run = service(store, now=AS_OF + timedelta(days=3)).create_run(request(as_of=AS_OF))
    assert run.as_of == AS_OF and run.summary.p_breach == pytest.approx(0.297, abs=0.0005)


# ---- required inputs -> 422 ----

def test_missing_variable_debt_share_is_422_naming_it():
    store = FakeStore()
    seed_worked_example(store, skip_metrics=("variable_debt_share",))
    with pytest.raises(DataError) as exc:
        service(store).create_run(request())
    assert exc.value.code == "E_MISSING_FACT"
    assert exc.value.details["missing"] == ["variable_debt_share"]
    assert "variable_debt_share" in exc.value.message
    assert store.saved == {}


def test_all_missing_inputs_are_named_together():
    store = FakeStore()
    seed_worked_example(store, skip_metrics=("operating_income", "fuel_gallons_m"), with_macro=False)
    with pytest.raises(DataError) as exc:
        service(store).create_run(request())
    assert exc.value.details["missing"] == ["operating_income", "fuel_gallons_m", "DGS10", "DCOILWTICO"]


def test_facts_filed_after_as_of_are_invisible(store):
    # On October 8 the October 9 8-K is not visible: only the June-quarter variable share is.
    with pytest.raises(DataError) as exc:
        service(store).create_run(request(as_of=AS_OF - timedelta(days=1)))
    assert "operating_income" in exc.value.details["missing"]


def test_optional_liquidity_facts_may_be_missing():
    store = FakeStore()
    seed_worked_example(store, skip_metrics=("liquidity", "debt_current"))
    run = service(store).create_run(request())
    assert run.summary.liquidity_cover is None and run.summary.liquidity_ok is None


def test_unknown_ticker_is_404(store):
    with pytest.raises(RequestError) as exc:
        service(store).create_run(request(ticker="XYZ"))
    assert exc.value.code == "E_NOT_FOUND"


# ---- coupon and baseline WTI ----

def test_coupon_comes_from_dgs10_plus_spread(store):
    run = service(store).create_run(request(coupon_spread_bp=250))  # c = (4.00 + 2.50) / 100
    hold_ni = run.rows[0].net_interest_usd_m
    assert hold_ni == pytest.approx(146 + 1000 * 0.065 / 4, abs=1e-9)


def test_baseline_wti_override_when_fred_has_too_few_observations():
    store = FakeStore()
    seed_worked_example(store)
    oil = [o for o in store.macro if o.series_id == "DCOILWTICO"]
    store.macro = [o for o in store.macro if o.series_id != "DCOILWTICO"] + oil[:29]  # one short of 30
    with pytest.raises(DataError) as exc:
        service(store).create_run(request())
    assert exc.value.details["missing"] == ["DCOILWTICO"]

    run = service(store, settings=Settings(fuel={"wti_ref_override": 85})).create_run(request())
    assert "REF_WTI_OVERRIDE" in run.flags
    assert run.summary.breakeven_wti_hold == pytest.approx(103.16, abs=0.005)


# ---- markets ----

def test_stale_quotes_are_excluded_and_flagged(store):
    store.snapshots = [s.model_copy(update={"ts": AS_OF - timedelta(minutes=10)}) for s in store.snapshots]
    run = service(store).create_run(request())
    assert {"STALE_MARKETS", "FUEL_NO_MARKET_INPUT", "RATES_NO_MARKET_INPUT"} <= set(run.flags)
    assert run.summary.p_breach is None
    assert all(r.probability is None for r in run.rows)
    assert run.summary.breakeven_wti_hold == pytest.approx(103.16, abs=0.005)  # breakevens still shown


def test_markets_closed_before_as_of_are_ignored(store):
    for i, v in enumerate(store.market_views):
        if v.meta.group_key == "FED-ILLUSTRATIVE":
            store.market_views[i] = v.model_copy(update={"meta": v.meta.model_copy(
                update={"close_time": AS_OF - timedelta(days=1)})})
    run = service(store).create_run(request())
    assert "RATES_NO_MARKET_INPUT" in run.flags


def test_unlabeled_or_irrelevant_markets_are_not_inputs(store):
    store.market_views = [v.model_copy(update={"label": None}) for v in store.market_views]
    run = service(store).create_run(request())
    assert {"FUEL_NO_MARKET_INPUT", "RATES_NO_MARKET_INPUT"} <= set(run.flags)


def test_backfilled_quotes_set_backfill_inputs(store):
    store.snapshots = [s.model_copy(update={"src": "backfill_kalshi"}) for s in store.snapshots]
    run = service(store).create_run(request())
    assert "BACKFILL_INPUTS" in run.flags


def test_crude_only_and_resolve_date_proxy_are_always_flagged(store):
    store.market_views = []
    run = service(store).create_run(request())
    assert {"CRUDE_ONLY", "RESOLVE_DATE_PROXY"} <= set(run.flags)
    assert len(run.flags) == len(set(run.flags))


# ---- get_run ----

def test_get_run_returns_a_saved_run_or_404(store):
    svc = service(store)
    run = svc.create_run(request())
    assert svc.get_run(run.run_id) == run
    with pytest.raises(RequestError) as exc:
        svc.get_run("run_missing")
    assert exc.value.code == "E_NOT_FOUND"
