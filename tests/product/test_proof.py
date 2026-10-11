"""The decision proof recomputes the stored answer from the inputs visible at its as-of time.

The worked-example ladder, quotes and baseline WTI are illustrative. The filing figures are Delta's.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from airscen import api as api_module
from airscen.config import Settings
from airscen.service import RunService
from airscen.types import RequestError, Snapshot, Venue
from fake_store import AS_OF, FakeStore, seed_peer, seed_worked_example

P = "/api/v1"
BODY = {"ticker": "DAL", "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150,
        "coverage_threshold": 6.0, "fuel_passthrough": 1.0}


def request(**changes):
    from airscen.types import RunRequest
    return RunRequest(**{"ticker": "DAL", "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150,
                         "coverage_threshold": 6.0, "fuel_passthrough": 1.0, **changes})


@pytest.fixture
def store():
    s = FakeStore()
    seed_worked_example(s)
    seed_peer(s, "UAL")
    return s


def service(store, now=AS_OF) -> RunService:
    return RunService(repo=store, settings=Settings(), clock=lambda: now)


def _failed(proof) -> list[tuple[str, str]]:
    return [(c.code, c.detail) for c in proof.checks if not c.passed]


def test_worked_example_is_proved_from_its_inputs(store):
    run = service(store).create_run(request())
    proof = service(store).prove(run.run_id)

    assert proof.verdict == "proved", _failed(proof)
    assert proof.decision_numbers_from_llm is False
    assert proof.llm_status == "skipped"
    assert any(t.kind == "fact" and t.used_in_decision and "sec.gov" in t.source for t in proof.inputs)
    assert any(t.kind == "macro" and t.name == "DGS10" and t.extraction == "fred" for t in proof.inputs)
    assert any(t.kind == "market" and t.used_in_decision for t in proof.inputs)
    rows = next(c for c in proof.checks if c.code == "scenario_rows")
    assert "1,196.42" in rows.detail and "coverage 7.49" in rows.detail
    breach = next(c for c in proof.checks if c.code == "breach_probability")
    assert "0.297" in breach.detail
    breakeven = next(c for c in proof.checks if c.code == "breakeven")
    assert "103.16" in breakeven.detail
    assert run.peers and run.peers[0].ticker == "UAL"


def test_a_ready_brief_that_invents_a_number_is_not_proved(store):
    svc = service(store)
    run = svc.create_run(request())
    store.update_run_brief(run.run_id, "Coverage falls below the threshold with probability 88.8%.", "ready")

    proof = svc.prove(run.run_id)

    assert proof.verdict == "not_proved"
    brief = next(c for c in proof.checks if c.code == "llm_brief")
    assert brief.passed is False
    assert next(c for c in proof.checks if c.code == "scenario_rows").passed is True


def test_a_rejected_brief_keeps_the_template_and_stays_proved(store):
    svc = service(store)
    run = svc.create_run(request())
    store.update_run_brief(run.run_id, None, "rejected")

    proof = svc.prove(run.run_id)

    assert proof.verdict == "proved", _failed(proof)
    brief = next(c for c in proof.checks if c.code == "llm_brief")
    assert "template" in brief.detail


def test_a_faithful_written_brief_stays_proved(store):
    svc = service(store)
    run = svc.create_run(request())
    store.update_run_brief(run.run_id, run.brief.template_md, "ready")

    assert service(store).prove(run.run_id).verdict == "proved"


def test_a_quote_revised_inside_the_as_of_window_is_not_proved(store):
    svc = service(store)
    run = svc.create_run(request())
    store.snapshots.append(Snapshot(
        venue=Venue.KALSHI, market_id="FED-ILLUSTRATIVE-HIKE", ts=AS_OF - timedelta(seconds=30),
        bid=0.495, ask=0.505, bid_size=1000, ask_size=1000, volume_24h=10_000,
    ))

    proof = svc.prove(run.run_id)

    assert proof.verdict == "not_proved"
    assert next(c for c in proof.checks if c.code == "engine_replay").passed is False
    assert next(c for c in proof.checks if c.code == "input_fingerprint").passed is False


def test_a_validated_model_extraction_is_named_and_the_run_stays_proved(store):
    from airscen.types import Extraction

    store.facts = [
        fact.model_copy(update={"extraction": Extraction.LLM}) if fact.metric.value == "operating_income" else fact
        for fact in store.facts
    ]
    svc = service(store)
    svc.create_run(request())
    proof = svc.prove(svc.repo.runs()[0].run_id)

    assert proof.verdict == "proved", _failed(proof)
    assert proof.decision_numbers_from_llm is True
    boundary = next(c for c in proof.checks if c.code == "llm_boundary")
    assert "operating_income" in boundary.detail and boundary.passed


def test_an_unvalidated_model_extraction_fails_the_boundary():
    from airscen.proof import _llm_boundary
    from airscen.types import InputTrace

    check = _llm_boundary([InputTrace(
        kind="fact", name="operating_income", value="1454 usd_m", source="https://example.test",
        as_of_visible=True, used_in_decision=True, extraction="llm", validated=False,
    )])
    assert check.passed is False
    assert "operating_income" in check.detail


def test_unknown_run_is_not_found(store):
    with pytest.raises(RequestError) as caught:
        service(store).prove("run_missing")
    assert caught.value.code == "E_NOT_FOUND"


def test_missing_inputs_leave_the_stored_run_unproved(store):
    svc = service(store)
    run = svc.create_run(request())
    store.facts = [f for f in store.facts if f.metric.value != "operating_income"]

    proof = svc.prove(run.run_id)

    assert proof.verdict == "not_proved"
    replay = next(c for c in proof.checks if c.code == "engine_replay")
    assert replay.passed is False
    assert "operating_income" in replay.detail


# ---- API ----

def test_get_proof_returns_the_proved_run():
    store = FakeStore()
    seed_worked_example(store)

    def repo_factory(settings):
        return store

    app = api_module.create_app(settings_loader=Settings, repo_factory=repo_factory, clock=lambda: AS_OF)
    with TestClient(app) as client:
        created = client.post(P + "/runs", json=BODY)
        assert created.status_code == 201, created.text
        resp = client.get(f"{P}/runs/{created.json()['run_id']}/proof")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "proved"
    assert body["decision_numbers_from_llm"] is False
    assert any(c["code"] == "llm_boundary" and c["passed"] for c in body["checks"])


def test_get_proof_unknown_run_is_404():
    store = FakeStore()
    seed_worked_example(store)
    app = api_module.create_app(settings_loader=Settings, repo_factory=lambda settings: store, clock=lambda: AS_OF)
    with TestClient(app) as client:
        resp = client.get(P + "/runs/run_missing/proof")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "E_NOT_FOUND"
