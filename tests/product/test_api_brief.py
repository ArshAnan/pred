"""C9: POST /runs returns the template brief at once and upgrades it in a background task;
GET /runs/{id} shows the result (LLD section 5 run lifecycle, server rule 2)."""

from fastapi.testclient import TestClient

from airscen import api as api_module
from airscen.config import Settings
from fake_llm_client import ScriptedLLM
from fake_store import AS_OF, FakeStore, seed_worked_example
from test_brief_llm import GOOD, INVENTED

P = "/api/v1"
BODY = {"ticker": "DAL", "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150, "coverage_threshold": 6.0}


def client_for(llm=None, llm_factory=None):
    store = FakeStore()
    seed_worked_example(store)
    factory = llm_factory or ((lambda s, r: llm) if llm is not None else None)
    app = api_module.create_app(settings_loader=Settings, repo_factory=lambda s: store, clock=lambda: AS_OF,
                                llm_factory=factory)
    return TestClient(app)


def test_post_returns_pending_then_get_shows_ready():
    with client_for(ScriptedLLM(GOOD)) as c:
        created = c.post(P + "/runs", json=BODY)
        assert created.status_code == 201 and created.json()["brief"]["llm_status"] == "pending"
        later = c.get(f"{P}/runs/{created.json()['run_id']}").json()
    assert later["brief"]["llm_status"] == "ready" and later["brief"]["llm_md"] == GOOD
    assert later["brief"]["template_md"] == created.json()["brief"]["template_md"]


def test_an_unfaithful_llm_brief_is_rejected():
    with client_for(ScriptedLLM(INVENTED)) as c:
        run_id = c.post(P + "/runs", json=BODY).json()["run_id"]
        brief = c.get(f"{P}/runs/{run_id}").json()["brief"]
    assert brief["llm_status"] == "rejected" and brief["llm_md"] is None


def test_without_an_llm_the_brief_is_skipped():
    with client_for() as c:
        run = c.post(P + "/runs", json=BODY).json()
    assert run["brief"]["llm_status"] == "skipped"


def test_an_llm_that_cannot_be_built_means_skipped_not_a_crash():
    def broken(settings, repo):
        raise RuntimeError("no providers")
    with client_for(llm_factory=broken) as c:
        assert c.post(P + "/runs", json=BODY).json()["brief"]["llm_status"] == "skipped"


def test_an_idempotent_replay_does_not_ask_the_llm_again():
    llm = ScriptedLLM(GOOD)
    with client_for(llm) as c:
        c.post(P + "/runs", json=BODY, headers={"Idempotency-Key": "k"})
        c.post(P + "/runs", json=BODY, headers={"Idempotency-Key": "k"})
    assert len(llm.calls) == 1
