"""C7: POST /admin/ingest and GET /admin/jobs/{job_id} (LLD section 7; server rule 4: admin endpoints
run jobs in the background, one job per source group at a time).

The ingest work itself is lane A's (WorkerLoop ticks, backfill). Here each source runs a fake job
that the test can hold open, so "already running" (409) is deterministic.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from airscen import api as api_module
from airscen.config import Settings
from airscen.types import SourceError
from fake_store import FakeStore

P = "/api/v1"


class FakeJobs:
    """One callable per source group; each blocks until released, and records its arguments."""

    def __init__(self, groups=("markets", "filings", "macro", "backfill")):
        self.release = {g: threading.Event() for g in groups}
        self.calls: list[tuple] = []
        self.fail: dict[str, Exception] = {}

    def runners(self):
        def make(group):
            def run(tickers, window):
                self.calls.append((group, tickers, window))
                assert self.release[group].wait(5), f"{group} job was never released"
                if group in self.fail:
                    raise self.fail[group]
            return run
        return {g: make(g) for g in self.release}

    def release_all(self):
        for e in self.release.values():
            e.set()


@pytest.fixture
def jobs():
    j = FakeJobs()
    yield j
    j.release_all()


def build(jobs_runners):
    return api_module.create_app(settings_loader=Settings, repo_factory=lambda s: FakeStore(),
                                 job_runners=lambda settings, repo: jobs_runners)


@pytest.fixture
def client(jobs):
    with TestClient(build(jobs.runners())) as c:
        yield c


def wait_done(client, job_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"{P}/admin/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def assert_envelope(resp, status, code):
    assert resp.status_code == status, resp.text
    err = resp.json()["error"]
    assert set(err) == {"code", "message", "details", "request_id"} and err["code"] == code
    return err


# ---- happy path ----

def test_ingest_returns_202_with_a_job_id_and_runs_in_the_background(client, jobs):
    resp = client.post(P + "/admin/ingest", json={"sources": ["markets", "macro"]})
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    running = client.get(f"{P}/admin/jobs/{job_id}").json()
    assert running["status"] in ("queued", "running") and running["sources"] == ["markets", "macro"]
    jobs.release_all()
    done = wait_done(client, job_id)
    assert done["status"] == "succeeded" and done["errors"] == []
    assert done["progress"] == {"done": 2, "total": 2}
    assert done["started_at"] and done["finished_at"]
    assert [c[0] for c in jobs.calls] == ["markets", "macro"]


def test_tickers_and_window_reach_the_job(client, jobs):
    jobs.release_all()
    job_id = client.post(P + "/admin/ingest", json={"sources": ["filings"], "tickers": ["DAL", "UAL"]}).json()["job_id"]
    wait_done(client, job_id)
    job_id = client.post(P + "/admin/ingest", json={"sources": ["markets"], "window": "2026-07-01..2026-07-12"}).json()["job_id"]
    job = wait_done(client, job_id)
    assert jobs.calls[0] == ("filings", ["DAL", "UAL"], None)
    assert jobs.calls[1][0] == "backfill" and str(jobs.calls[1][2][0]) == "2026-07-01" and str(jobs.calls[1][2][1]) == "2026-07-12"
    assert job["sources"] == ["backfill"] and job["window"] == "2026-07-01..2026-07-12"


def test_default_ingest_runs_markets_filings_and_macro(client, jobs):
    jobs.release_all()
    job = wait_done(client, client.post(P + "/admin/ingest", json={}).json()["job_id"])
    assert job["sources"] == ["markets", "filings", "macro"] and job["status"] == "succeeded"


# ---- one job per source group ----

def test_same_source_group_while_running_is_409_with_the_running_job(client, jobs):
    first = client.post(P + "/admin/ingest", json={"sources": ["markets"]}).json()["job_id"]
    err = assert_envelope(client.post(P + "/admin/ingest", json={"sources": ["markets", "macro"]}), 409, "E_CONFLICT")
    assert err["details"]["job_id"] == first and err["details"]["source"] == "markets"


def test_different_source_groups_can_run_together(client, jobs):
    a = client.post(P + "/admin/ingest", json={"sources": ["markets"]})
    b = client.post(P + "/admin/ingest", json={"sources": ["filings"]})
    assert (a.status_code, b.status_code) == (202, 202)


def test_a_group_can_run_again_once_its_job_finished(client, jobs):
    first = client.post(P + "/admin/ingest", json={"sources": ["macro"]}).json()["job_id"]
    jobs.release["macro"].set()
    wait_done(client, first)
    assert client.post(P + "/admin/ingest", json={"sources": ["macro"]}).status_code == 202


# ---- failures ----

def test_a_failing_source_is_recorded_and_the_others_still_run(client, jobs):
    jobs.fail["markets"] = SourceError("Kalshi is down", code="E_SOURCE_UNAVAILABLE")
    jobs.release_all()
    job = wait_done(client, client.post(P + "/admin/ingest", json={"sources": ["markets", "macro"]}).json()["job_id"])
    assert job["status"] == "partial" and job["progress"] == {"done": 2, "total": 2}
    assert job["errors"] == [{"source": "markets", "code": "E_SOURCE_UNAVAILABLE", "message": "Kalshi is down"}]


def test_all_sources_failing_is_failed_and_unexpected_errors_are_internal(client, jobs):
    jobs.fail["macro"] = RuntimeError("bug")
    jobs.release_all()
    job = wait_done(client, client.post(P + "/admin/ingest", json={"sources": ["macro"]}).json()["job_id"])
    assert job["status"] == "failed"
    assert job["errors"][0]["code"] == "E_INTERNAL" and "bug" not in job["errors"][0]["message"]


def test_a_source_with_no_job_wired_is_502(jobs):
    runners = jobs.runners()
    del runners["filings"]
    with TestClient(build(runners)) as c:
        err = assert_envelope(c.post(P + "/admin/ingest", json={"sources": ["filings"]}), 502, "E_SOURCE_UNAVAILABLE")
    assert "filings" in err["message"]


@pytest.mark.parametrize("body", [
    {"sources": []},
    {"sources": ["weather"]},
    {"sources": ["markets", "markets"]},
    {"window": "2026-07-12..2026-07-01"},
    {"window": "July"},
    {"tickers": ["XYZ"]},
    {"unexpected": True},
])
def test_bad_ingest_requests_are_400(client, body):
    assert_envelope(client.post(P + "/admin/ingest", json=body), 400, "E_VALIDATION")


def test_unknown_job_is_404(client):
    assert_envelope(client.get(P + "/admin/jobs/job_nope"), 404, "E_NOT_FOUND")
