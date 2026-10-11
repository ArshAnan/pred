"""A2–A3: schema, round-trips, and as_of visibility."""

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from airscen.store import Repository
from airscen.types import (
    Alert,
    Basis,
    Brief,
    Extraction,
    Factor,
    Fact,
    MarketKind,
    MarketLabel,
    MarketMeta,
    Metric,
    MacroObs,
    RunRequest,
    RunResult,
    ScenarioRow,
    ScenarioSummary,
    Snapshot,
    StoreError,
    Venue,
)

AS_OF_OCT8 = datetime(2026, 10, 8, tzinfo=timezone.utc)
JUNE_FILED = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
SEPT_FILED = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def _fact(metric, value, filed_at, period_end, extraction=Extraction.OVERRIDE, validated=True, basis=Basis.GAAP):
    return Fact(
        company="DAL",
        metric=metric,
        value=value,
        unit="usd_m",
        basis=basis,
        period_end=period_end,
        filed_at=filed_at,
        source_url="https://example.test/filing",
        extraction=extraction,
        validated=validated,
    )


def _meta(market_id="MKT", kind=MarketKind.OTHER):
    return MarketMeta(
        venue=Venue.KALSHI,
        market_id=market_id,
        title="Will WTI close above 90?",
        rules="resolves on the close",
        close_time=datetime(2026, 10, 31, tzinfo=timezone.utc),
        kind=kind,
        threshold=90,
        threshold_unit="usd_per_bbl",
        group_key="2026-10-31",
    )


def _snap(ts, market_id="MKT", bid=0.4, ask=0.5):
    return Snapshot(
        venue=Venue.KALSHI,
        market_id=market_id,
        ts=ts,
        bid=bid,
        ask=ask,
        bid_size=10,
        ask_size=12,
        last=0.45,
        volume_24h=100,
        src="live",
    )


def _run(run_id="run_1"):
    as_of = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return RunResult(
        run_id=run_id,
        company="DAL",
        as_of=as_of,
        created_at=as_of,
        inputs=RunRequest(ticker="DAL", note_size_usd_m=1000, tenor_years=7, coupon_spread_bp=150),
        rows=[
            ScenarioRow(
                fuel_label="P50",
                rate_label="hold",
                wti=90,
                probability=1.0,
                operating_income_usd_m=1454,
                net_interest_usd_m=146,
                coverage=9.96,
                breach=False,
            )
        ],
        summary=ScenarioSummary(
            p_breach=0.2,
            breakeven_wti_hold=103.16,
            breakeven_wti_hike=102.77,
            coverage_p50=7.49,
            coverage_p90=3.47,
            liquidity_cover=2.08,
            liquidity_ok=True,
        ),
        peers=[],
        flags=["CRUDE_ONLY"],
        data_version="abc12345",
        brief=Brief(template_md="template", llm_md=None, llm_status="pending"),
    )


def test_migrate_creates_seven_tables_and_is_repeatable(db_path):
    repo = Repository(db_path)
    repo.migrate()
    repo.migrate()
    names = {row[0] for row in repo._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    journal = repo._conn.execute("PRAGMA journal_mode").fetchone()[0]
    foreign_keys = repo._conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert set(("facts", "markets", "snapshots", "macro", "runs", "alerts", "llm_cache")).issubset(names)
    assert journal.lower() == "wal"
    assert foreign_keys == 1
    assert repo.ping()


def test_october_8_hides_the_october_9_filing(db_path):
    repo = Repository(db_path)
    repo.migrate()
    june = _fact(Metric.OPERATING_INCOME, 1000, JUNE_FILED, date(2026, 6, 30))
    sept = _fact(Metric.OPERATING_INCOME, 1454, SEPT_FILED, date(2026, 9, 30))
    repo.upsert_facts([june, sept])
    facts, flags = repo.latest_facts("DAL", AS_OF_OCT8)
    assert flags == []
    assert len(facts) == 1
    assert facts[0].value == 1000
    assert facts[0].period_end == date(2026, 6, 30)
    later, _ = repo.latest_facts("DAL", datetime(2026, 10, 9, 13, tzinfo=timezone.utc))
    assert later[0].value == 1454


def test_unvalidated_rows_and_source_priority(db_path):
    repo = Repository(db_path)
    repo.migrate()
    filed = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    period = date(2026, 9, 30)
    repo.upsert_facts([
        _fact(Metric.NET_INTEREST_EXPENSE, 999, filed, period, Extraction.LLM, validated=False),
        _fact(Metric.NET_INTEREST_EXPENSE, 146.2, filed, period, Extraction.PARSER),
        _fact(Metric.NET_INTEREST_EXPENSE, 146, filed, period, Extraction.XBRL),
    ])
    facts, flags = repo.latest_facts("DAL", datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert flags == []
    assert facts[0].value == 146
    assert facts[0].extraction == Extraction.XBRL


def test_fact_conflict_prefers_xbrl(db_path):
    repo = Repository(db_path)
    repo.migrate()
    filed = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    period = date(2026, 9, 30)
    repo.upsert_facts([
        _fact(Metric.DEBT_TOTAL, 12000, filed, period, Extraction.OVERRIDE),
        _fact(Metric.DEBT_TOTAL, 12851, filed, period, Extraction.XBRL),
    ])
    facts, flags = repo.latest_facts("DAL", datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert flags == ["FACT_CONFLICT"]
    assert facts[0].value == 12851
    assert facts[0].extraction == Extraction.XBRL


def test_write_retries_once_when_sqlite_is_busy(db_path, monkeypatch):
    repo = Repository(db_path)
    repo.migrate()
    original = repo._execute
    seen = {"inserts": 0}

    def flaky(sql, *args):
        if sql.lstrip().upper().startswith("INSERT") and seen["inserts"] == 0:
            seen["inserts"] += 1
            raise sqlite3.OperationalError("database is locked")
        return original(sql, *args)

    monkeypatch.setattr(repo, "_execute", flaky)
    repo.upsert_facts([_fact(Metric.LIQUIDITY, 6900, SEPT_FILED, date(2026, 9, 30))])
    facts, _ = repo.latest_facts("DAL", datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert facts[0].value == 6900
    assert seen["inserts"] == 1


def test_second_lock_raises_store_error(db_path, monkeypatch):
    repo = Repository(db_path)
    repo.migrate()
    original = repo._execute

    def locked(sql, *args):
        if sql.lstrip().upper().startswith("INSERT"):
            raise sqlite3.OperationalError("database is locked")
        return original(sql, *args)

    monkeypatch.setattr(repo, "_execute", locked)
    with pytest.raises(StoreError):
        repo.upsert_facts([_fact(Metric.LIQUIDITY, 1, SEPT_FILED, date(2026, 9, 30))])


def test_snapshot_visibility_stale_flag_and_series(db_path):
    repo = Repository(db_path)
    repo.migrate()
    repo.upsert_market(_meta())
    as_of = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    fresh = _snap(as_of - timedelta(seconds=30))
    old = _snap(as_of - timedelta(minutes=10), bid=0.1, ask=0.2)
    future = _snap(as_of + timedelta(hours=1), bid=0.9, ask=0.95)
    repo.put_snapshots([old, fresh, future])
    snap, stale = repo.latest_snapshot(Venue.KALSHI, "MKT", as_of, stale_after_s=300)
    assert snap is not None and snap.bid == 0.4
    assert stale is False
    older, older_stale = repo.latest_snapshot(
        Venue.KALSHI, "MKT", as_of - timedelta(minutes=4), stale_after_s=300
    )
    assert older is not None and older.bid == 0.1
    assert older_stale is True
    series = repo.snapshot_series(
        Venue.KALSHI, "MKT", as_of - timedelta(hours=1), as_of, as_of
    )
    assert [item.bid for item in series] == [0.1, 0.4]
    assert repo.newest_snapshot_ts() == future.ts


def test_snapshot_without_a_market_is_rejected(db_path):
    repo = Repository(db_path)
    repo.migrate()
    with pytest.raises(StoreError):
        repo.put_snapshots([_snap(datetime(2026, 10, 10, tzinfo=timezone.utc))])


def test_hand_label_beats_llm_and_factor_filter(db_path):
    repo = Repository(db_path)
    repo.migrate()
    hand = MarketLabel(
        relevant=True, factor=Factor.FUEL, yes_moves_factor="up", mechanism="fuel", label_source="hand",
    )
    llm = MarketLabel(
        relevant=True, factor=Factor.RATES, yes_moves_factor="up", mechanism="rates", label_source="llm",
    )
    repo.upsert_market(_meta(), hand)
    repo.upsert_market(_meta(), llm)
    markets = repo.markets()
    assert markets[0][1].label_source == "hand"
    assert markets[0][1].factor == Factor.FUEL
    assert repo.markets(factor=Factor.RATES) == []
    assert len(repo.markets(factor=Factor.FUEL)) == 1
    repo.upsert_market(_meta("OTHER"))
    assert len(repo.markets()) == 2


def test_macro_uses_the_newest_vintage_visible_at_as_of(db_path):
    repo = Repository(db_path)
    repo.migrate()
    repo.put_macro([
        MacroObs(series_id="DGS10", obs_date=date(2026, 7, 10), value=4.1, realtime_start=date(2026, 7, 10)),
        MacroObs(series_id="DGS10", obs_date=date(2026, 7, 10), value=4.4, realtime_start=date(2026, 7, 20)),
        MacroObs(series_id="DGS10", obs_date=date(2026, 10, 8), value=5.22, realtime_start=date(2026, 10, 9)),
    ])
    early = repo.macro_series(
        "DGS10", date(2026, 1, 1), date(2026, 12, 31), datetime(2026, 7, 15, tzinfo=timezone.utc)
    )
    assert [(row.obs_date, row.value, row.realtime_start) for row in early] == [
        (date(2026, 7, 10), 4.1, date(2026, 7, 10))
    ]
    later = repo.macro_series(
        "DGS10", date(2026, 1, 1), date(2026, 12, 31), datetime(2026, 10, 9, tzinfo=timezone.utc)
    )
    assert [(row.obs_date, row.value) for row in later] == [
        (date(2026, 7, 10), 4.4),
        (date(2026, 10, 8), 5.22),
    ]


def test_runs_lists_newest_first_and_can_filter_by_company(db_path):
    repo = Repository(db_path)
    repo.migrate()
    older = _run("run_old")
    newer = _run("run_new").model_copy(update={"created_at": datetime(2026, 10, 2, tzinfo=timezone.utc)})
    other = _run("run_ual").model_copy(update={
        "company": "UAL",
        "created_at": datetime(2026, 10, 3, tzinfo=timezone.utc),
        "inputs": RunRequest(ticker="UAL", note_size_usd_m=1000, tenor_years=7, coupon_spread_bp=150),
    })
    repo.save_run(older)
    repo.save_run(newer)
    repo.save_run(other)
    assert [row.run_id for row in repo.runs(limit=2)] == ["run_ual", "run_new"]
    assert [row.run_id for row in repo.runs(ticker="DAL")] == ["run_new", "run_old"]


def test_run_alert_and_cache_round_trip(db_path):
    repo = Repository(db_path)
    repo.migrate()
    run = _run()
    repo.save_run(run)
    loaded = repo.get_run("run_1")
    assert loaded is not None
    assert loaded.summary.p_breach == 0.2
    assert loaded.brief.llm_status == "pending"
    repo.update_run_brief("run_1", "llm text", "ready")
    updated = repo.get_run("run_1")
    assert updated.brief.llm_md == "llm text"
    assert updated.brief.llm_status == "ready"
    when = datetime(2026, 10, 2, tzinfo=timezone.utc)
    repo.save_alert(Alert(
        alert_id="a1", ts=when, company="DAL", run_id="run_1", rule="BREACH_FLIP",
        dedup_key="dal-breach", severity="high", payload={"p_breach": 0.2}, status="pending",
    ))
    found = repo.alerts("DAL", datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert len(found) == 1 and found[0].payload["p_breach"] == 0.2
    assert repo.alerts("DAL", datetime(2026, 10, 3, tzinfo=timezone.utc)) == []
    repo.cache_put({
        "cache_key": "k1", "task": "extract_metrics", "provider": "fake", "model": "m",
        "response_json": {"ok": True}, "tokens_in": 3, "tokens_out": 4,
    })
    cached = repo.cache_get("k1")
    assert cached["response_json"] == {"ok": True}
    assert cached["task"] == "extract_metrics"
    assert repo.cache_get("missing") is None
    assert repo.get_run("missing") is None


def test_data_version_changes_when_a_later_fact_becomes_visible(db_path):
    repo = Repository(db_path)
    repo.migrate()
    repo.upsert_facts([_fact(Metric.OPERATING_INCOME, 1000, JUNE_FILED, date(2026, 6, 30))])
    before = repo.data_version(AS_OF_OCT8)
    again = repo.data_version(AS_OF_OCT8)
    assert before == again
    repo.upsert_facts([_fact(Metric.OPERATING_INCOME, 1454, SEPT_FILED, date(2026, 9, 30))])
    assert repo.data_version(AS_OF_OCT8) == before
    assert repo.data_version(datetime(2026, 10, 10, tzinfo=timezone.utc)) != before
