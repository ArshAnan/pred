"""C6: run, facts and market endpoints. LLD section 7 endpoints table and section 9 API contract tests:
every endpoint's status codes, the envelope shape, the idempotency key, a future as_of rejected.

Backed by the real RunService on the TEMPORARY FakeStore seeded with the ILLUSTRATIVE worked example.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from airscen import api as api_module
from airscen.config import Settings
from airscen.types import StoreError
from fake_store import AS_OF, FakeStore, seed_worked_example

P = "/api/v1"
BODY = {"ticker": "DAL", "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150,
        "coverage_threshold": 6.0, "fuel_passthrough": 1.0}


class Clock:
    """Fixed wall clock (AS_OF) for the service and a movable monotonic clock for idempotency."""

    def __init__(self):
        self.mono = 1000.0

    def now(self):
        return AS_OF

    def monotonic(self):
        return self.mono


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store():
    s = FakeStore()
    seed_worked_example(s)
    return s


def build(store, clock, repo_error=None):
    def repo_factory(settings):
        if repo_error:
            raise repo_error
        return store
    return api_module.create_app(settings_loader=Settings, repo_factory=repo_factory,
                                 clock=clock.now, monotonic=clock.monotonic)


@pytest.fixture
def client(store, clock):
    with TestClient(build(store, clock)) as c:
        yield c


def iso(t):
    return t.isoformat().replace("+00:00", "Z")


def assert_envelope(resp, status, code):
    assert resp.status_code == status, resp.text
    err = resp.json()["error"]
    assert set(err) == {"code", "message", "details", "request_id"} and err["code"] == code
    assert err["request_id"] == resp.headers["X-Request-ID"]
    return err


# ---- POST /runs ----

def test_post_runs_201_with_the_golden_run(client, store):
    resp = client.post(P + "/runs", json=BODY, headers={"Idempotency-Key": "k-1"})
    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert run["schema_version"] == 1 and run["company"] == "DAL"
    assert run["as_of"] == "2026-10-09T14:30:00Z"
    assert run["summary"]["p_breach"] == pytest.approx(0.297, abs=0.0005)
    assert run["summary"]["breakeven_wti_hold"] == pytest.approx(103.16, abs=0.005)
    assert len(run["rows"]) == 6 and run["brief"]["template_md"]
    assert run["run_id"] in store.saved


def test_same_idempotency_key_within_10_minutes_returns_the_original_with_200(client, store, clock):
    first = client.post(P + "/runs", json=BODY, headers={"Idempotency-Key": "k-1"})
    clock.mono += 599
    again = client.post(P + "/runs", json=BODY, headers={"Idempotency-Key": "k-1"})
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert len(store.saved) == 1


def test_idempotency_key_expires_after_10_minutes(client, store, clock):
    first = client.post(P + "/runs", json=BODY, headers={"Idempotency-Key": "k-1"})
    clock.mono += 601
    later = client.post(P + "/runs", json=BODY, headers={"Idempotency-Key": "k-1"})
    assert later.status_code == 201 and later.json()["run_id"] != first.json()["run_id"]


def test_different_or_missing_keys_create_new_runs(client, store):
    a = client.post(P + "/runs", json=BODY, headers={"Idempotency-Key": "k-1"})
    b = client.post(P + "/runs", json=BODY, headers={"Idempotency-Key": "k-2"})
    c = client.post(P + "/runs", json=BODY)
    assert {a.status_code, b.status_code, c.status_code} == {201}
    assert len(store.saved) == 3


def test_a_failed_request_does_not_burn_the_key(store, clock):
    store.facts = [f for f in store.facts if f.metric != "variable_debt_share"]
    with TestClient(build(store, clock)) as c:
        assert_envelope(c.post(P + "/runs", json=BODY, headers={"Idempotency-Key": "k-1"}), 422, "E_MISSING_FACT")
        seed_worked_example(store)  # the missing fact arrives; the retry with the same key now succeeds
        assert c.post(P + "/runs", json=BODY, headers={"Idempotency-Key": "k-1"}).status_code == 201


def test_future_as_of_is_400(client, store):
    from datetime import datetime, timezone
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    err = assert_envelope(client.post(P + "/runs", json=BODY | {"as_of": iso(future)}), 400, "E_VALIDATION")
    assert "as_of" in str(err["details"]["fields"])
    assert store.saved == {}


@pytest.mark.parametrize("change", [
    {"as_of": "2026-10-09T14:30:00"},       # no timezone
    {"note_size_usd_m": 0},
    {"tenor_years": 41},
    {"fuel_passthrough": 1.5},
    {"unexpected": True},                   # unknown fields rejected
])
def test_invalid_run_requests_are_400(client, change):
    assert_envelope(client.post(P + "/runs", json=BODY | change), 400, "E_VALIDATION")


def test_missing_fact_is_422_naming_it(store, clock):
    store.facts = [f for f in store.facts if f.metric != "variable_debt_share"]
    with TestClient(build(store, clock)) as c:
        err = assert_envelope(c.post(P + "/runs", json=BODY), 422, "E_MISSING_FACT")
    assert err["details"]["missing"] == ["variable_debt_share"]


def test_unknown_ticker_is_404(client):
    assert_envelope(client.post(P + "/runs", json=BODY | {"ticker": "XYZ"}), 404, "E_NOT_FOUND")


def test_runs_are_503_when_the_store_is_down(store, clock):
    with TestClient(build(store, clock, repo_error=StoreError("database is locked"))) as c:
        assert_envelope(c.post(P + "/runs", json=BODY), 503, "E_STORE_UNAVAILABLE")
        assert_envelope(c.get(P + "/runs"), 503, "E_STORE_UNAVAILABLE")


# ---- GET /runs/{id}, GET /runs ----

def test_get_run_by_id(client):
    created = client.post(P + "/runs", json=BODY).json()
    resp = client.get(f"{P}/runs/{created['run_id']}")
    assert resp.status_code == 200 and resp.json() == created


def test_get_unknown_run_is_404(client):
    assert_envelope(client.get(P + "/runs/run_nope"), 404, "E_NOT_FOUND")


def test_list_runs_returns_summaries_newest_first(client, store):
    ids = [client.post(P + "/runs", json=BODY).json()["run_id"] for _ in range(3)]
    for i, run_id in enumerate(ids):  # make created_at distinct
        store.saved[run_id] = store.saved[run_id].model_copy(update={"created_at": AS_OF + timedelta(seconds=i)})
    resp = client.get(P + "/runs", params={"ticker": "DAL", "limit": 2})
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["run_id"] for r in rows] == [ids[2], ids[1]]
    assert set(rows[0]) == {"run_id", "company", "as_of", "created_at", "p_breach", "flags", "llm_status"}
    assert rows[0]["llm_status"] == "skipped" and rows[0]["p_breach"] == pytest.approx(0.297, abs=0.0005)
    assert client.get(P + "/runs", params={"ticker": "UAL"}).json() == []


@pytest.mark.parametrize("limit", [0, 201, "x"])
def test_list_runs_limit_out_of_range_is_400(client, limit):
    assert_envelope(client.get(P + "/runs", params={"limit": limit}), 400, "E_VALIDATION")


# ---- GET /companies/{ticker}/facts ----

def test_facts_for_a_company(client):
    resp = client.get(P + "/companies/DAL/facts", params={"as_of": iso(AS_OF)})
    assert resp.status_code == 200
    facts = {f["metric"]: f for f in resp.json()}
    assert facts["operating_income"]["value"] == 1454 and facts["variable_debt_share"]["value"] == 0.22
    assert len(facts) == 7


def test_facts_respect_as_of(client):
    resp = client.get(P + "/companies/DAL/facts", params={"as_of": iso(AS_OF - timedelta(days=1))})
    assert [f["metric"] for f in resp.json()] == ["variable_debt_share"]  # the October 9 8-K is not visible yet


def test_facts_default_as_of_is_now(client):
    assert len(client.get(P + "/companies/DAL/facts").json()) == 7


def test_facts_unknown_company_is_404(client):
    assert_envelope(client.get(P + "/companies/XYZ/facts"), 404, "E_NOT_FOUND")


@pytest.mark.parametrize("path", ["/companies/DAL/facts", "/markets", "/markets/kalshi/WTI-ILLUSTRATIVE-80/history"])
def test_reads_reject_a_future_as_of(client, path):
    assert_envelope(client.get(P + path, params={"as_of": iso(AS_OF + timedelta(minutes=5))}), 400, "E_VALIDATION")


# ---- GET /markets ----

def test_markets_with_latest_quote_and_trust(client):
    resp = client.get(P + "/markets")
    assert resp.status_code == 200
    views = resp.json()
    assert len(views) == 8
    v = next(x for x in views if x["meta"]["market_id"] == "WTI-ILLUSTRATIVE-90")
    assert v["label"]["factor"] == "fuel" and v["stale"] is False
    assert v["latest"]["bid"] == pytest.approx(0.615) and v["trust"]["mid"] == pytest.approx(0.62)
    assert v["trust"]["score"] >= 0.4 and v["trust"]["excluded_reason"] is None


def test_markets_filter_by_factor_and_min_trust(client):
    assert len(client.get(P + "/markets", params={"factor": "fuel"}).json()) == 5
    assert len(client.get(P + "/markets", params={"factor": "rates"}).json()) == 3
    assert client.get(P + "/markets", params={"min_trust": 0.99}).json() == []


def test_markets_mark_stale_quotes(store, clock):
    store.snapshots = [s.model_copy(update={"ts": AS_OF - timedelta(minutes=10)}) for s in store.snapshots]
    with TestClient(build(store, clock)) as c:
        views = c.get(P + "/markets").json()
    assert all(v["stale"] for v in views)


@pytest.mark.parametrize("params", [{"factor": "weather"}, {"min_trust": 1.5}, {"min_trust": -0.1}])
def test_markets_bad_query_is_400(client, params):
    assert_envelope(client.get(P + "/markets", params=params), 400, "E_VALIDATION")


# ---- GET /markets/{venue}/{market_id}/history ----

def test_history_returns_snapshots_in_range(client):
    resp = client.get(P + "/markets/kalshi/WTI-ILLUSTRATIVE-80/history",
                      params={"from": iso(AS_OF - timedelta(hours=1)), "to": iso(AS_OF)})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1 and rows[0]["market_id"] == "WTI-ILLUSTRATIVE-80" and rows[0]["bid"] == pytest.approx(0.895)


def test_history_hides_snapshots_after_as_of(client):
    resp = client.get(P + "/markets/kalshi/WTI-ILLUSTRATIVE-80/history",
                      params={"as_of": iso(AS_OF - timedelta(minutes=5))})
    assert resp.json() == []


def test_history_unknown_market_is_404(client):
    assert_envelope(client.get(P + "/markets/kalshi/NOPE/history"), 404, "E_NOT_FOUND")


@pytest.mark.parametrize("path,params", [
    ("/markets/betfair/WTI-ILLUSTRATIVE-80/history", {}),                                  # unknown venue
    ("/markets/kalshi/WTI-ILLUSTRATIVE-80/history", {"from": iso(AS_OF), "to": iso(AS_OF - timedelta(hours=1))}),
])
def test_history_bad_request_is_400(client, path, params):
    assert_envelope(client.get(P + path, params=params), 400, "E_VALIDATION")
