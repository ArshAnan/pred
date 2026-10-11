"""C8: lane A's WorkerLoop runs inside the API process (DD-2): built and started in the lifespan,
stopped on shutdown, its status fed into /health, and its ticks used as the ingest jobs.

Done when /health shows per-source status from the running worker. The worker here is the
TEMPORARY FakeWorker (fake_worker.py) that follows the CP0_DECISIONS D5 contract.
"""

import time

import pytest
from fastapi.testclient import TestClient

from airscen import api as api_module
from airscen.config import Settings
from airscen.types import SourceError, SourceStatus, StoreError, WorkerStatus
from fake_store import FakeStore
from fake_worker import SOURCES, FakeWorker

P = "/api/v1"


def build(worker=None, store=None, repo_error=None, factory_calls=None, **kw):
    def repo_factory(settings):
        if repo_error:
            raise repo_error
        return store or FakeStore()

    def worker_factory(settings, repo, after_market_tick=None):
        if factory_calls is not None:
            factory_calls.append((settings, repo))
        return worker

    return api_module.create_app(settings_loader=Settings, repo_factory=repo_factory,
                                 worker_factory=worker_factory if worker is not None else None, **kw)


def health(client):
    resp = client.get(P + "/health")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- lifecycle ----

def test_worker_is_built_with_settings_and_repo_started_and_stopped():
    worker, store, calls = FakeWorker(), FakeStore(), []
    with TestClient(build(worker, store=store, factory_calls=calls)):
        assert worker.started == 1 and worker.stopped == 0
        assert calls[0][1] is store and isinstance(calls[0][0], Settings)
    assert worker.stopped == 1


def test_no_worker_is_started_when_the_database_is_down():
    worker = FakeWorker()
    with TestClient(build(worker, repo_error=StoreError("locked"))) as c:
        assert c.get(P + "/health").status_code == 503
    assert worker.started == 0 and worker.stopped == 0


def test_a_worker_that_fails_to_start_leaves_the_api_up_and_degraded():
    worker = FakeWorker(fail_start=RuntimeError("bad key"))
    with TestClient(build(worker)) as c:
        h = health(c)
    assert h["status"] == "degraded" and h["worker"]["degraded"] is True and h["db"] == "ok"
    assert worker.stopped == 0  # never started, so nothing to stop


# ---- /health from the running worker ----

def test_health_shows_per_source_status_from_the_running_worker():
    worker = FakeWorker()
    with TestClient(build(worker)) as c:
        h = health(c)
    assert h["status"] == "ok" and h["db"] == "ok"
    assert set(h["sources"]) == set(SOURCES)
    assert h["sources"]["kalshi"] == {"status": "ok", "last_ok_at": "2026-10-09T14:29:00Z", "fail_streak": 0}
    assert h["worker"] == {"last_tick_at": "2026-10-09T14:29:00Z", "degraded": False}


def test_health_follows_the_worker_as_it_changes():
    worker = FakeWorker()
    with TestClient(build(worker)) as c:
        assert health(c)["status"] == "ok"
        worker.sources["kalshi"] = SourceStatus(status="degraded", fail_streak=20)
        h = health(c)
    assert h["status"] == "degraded" and h["sources"]["kalshi"]["fail_streak"] == 20


@pytest.mark.parametrize("change", [
    {"source": SourceStatus(status="down", fail_streak=50)},
    {"worker": WorkerStatus(degraded=True)},
])
def test_a_down_source_or_degraded_worker_makes_health_degraded(change):
    worker = FakeWorker()
    if "source" in change:
        worker.sources["fred"] = change["source"]
    else:
        worker.worker = change["worker"]
    with TestClient(build(worker)) as c:
        h = health(c)
    assert h["status"] == "degraded" and h["db"] == "ok"


def test_a_worker_status_error_does_not_break_health():
    worker = FakeWorker(fail_status=RuntimeError("boom"))
    with TestClient(build(worker)) as c:
        h = health(c)
    assert h["status"] == "degraded" and h["worker"]["degraded"] is True


def test_without_a_worker_health_has_no_sources():
    with TestClient(build()) as c:
        h = health(c)
    assert h["status"] == "ok" and h["sources"] == {}


# ---- ingest jobs run the worker's ticks ----

def wait_done(client, job_id):
    for _ in range(250):
        job = client.get(f"{P}/admin/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_make_ingest_runs_the_worker_ticks():
    worker = FakeWorker()
    with TestClient(build(worker)) as c:
        job = wait_done(c, c.post(P + "/admin/ingest", json={}).json()["job_id"])
    assert job["status"] == "succeeded"
    assert worker.ticks == ["discover", "markets", "filings", "macro"]


def test_backfill_is_not_wired_to_the_worker_yet():
    with TestClient(build(FakeWorker())) as c:
        resp = c.post(P + "/admin/ingest", json={"window": "2026-07-01..2026-07-12"})
    assert resp.status_code == 502 and resp.json()["error"]["details"]["source"] == "backfill"


def test_backfill_runs_the_worker_backfill_over_the_whole_window():
    worker, calls = FakeWorker(), []
    worker.backfill = lambda start, end: calls.append((start, end)) or []
    with TestClient(build(worker)) as c:
        job = wait_done(c, c.post(P + "/admin/ingest", json={"window": "2026-07-01..2026-07-12"}).json()["job_id"])
    assert job["status"] == "succeeded"
    assert [(s.isoformat(), e.isoformat()) for s, e in calls] == [("2026-07-01T00:00:00+00:00", "2026-07-13T00:00:00+00:00")]


def test_health_shows_the_newest_snapshot_age_from_the_worker():
    worker = FakeWorker()
    worker.last_snapshot_age_s = lambda: 42.0
    with TestClient(build(worker)) as c:
        assert health(c)["last_snapshot_age_s"] == 42.0


def test_health_is_503_when_the_store_ping_fails():
    store = FakeStore()
    store.ping = lambda: False
    with TestClient(build(FakeWorker(), store=store)) as c:
        assert c.get(P + "/health").status_code == 503


def test_explicit_job_runners_take_precedence_over_the_worker():
    worker, called = FakeWorker(), []
    runners = {"markets": lambda tickers, window: called.append("custom")}
    with TestClient(build(worker, job_runners=lambda s, r: runners)) as c:
        wait_done(c, c.post(P + "/admin/ingest", json={"sources": ["markets"]}).json()["job_id"])
    assert called == ["custom"] and worker.ticks == []


def test_a_failing_tick_is_reported_on_the_job():
    worker = FakeWorker()
    worker.tick_filings = lambda: (_ for _ in ()).throw(SourceError("EDGAR down", code="E_SOURCE_UNAVAILABLE"))
    with TestClient(build(worker)) as c:
        job = wait_done(c, c.post(P + "/admin/ingest", json={}).json()["job_id"])
    assert job["status"] == "partial"
    assert job["errors"] == [{"source": "filings", "code": "E_SOURCE_UNAVAILABLE", "message": "EDGAR down"}]
