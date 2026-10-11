"""Sensitivity of a saved run: service and API, on the illustrative worked example."""

import pytest
from fastapi.testclient import TestClient

from airscen import api as api_module
from airscen.config import Settings
from airscen.service import RunService
from airscen.types import RunRequest
from fake_store import AS_OF, FakeStore, seed_worked_example

P = "/api/v1"


def _request() -> RunRequest:
    return RunRequest(ticker="DAL", note_size_usd_m=1000, tenor_years=7, coupon_spread_bp=150,
                      coverage_threshold=6.0, fuel_passthrough=1.0)


@pytest.fixture
def store():
    s = FakeStore()
    seed_worked_example(s)
    return s


def test_sensitivity_matches_the_saved_run_and_names_a_flip(store):
    svc = RunService(repo=store, settings=Settings(), clock=lambda: AS_OF)
    run = svc.create_run(_request())
    report = svc.sensitivity(run.run_id)

    assert report.run_id == run.run_id
    assert report.baseline_p_breach == pytest.approx(run.summary.p_breach, abs=1e-12)
    assert report.line == 0.20
    threshold = next(lever for lever in report.levers if lever.name == "coverage_threshold")
    assert threshold.flip_value is not None
    assert any(point.is_baseline and point.p_breach == pytest.approx(run.summary.p_breach) for point in threshold.points)
    assert len(store.saved) == 1  # the sweep does not save more runs


def test_unknown_run_is_404(store):
    from airscen.types import RequestError

    svc = RunService(repo=store, settings=Settings(), clock=lambda: AS_OF)
    with pytest.raises(RequestError) as exc:
        svc.sensitivity("run_missing")
    assert exc.value.code == "E_NOT_FOUND"


def test_get_sensitivity_returns_the_report(store):
    app = api_module.create_app(settings_loader=Settings, repo_factory=lambda s: store, clock=lambda: AS_OF)
    with TestClient(app) as client:
        created = client.post(f"{P}/runs", json=_request().model_dump(mode="json"))
        assert created.status_code == 201, created.text
        run_id = created.json()["run_id"]
        resp = client.get(f"{P}/runs/{run_id}/sensitivity")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == run_id
    assert body["baseline_p_breach"] == pytest.approx(created.json()["summary"]["p_breach"])
    assert body["most_sensitive"]
    assert {lever["name"] for lever in body["levers"]} == {
        "note_size_usd_m", "coupon_spread_bp", "coverage_threshold", "fuel_passthrough",
    }


def test_get_sensitivity_unknown_run_is_404(store):
    app = api_module.create_app(settings_loader=Settings, repo_factory=lambda s: store, clock=lambda: AS_OF)
    with TestClient(app) as client:
        resp = client.get(f"{P}/runs/run_missing/sensitivity")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "E_NOT_FOUND"
