"""C7: cli.py ingest / backfill start jobs through the API and wait for them (fake API via respx)."""

import json

import httpx
import pytest
import respx

from airscen import cli

BASE = "http://127.0.0.1:8000/api/v1"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("API_BASE_URL", BASE)
    monkeypatch.delenv("API_TOKEN", raising=False)


@pytest.fixture
def api():
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


def job(status, done=0, total=1, errors=None, sources=("markets", "filings", "macro"), window=None):
    return {"job_id": "job_1", "status": status, "sources": list(sources), "tickers": None, "window": window,
            "progress": {"done": done, "total": total}, "errors": errors or [],
            "started_at": "2026-10-11T00:00:00Z", "finished_at": None if status == "running" else "2026-10-11T00:00:05Z"}


def run(*argv):
    return cli.main(list(argv), sleep=lambda s: None)


def test_ingest_starts_a_job_and_waits_for_success(api, capsys):
    post = api.post("/admin/ingest").respond(202, json={"job_id": "job_1"})
    api.get("/admin/jobs/job_1").mock(side_effect=[
        httpx.Response(200, json=job("running", 1, 3)), httpx.Response(200, json=job("succeeded", 3, 3)),
    ])
    assert run("ingest") == 0
    assert json.loads(post.calls.last.request.content) == {"sources": ["markets", "filings", "macro"]}
    out = capsys.readouterr().out
    assert "job_1" in out and "succeeded" in out


def test_ingest_options_are_sent(api):
    post = api.post("/admin/ingest").respond(202, json={"job_id": "job_1"})
    api.get("/admin/jobs/job_1").respond(200, json=job("succeeded"))
    assert run("ingest", "--sources", "markets,macro", "--tickers", "DAL,UAL") == 0
    assert json.loads(post.calls.last.request.content) == {"sources": ["markets", "macro"], "tickers": ["DAL", "UAL"]}


def test_no_wait_returns_after_starting(api):
    api.post("/admin/ingest").respond(202, json={"job_id": "job_1"})
    jobs = api.get("/admin/jobs/job_1").respond(200, json=job("running"))
    assert run("ingest", "--no-wait") == 0
    assert jobs.call_count == 0


def test_backfill_sends_the_window(api):
    post = api.post("/admin/ingest").respond(202, json={"job_id": "job_1"})
    api.get("/admin/jobs/job_1").respond(200, json=job("succeeded", sources=["backfill"], window="2026-07-01..2026-07-12"))
    assert run("backfill", "--window", "2026-07-01..2026-07-12") == 0
    assert json.loads(post.calls.last.request.content) == {"sources": ["markets"], "window": "2026-07-01..2026-07-12"}


def test_backfill_needs_a_window(api, capsys):
    with pytest.raises(SystemExit) as exc:
        run("backfill")
    assert exc.value.code == 2


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_a_job_with_errors_exits_1_and_prints_them(api, capsys, status):
    api.post("/admin/ingest").respond(202, json={"job_id": "job_1"})
    errors = [{"source": "markets", "code": "E_SOURCE_UNAVAILABLE", "message": "Kalshi is down"}]
    api.get("/admin/jobs/job_1").respond(200, json=job(status, 1, 1, errors))
    assert run("ingest") == 1
    out = capsys.readouterr().out
    assert "Kalshi is down" in out and "E_SOURCE_UNAVAILABLE" in out


def test_api_errors_exit_2_with_the_code(api, capsys):
    api.post("/admin/ingest").respond(409, json={"error": {"code": "E_CONFLICT", "message": "markets is already running",
                                                            "details": {"job_id": "job_0"}, "request_id": "r1"}})
    assert run("ingest") == 2
    err = capsys.readouterr().err
    assert "E_CONFLICT" in err and "job_0" in err


def test_unreachable_api_exits_2_and_says_where_it_looked(api, capsys):
    api.post("/admin/ingest").mock(side_effect=httpx.ConnectError("refused"))
    assert run("ingest") == 2
    assert BASE in capsys.readouterr().err


def test_eval_is_not_wired_yet(capsys):
    assert run("eval") == 2
    assert "C14" in capsys.readouterr().err
