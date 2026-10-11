"""C12: GET /alerts, POST /admin/alerts/test and the demo flip (POST /admin/alerts/tick), plus the worker
hookup of the alert engine (C11's after_market_tick). Slack is lane C's FakeSlack; data is ILLUSTRATIVE."""

import pytest
from fastapi.testclient import TestClient

from airscen import api as api_module
from airscen.alerts import AlertEngine, SlackSender
from airscen.config import Settings
from fake_store import AS_OF, FakeStore, seed_worked_example
from fake_worker import FakeWorker
from tests.fakes.fake_slack import FakeSlack

P = "/api/v1"
HOOK = "https://hooks.slack.test/services/T/B/x"


def build(slack, store=None, worker=None, with_alerts=True, factory_calls=None):
    store = store or FakeStore()
    if not store.facts:
        seed_worked_example(store)

    def alerts_factory(settings, repo, service, status_fn):
        return AlertEngine(repo=repo, settings=settings, service=service, clock=lambda: AS_OF, status_fn=status_fn,
                           slack=SlackSender(settings.slack_webhook_url, post=slack.post, sleep=lambda s: None))

    def worker_factory(settings, repo, after_market_tick=None):
        if factory_calls is not None:
            factory_calls.append(after_market_tick)
        return worker

    return api_module.create_app(
        settings_loader=lambda: Settings(slack_webhook_url=HOOK), repo_factory=lambda s: store, clock=lambda: AS_OF,
        alerts_factory=alerts_factory if with_alerts else None,
        worker_factory=worker_factory if worker is not None else None,
    ), store


def envelope(resp, status, code):
    assert resp.status_code == status, resp.text
    assert resp.json()["error"]["code"] == code


# ---- POST /admin/alerts/tick: the demo flip ----

def test_forced_flip_sends_a_breach_flip_to_slack():
    slack = FakeSlack()
    app, store = build(slack)
    with TestClient(app) as c:
        first = c.post(P + "/admin/alerts/tick", json={"coverage_threshold": 3.0})   # p_breach about 0.074
        second = c.post(P + "/admin/alerts/tick", json={"coverage_threshold": 6.0})  # p_breach 0.297: flips
    assert first.status_code == 200 and first.json()["alerts"] == []
    [a] = second.json()["alerts"]
    assert (a["rule"], a["status"], a["company"]) == ("BREACH_FLIP", "sent", "DAL")
    assert len(slack.posts) == 1 and "BREACH_FLIP" in slack.texts[0]


def test_tick_without_a_threshold_uses_config_watch_inputs():
    app, _ = build(FakeSlack())
    with TestClient(app) as c:
        assert c.post(P + "/admin/alerts/tick").json()["alerts"] == []


@pytest.mark.parametrize("body", [{"coverage_threshold": 0}, {"coverage_threshold": 51}, {"other": 1}])
def test_tick_rejects_bad_input(body):
    app, _ = build(FakeSlack())
    with TestClient(app) as c:
        envelope(c.post(P + "/admin/alerts/tick", json=body), 400, "E_VALIDATION")


# ---- GET /alerts ----

def flip(c):
    c.post(P + "/admin/alerts/tick", json={"coverage_threshold": 3.0})
    return c.post(P + "/admin/alerts/tick", json={"coverage_threshold": 6.0}).json()["alerts"][0]


def test_alert_feed_lists_alerts_newest_first():
    app, _ = build(FakeSlack())
    with TestClient(app) as c:
        a = flip(c)
        feed = c.get(P + "/alerts").json()
        assert [x["alert_id"] for x in feed] == [a["alert_id"]]
        assert c.get(P + "/alerts", params={"company": "DAL"}).json()[0]["rule"] == "BREACH_FLIP"
        assert c.get(P + "/alerts", params={"company": "UAL"}).json() == []
        assert c.get(P + "/alerts", params={"since": "2026-10-10T00:00:00Z"}).json() == []


def test_undelivered_alerts_stay_in_the_feed_as_failed():
    app, _ = build(FakeSlack(500, 500, 500))
    with TestClient(app) as c:
        assert flip(c)["status"] == "failed"
        assert c.get(P + "/alerts").json()[0]["status"] == "failed"


@pytest.mark.parametrize("params,code,status", [
    ({"company": "XYZ"}, "E_NOT_FOUND", 404), ({"limit": 0}, "E_VALIDATION", 400),
    ({"since": "2026-10-09T00:00:00"}, "E_VALIDATION", 400),
])
def test_alert_feed_bad_queries(params, code, status):
    app, _ = build(FakeSlack())
    with TestClient(app) as c:
        envelope(c.get(P + "/alerts", params=params), status, code)


# ---- POST /admin/alerts/test ----

def test_test_alert_delivered():
    slack = FakeSlack()
    app, _ = build(slack)
    with TestClient(app) as c:
        resp = c.post(P + "/admin/alerts/test")
    assert resp.status_code == 200 and resp.json() == {"delivered": True}
    assert "test" in slack.texts[0].lower()


def test_test_alert_not_delivered_is_502():
    app, _ = build(FakeSlack(500, 500, 500))
    with TestClient(app) as c:
        envelope(c.post(P + "/admin/alerts/test"), 502, "E_SOURCE_UNAVAILABLE")


def test_alert_endpoints_without_an_alert_engine():
    app, _ = build(FakeSlack(), with_alerts=False)
    with TestClient(app) as c:
        envelope(c.post(P + "/admin/alerts/test"), 502, "E_SOURCE_UNAVAILABLE")
        envelope(c.post(P + "/admin/alerts/tick"), 502, "E_SOURCE_UNAVAILABLE")
        assert c.get(P + "/alerts").json() == []  # the feed still reads the store


# ---- worker hookup ----

def test_the_worker_gets_the_alert_engine_as_after_market_tick():
    calls = []
    app, _ = build(FakeSlack(), worker=FakeWorker(), factory_calls=calls)
    with TestClient(app):
        pass
    assert calls and calls[0] is not None and calls[0].__name__ == "tick"
