"""Lane C on lane A's real store: tests/fakes/fake_repo.py (the real Repository on in-memory SQLite,
CP0_DECISIONS R5), seeded with the same ILLUSTRATIVE worked example as fake_store.py. If either side
changes a return shape, these fail instead of the demo."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from airscen import api as api_module
from airscen.alerts import AlertEngine, SlackSender
from airscen.config import Settings
from airscen.service import RunService
from airscen.types import RunRequest
from fake_llm_client import ScriptedLLM
from fake_store import AS_OF, FakeStore, seed_peer, seed_worked_example
from test_brief_llm import GOOD
from tests.fakes.fake_repo import FakeRepository
from tests.fakes.fake_slack import FakeSlack

P = "/api/v1"
BODY = {"ticker": "DAL", "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150, "coverage_threshold": 6.0}


def real_repo(with_peer: bool = False) -> FakeRepository:
    seed = FakeStore()
    seed_worked_example(seed)
    if with_peer:
        seed_peer(seed, "UAL")
    return FakeRepository({"facts": seed.facts, "markets": [(v.meta, v.label) for v in seed.market_views],
                           "snapshots": seed.snapshots, "macro": seed.macro})


def service(repo, llm=None):
    return RunService(repo=repo, settings=Settings(), llm=llm, clock=lambda: AS_OF)


def test_golden_run_on_the_real_store():
    repo = real_repo()
    run = service(repo).create_run(RunRequest(**BODY))
    s = run.summary
    assert s.p_breach == pytest.approx(0.297, abs=0.0005)
    assert (s.breakeven_wti_hold, s.breakeven_wti_hike) == (pytest.approx(103.16, abs=0.005), pytest.approx(102.77, abs=0.005))
    assert s.coverage_p50 == pytest.approx(7.49, abs=0.005) and s.liquidity_cover == pytest.approx(2.08, abs=0.005)
    assert set(run.flags) == {"VARIABLE_SHARE_STALE", "CRUDE_ONLY", "RESOLVE_DATE_PROXY"}
    assert repo.get_run(run.run_id) == run
    assert run.data_version == repo.data_version(AS_OF)


def test_peers_on_the_real_store():
    run = service(real_repo(with_peer=True)).create_run(RunRequest(**BODY))
    assert [p.ticker for p in run.peers] == ["UAL"] and run.peers[0].summary == run.summary


def test_llm_brief_is_saved_on_the_real_store():
    repo = real_repo()
    svc = service(repo, ScriptedLLM(GOOD))
    run = svc.create_run(RunRequest(**BODY))
    svc.finish_brief(run.run_id)
    saved = repo.get_run(run.run_id)
    assert saved.brief.llm_status == "ready" and saved.brief.llm_md == GOOD


def test_stale_quotes_on_the_real_store():
    repo = real_repo()
    run = service(repo).create_run(RunRequest(**BODY | {"as_of": AS_OF + timedelta(minutes=10)}))
    assert "STALE_MARKETS" in run.flags and run.summary.p_breach is None


def test_breach_flip_is_saved_and_listed_on_the_real_store():
    repo, slack = real_repo(), FakeSlack()
    settings = Settings(slack_webhook_url="https://hooks.slack.test/x")
    eng = AlertEngine(repo=repo, settings=settings, service=service(repo), clock=lambda: AS_OF,
                      slack=SlackSender(settings.slack_webhook_url, post=slack.post, sleep=lambda s: None))
    eng.tick(AS_OF, coverage_threshold=3.0)
    [alert] = eng.tick(AS_OF, coverage_threshold=6.0)
    assert alert.rule == "BREACH_FLIP" and alert.status == "sent"
    assert [a.alert_id for a in repo.alerts("DAL", AS_OF - timedelta(days=1))] == [alert.alert_id]


def test_the_api_end_to_end_on_the_real_store():
    repo = real_repo()
    app = api_module.create_app(settings_loader=Settings, repo_factory=lambda s: repo, clock=lambda: AS_OF)
    with TestClient(app) as c:
        created = c.post(P + "/runs", json=BODY)
        assert created.status_code == 201
        assert c.get(f"{P}/runs/{created.json()['run_id']}").json() == created.json()
        assert len(c.get(P + "/markets").json()) == 8
        assert len(c.get(P + "/markets", params={"factor": "fuel"}).json()) == 5
        assert len(c.get(P + "/companies/DAL/facts").json()) == 7
        assert len(c.get(P + "/markets/kalshi/WTI-ILLUSTRATIVE-80/history").json()) == 1
        assert c.get(P + "/health").json()["db"] == "ok"
        listed = c.get(P + "/runs")
    # Lane A's store has no runs() list yet: GET /runs says so instead of crashing.
    assert listed.status_code == 503 and "Repository.runs" in listed.json()["error"]["message"]
