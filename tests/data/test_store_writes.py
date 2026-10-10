"""A2: the LLD schema and one-transaction write paths."""

import sqlite3
from datetime import date, datetime, timezone

import pytest

from airscen.store import Repository
from airscen.types import (
    Alert, Basis, Brief, Extraction, Fact, MacroObs, MarketKind,
    MarketLabel, MarketMeta, MarketView, Metric, RunRequest, RunResult, ScenarioSummary,
    Snapshot, Venue,
)


NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "app.db"
    repository = Repository(path)
    repository.migrate()
    return repository, path


def rows(path, table):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


def test_migrate_creates_exact_schema_and_enables_connection_settings(repo):
    repository, path = repo
    repository.migrate()  # repeatable
    with repository._connect() as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        assert tables == {"facts", "markets", "snapshots", "macro", "runs", "alerts", "llm_cache"}
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_facts_round_trip_and_batch_rollback(repo):
    repository, path = repo
    fact = Fact(company="DAL", metric=Metric.OPERATING_INCOME, value=1454, unit="usd_m",
                basis=Basis.GAAP, period_end=date(2026, 9, 30), filed_at=NOW,
                source_url="https://example.test/filing", extraction=Extraction.XBRL, validated=True)
    repository.upsert_facts([fact])
    repository.upsert_facts([fact.model_copy(update={"value": 1455})])
    saved = rows(path, "facts")
    assert len(saved) == 1
    assert saved[0]["value"] == 1455
    assert saved[0]["validated"] == 1
    assert saved[0]["loaded_at"]

    bad = fact.model_copy(update={"company": "UAL", "basis": "INVALID"})
    with pytest.raises(Exception):
        repository.upsert_facts([fact.model_copy(update={"company": "UAL"}), bad])
    assert len(rows(path, "facts")) == 1


def test_market_and_snapshot_round_trip_with_foreign_key(repo):
    repository, path = repo
    meta = MarketMeta(venue=Venue.KALSHI, market_id="WTI-100", title="WTI over $100",
                      kind=MarketKind.CLOSE_ABOVE, threshold=100, threshold_unit="usd_per_bbl")
    repository.upsert_market(meta)
    snap = Snapshot(venue=Venue.KALSHI, market_id=meta.market_id, ts=NOW,
                    bid=0.4, ask=0.5, src="backfill_kalshi")
    repository.put_snapshots([snap])
    assert rows(path, "markets")[0]["content_hash"]
    assert rows(path, "snapshots")[0]["src"] == "backfill_kalshi"
    with pytest.raises(Exception):
        repository.put_snapshots([snap.model_copy(update={"market_id": "missing"})])
    assert len(rows(path, "snapshots")) == 1


def test_hand_market_label_survives_llm_refresh(repo):
    repository, path = repo
    meta = MarketMeta(venue=Venue.KALSHI, market_id="WTI-100", title="WTI over $100")
    hand = MarketLabel(relevant=True, factor="fuel", yes_moves_factor="up",
                       label_source="hand")
    llm = MarketLabel(relevant=False, label_source="llm")
    repository.upsert_market(MarketView(meta=meta, label=hand))
    repository.upsert_market(MarketView(meta=meta, label=llm))
    market = rows(path, "markets")[0]
    assert (market["relevant"], market["factor"], market["label_source"]) == (1, "fuel", "hand")


def test_macro_round_trip(repo):
    repository, path = repo
    obs = MacroObs(series_id="DGS10", obs_date=date(2026, 10, 9), value=4.2,
                   realtime_start=date(2026, 10, 10))
    repository.put_macro([obs])
    repository.put_macro([obs.model_copy(update={"value": 4.3})])
    assert rows(path, "macro") == [{"series_id": "DGS10", "obs_date": "2026-10-09",
                                     "value": 4.3, "realtime_start": "2026-10-10"}]


def test_run_alert_and_brief_round_trip(repo):
    repository, path = repo
    request = RunRequest(ticker="DAL", as_of=NOW, note_size_usd_m=1000,
                         tenor_years=7, coupon_spread_bp=150)
    result = RunResult(run_id="r1", company="DAL", as_of=NOW, created_at=NOW,
                       inputs=request, rows=[], peers=[], flags=[], data_version="v1",
                       summary=ScenarioSummary(p_breach=None, breakeven_wti_hold=None,
                                               breakeven_wti_hike=None, coverage_p50=7,
                                               coverage_p90=5, liquidity_cover=None,
                                               liquidity_ok=None),
                       brief=Brief(template_md="Template", llm_md=None, llm_status="pending"))
    repository.save_run(result)
    repository.update_run_brief("r1", "Finished", "ready")
    saved = rows(path, "runs")[0]
    assert saved["brief_template_md"] == "Template"
    assert saved["brief_llm_md"] == "Finished"
    assert saved["brief_llm_status"] == "ready"
    assert RunResult.model_validate_json(saved["result_json"]).brief.llm_md == "Finished"

    alert = Alert(alert_id="a1", ts=NOW, company="DAL", run_id="r1",
                  rule="BREACH_FLIP", dedup_key="DAL:breach", severity="high",
                  payload={"p_breach": 0.2}, status="pending")
    repository.save_alert(alert)
    assert rows(path, "alerts")[0]["payload_json"] == '{"p_breach":0.2}'
    with pytest.raises(Exception):
        repository.save_alert(alert.model_copy(update={"alert_id": "a2", "run_id": "missing"}))
    assert len(rows(path, "alerts")) == 1


def test_cache_round_trip(repo):
    repository, path = repo
    entry = {"cache_key": "abc", "task": "brief", "provider": "gemini", "model": "m",
             "response_json": '{"text":"hello"}', "tokens_in": 10, "tokens_out": 3,
             "created_at": NOW.isoformat()}
    repository.cache_put(entry)
    assert rows(path, "llm_cache") == [entry]
