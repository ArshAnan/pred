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


def alert(rule="BREACH_FLIP", status="sent"):
    return {"alert_id": "alert_1", "ts": "2026-10-09T14:30:00Z", "company": "DAL", "run_id": "run_1", "rule": rule,
            "dedup_key": f"DAL:{rule}:-", "severity": "high", "status": status,
            "payload": {"before": 0.074, "after": 0.297}}


def test_flip_runs_a_low_then_a_high_threshold_tick(api, capsys):
    tick = api.post("/admin/alerts/tick").mock(side_effect=[
        httpx.Response(200, json={"alerts": []}), httpx.Response(200, json={"alerts": [alert()]})])
    assert run("flip") == 0
    assert [json.loads(c.request.content) for c in tick.calls] == [{"coverage_threshold": 3.0}, {"coverage_threshold": 6.0}]
    out = capsys.readouterr().out
    assert "BREACH_FLIP" in out and "sent" in out


def test_flip_that_does_not_flip_exits_1(api, capsys):
    api.post("/admin/alerts/tick").respond(200, json={"alerts": []})
    assert run("flip", "--low", "2", "--high", "4") == 1
    assert "No BREACH_FLIP" in capsys.readouterr().out


def test_flip_reports_an_undelivered_alert(api, capsys):
    api.post("/admin/alerts/tick").mock(side_effect=[
        httpx.Response(200, json={"alerts": []}), httpx.Response(200, json={"alerts": [alert(status="failed")]})])
    assert run("flip") == 1
    assert "not delivered" in capsys.readouterr().out


REPLAY_BODY = {"ticker": "DAL", "as_of": "2026-07-10T20:00:00Z", "note_size_usd_m": 1000.0, "tenor_years": 7,
               "coupon_spread_bp": 150.0, "coverage_threshold": 3.0, "fuel_passthrough": 1.0, "include_peers": True}


def run_with(run_id, status):
    return {"run_id": run_id, "brief": {"template_md": "t", "llm_md": "x" if status == "ready" else None,
                                        "llm_status": status}}


def test_warm_runs_the_replay_question_and_waits_for_its_brief(api, capsys):
    post = api.post("/runs").respond(201, json=run_with("run_r", "pending"))
    api.get("/runs/run_r").mock(side_effect=[httpx.Response(200, json=run_with("run_r", "pending")),
                                             httpx.Response(200, json=run_with("run_r", "ready"))])
    assert run("warm") == 0
    assert json.loads(post.calls.last.request.content) == REPLAY_BODY  # exactly what the Replay page sends
    out = capsys.readouterr().out
    assert "ready" in out and "LLM_MODE=cache_only" in out


def test_warm_also_runs_a_pinned_demo_as_of(api):
    post = api.post("/runs").respond(201, json=run_with("run_x", "ready"))
    assert run("warm", "--as-of", "2026-10-11T14:00:00Z") == 0
    sent = [json.loads(c.request.content)["as_of"] for c in post.calls]
    assert sent == ["2026-07-10T20:00:00Z", "2026-10-11T14:00:00Z"]


@pytest.mark.parametrize("status", ["failed", "rejected", "skipped"])
def test_warm_exits_1_when_a_brief_did_not_reach_ready(api, capsys, status):
    api.post("/runs").respond(201, json=run_with("run_r", status))
    assert run("warm") == 1
    assert status in capsys.readouterr().out


def test_warm_reports_an_api_error(api, capsys):
    api.post("/runs").respond(422, json={"error": {"code": "E_MISSING_FACT", "message": "missing DGS10",
                                                   "details": {"missing": ["DGS10"]}, "request_id": "r"}})
    assert run("warm") == 2
    assert "E_MISSING_FACT" in capsys.readouterr().err


def test_eval_runs_lane_b_module(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.runpy, "run_module", lambda name, run_name: calls.append((name, run_name)))
    assert run("eval") == 0
    assert calls == [("airscen.eval", "__main__")]
