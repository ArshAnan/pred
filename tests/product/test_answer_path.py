"""Answer path: the same question through time, on the illustrative worked example.

A live run drops a quote older than the freshness window. The path keeps it and says so.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from airscen import api as api_module
from airscen.config import Settings
from airscen.service import RunService
from airscen.types import AnswerPathRequest, RunRequest
from fake_store import AS_OF, FakeStore, seed_worked_example

P = "/api/v1"


def _question(**changes) -> dict:
    body = {
        "ticker": "DAL", "start": AS_OF, "end": AS_OF, "step_hours": 24,
        "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150,
        "coverage_threshold": 6.0, "fuel_passthrough": 1.0,
    }
    body.update(changes)
    return body


@pytest.fixture
def store():
    s = FakeStore()
    seed_worked_example(s)
    return s


def _service(store) -> RunService:
    return RunService(repo=store, settings=Settings(), clock=lambda: AS_OF)


def test_a_day_before_the_filing_is_a_gap_and_the_filing_day_matches_the_run(store):
    path = _service(store).answer_path(AnswerPathRequest(**_question(
        start=AS_OF - timedelta(hours=24), end=AS_OF, step_hours=24,
    )))
    assert path.n_gap == 1 and path.n_ok == 1
    assert path.points[0].status == "gap"
    assert path.points[1].status == "ok"
    assert path.points[1].p_breach == pytest.approx(0.297, abs=0.0005)
    assert path.points[1].fresh is True
    assert path.points[1].call == "on_or_above_line"
    assert path.crossings == []
    assert store.saved == {}
    assert path.reading.startswith("2 points, 1 computed and 1 missing")


def test_the_path_keeps_a_stale_quote_that_a_live_run_drops(store):
    store.snapshots = [snap.model_copy(update={"ts": AS_OF - timedelta(hours=2)}) for snap in store.snapshots]
    svc = _service(store)
    run = svc.create_run(RunRequest(**{
        "ticker": "DAL", "as_of": AS_OF, "note_size_usd_m": 1000, "tenor_years": 7,
        "coupon_spread_bp": 150, "coverage_threshold": 6.0, "fuel_passthrough": 1.0,
    }))
    assert run.summary.p_breach is None
    assert "STALE_MARKETS" in run.flags

    path = svc.answer_path(AnswerPathRequest(**_question()))
    assert path.points[0].p_breach == pytest.approx(0.297, abs=0.0005)
    assert path.points[0].fresh is False
    assert "STALE_MARKETS" in path.points[0].flags
    assert "not fresh" in path.reading


def test_a_later_oil_quote_can_cross_the_alert_line(store):
    later = AS_OF + timedelta(hours=2)
    for snap in list(store.snapshots):
        if not snap.market_id.startswith("WTI"):
            continue
        store.snapshots.append(snap.model_copy(update={
            "ts": later - timedelta(hours=1), "bid": 0.01, "ask": 0.02,
        }))
    path = _service(store).answer_path(AnswerPathRequest(**_question(start=AS_OF, end=later, step_hours=2)))
    assert [point.call for point in path.points] == ["on_or_above_line", "below_line"]
    assert path.crossings == [later]
    assert path.points[0].fresh is True and path.points[1].fresh is False
    first, second = path.points
    assert second.coverage_change_from_fuel + second.coverage_change_from_funding == pytest.approx(
        second.coverage_p50 - first.coverage_p50, abs=1e-9,
    )
    assert "crosses the 20% alert line" in path.reading


def test_post_path_returns_the_series(store):
    app = api_module.create_app(settings_loader=Settings, repo_factory=lambda s: store, clock=lambda: AS_OF)
    with TestClient(app) as client:
        resp = client.post(f"{P}/runs/path", json={
            "ticker": "DAL",
            "start": (AS_OF - timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
            "end": AS_OF.isoformat().replace("+00:00", "Z"),
            "step_hours": 24,
            "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150,
            "coverage_threshold": 6.0, "fuel_passthrough": 1.0,
        })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["n_ok"] == 1 and body["n_gap"] == 1
    assert body["points"][1]["p_breach"] == pytest.approx(0.297, abs=0.0005)
    assert store.saved == {}


def test_more_than_48_points_is_400(store):
    app = api_module.create_app(settings_loader=Settings, repo_factory=lambda s: store, clock=lambda: AS_OF)
    with TestClient(app) as client:
        resp = client.post(f"{P}/runs/path", json={
            "ticker": "DAL",
            "start": (AS_OF - timedelta(hours=72)).isoformat().replace("+00:00", "Z"),
            "end": AS_OF.isoformat().replace("+00:00", "Z"),
            "step_hours": 1,
            "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150,
            "coverage_threshold": 6.0, "fuel_passthrough": 1.0,
        })
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "E_VALIDATION"


def test_unknown_ticker_is_404(store):
    app = api_module.create_app(settings_loader=Settings, repo_factory=lambda s: store, clock=lambda: AS_OF)
    body = _question(ticker="XYZ")
    body["start"] = AS_OF.isoformat().replace("+00:00", "Z")
    body["end"] = AS_OF.isoformat().replace("+00:00", "Z")
    with TestClient(app) as client:
        resp = client.post(f"{P}/runs/path", json=body)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "E_NOT_FOUND"
