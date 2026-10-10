"""A3: replay reads cannot see information published after as_of."""

from datetime import date, datetime, timedelta, timezone

import pytest

from airscen.store import Repository
from airscen.types import (
    Alert, Basis, Brief, Extraction, Fact, MacroObs, MarketLabel, MarketMeta,
    MarketView, Metric, RunRequest, RunResult, ScenarioSummary, Snapshot, Venue,
)


JULY = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
OCT8 = datetime(2026, 10, 8, 23, tzinfo=timezone.utc)
OCT9 = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


@pytest.fixture
def repo(tmp_path):
    store = Repository(tmp_path / "app.db")
    store.migrate()
    return store


def fact(value, period_end, filed_at, extraction=Extraction.XBRL,
         metric=Metric.OPERATING_INCOME, validated=True):
    return Fact(company="DAL", metric=metric, value=value, unit="usd_m",
                basis=Basis.GAAP, period_end=period_end, filed_at=filed_at,
                source_url="https://example.test/filing", extraction=extraction,
                validated=validated)


def test_october_8_sees_june_facts_but_not_october_9_filing(repo):
    june = fact(1000, date(2026, 6, 30), JULY)
    september = fact(1454, date(2026, 9, 30), OCT9)
    repo.upsert_facts([june, september])
    found, flags = repo.latest_facts("DAL", OCT8)
    assert found == [june]
    assert flags == set()
    found, flags = repo.latest_facts("DAL", OCT9)
    assert found == [september]
    assert flags == set()
    assert repo.latest_facts("UAL", OCT9) == ([], set())


def test_fact_ties_priority_validation_and_conflict_flag(repo):
    period = date(2026, 9, 30)
    rows = [
        fact(1454, period, OCT9, Extraction.XBRL),
        fact(1465, period, OCT9, Extraction.PARSER),
        fact(2000, period, OCT9, Extraction.LLM, validated=False),
        fact(1454, period, OCT9, Extraction.OVERRIDE,
             metric=Metric.NET_INTEREST_EXPENSE),
    ]
    repo.upsert_facts(rows)
    selected, flags = repo.latest_facts("DAL", OCT9)
    assert [(f.metric, f.extraction, f.value) for f in selected] == [
        (Metric.NET_INTEREST_EXPENSE, Extraction.OVERRIDE, 1454),
        (Metric.OPERATING_INCOME, Extraction.XBRL, 1454),
    ]
    assert flags == {"FACT_CONFLICT"}


def test_newest_filing_wins_across_periods_and_xbrl_wins_same_period_conflict(repo):
    period = date(2026, 6, 30)
    repo.upsert_facts([
        fact(100, period, JULY, Extraction.XBRL),
        fact(110, period, JULY + timedelta(days=1), Extraction.PARSER),
    ])
    found, flags = repo.latest_facts("DAL", OCT8)
    assert found[0].value == 100
    assert flags == {"FACT_CONFLICT"}


def test_source_priority_breaks_same_timestamp_tie_without_false_conflict(repo):
    period = date(2026, 9, 30)
    repo.upsert_facts([
        fact(100, period, OCT9, Extraction.LLM),
        fact(100.2, period, OCT9, Extraction.PARSER),
        fact(100.1, period, OCT9, Extraction.OVERRIDE),
        fact(100.3, period, OCT9, Extraction.XBRL),
    ])
    found, flags = repo.latest_facts("DAL", OCT9)
    assert found[0].extraction == Extraction.XBRL
    assert flags == set()


def test_snapshot_visibility_staleness_and_series_bounds(repo):
    repo.upsert_market(MarketMeta(venue=Venue.KALSHI, market_id="m", title="Market"))
    early = Snapshot(venue=Venue.KALSHI, market_id="m", ts=OCT9, bid=0.4, ask=0.5)
    late = early.model_copy(update={"ts": OCT9 + timedelta(minutes=10), "bid": 0.6})
    repo.put_snapshots([early, late])
    assert repo.latest_snapshot(Venue.KALSHI, "m", OCT9 - timedelta(seconds=1)) == (None, False)
    assert repo.latest_snapshot(Venue.KALSHI, "m", OCT9 + timedelta(minutes=4)) == (early, False)
    assert repo.latest_snapshot(Venue.KALSHI, "m", OCT9 + timedelta(minutes=6)) == (early, True)
    assert repo.latest_snapshot(Venue.KALSHI, "m", late.ts) == (late, False)
    assert repo.snapshot_series("kalshi", "m", OCT9, late.ts, OCT9 + timedelta(minutes=5)) == [early]
    assert repo.snapshot_series("kalshi", "m", late.ts, late.ts, late.ts) == [late]


def test_macro_requires_observation_and_vintage_visible(repo):
    old = MacroObs(series_id="DGS10", obs_date=date(2026, 10, 7),
                   value=4.1, realtime_start=date(2026, 10, 7))
    revised = old.model_copy(update={"value": 4.2, "realtime_start": date(2026, 10, 9)})
    future_obs = MacroObs(series_id="DGS10", obs_date=date(2026, 10, 10),
                          value=4.3, realtime_start=date(2026, 10, 8))
    repo.put_macro([old, revised, future_obs])
    assert repo.macro_series("DGS10", date(2026, 10, 1), date(2026, 10, 10), OCT8) == [old]
    assert repo.macro_series("DGS10", date(2026, 10, 1), date(2026, 10, 10), OCT9) == [revised]
    assert repo.macro_series("DGS10", date(2026, 10, 8), date(2026, 10, 10), OCT9) == []


def test_markets_are_current_and_hand_label_wins(repo):
    meta = MarketMeta(venue=Venue.KALSHI, market_id="m", title="Market")
    hand = MarketLabel(relevant=True, factor="fuel", yes_moves_factor="up", label_source="hand")
    llm = MarketLabel(relevant=False, label_source="llm")
    repo.upsert_market(MarketView(meta=meta, label=hand))
    repo.upsert_market(MarketView(meta=meta, label=llm))
    assert repo.markets("fuel") == [MarketView(meta=meta, label=hand)]
    assert repo.markets("rates") == []


def test_cache_get_and_data_version_ignore_future_inputs(repo):
    baseline = repo.data_version(OCT8)
    repo.upsert_facts([fact(1454, date(2026, 9, 30), OCT9)])
    assert repo.data_version(OCT8) == baseline
    assert repo.data_version(OCT9) != baseline
    current = repo.data_version(OCT9)
    entry = {"cache_key": "k", "task": "brief", "response_json": '{"text":"ok"}',
             "created_at": OCT9.isoformat()}
    repo.cache_put(entry)
    assert repo.cache_get("k")["response_json"] == entry["response_json"]
    assert repo.cache_get("missing") is None
    assert repo.data_version(OCT9) == current  # cache entries are output, not input


def test_runs_and_alerts_are_history_not_as_of_filtered(repo):
    request = RunRequest(ticker="DAL", as_of=JULY, note_size_usd_m=1000,
                         tenor_years=7, coupon_spread_bp=150)
    run = RunResult(run_id="r1", company="DAL", as_of=JULY, created_at=OCT9,
                    inputs=request, rows=[], peers=[], flags=[], data_version="v1",
                    summary=ScenarioSummary(p_breach=None, breakeven_wti_hold=None,
                                            breakeven_wti_hike=None, coverage_p50=7,
                                            coverage_p90=5, liquidity_cover=None,
                                            liquidity_ok=None),
                    brief=Brief(template_md="Template", llm_md=None, llm_status="pending"))
    repo.save_run(run)
    alert = Alert(alert_id="a1", ts=OCT9, company="DAL", run_id="r1",
                  rule="DATA_STALE", dedup_key="DAL:stale", severity="low",
                  payload={"source": "fred"}, status="pending")
    repo.save_alert(alert)
    assert repo.get_run("r1") == run
    assert repo.alerts("DAL", JULY) == [alert]
