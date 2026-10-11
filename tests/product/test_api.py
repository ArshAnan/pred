"""C1: API skeleton. LLD section 7 (conventions, error envelope, server rules) and CP0_DECISIONS D3.

Lane A's load_settings() and Repository are not on main yet, so the app is built with
create_app(settings_loader, repo_factory) and these tests pass simple fakes.
"""

import json
import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi import Query
from fastapi.testclient import TestClient

from airscen import api as api_module
from airscen.types import AirScenError, DataError, LLMError, RequestError, SourceError, StoreError

PREFIX = "/api/v1"


def fake_settings(token: str = "", llm_mode: str = "live"):
    # As in config.yaml: DAL's CIK is verified; UAL's is null until lane A resolves it from EDGAR.
    companies = [
        SimpleNamespace(ticker="DAL", cik=27904, name="Delta Air Lines", peers=["UAL", "AAL"]),
        SimpleNamespace(ticker="UAL", cik=None, name="United Airlines Holdings", peers=["DAL", "AAL"]),
    ]
    return SimpleNamespace(api_token=token, llm_mode=llm_mode, companies=companies)


class FakeRepo:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.migrated = 0

    def migrate(self):
        self.migrated += 1
        if self.fail:
            raise self.fail


def make_app(token: str = "", repo: FakeRepo | None = None):
    repo = repo or FakeRepo()
    app = api_module.create_app(settings_loader=lambda: fake_settings(token), repo_factory=lambda s: repo)

    # Test-only routes to exercise the error handlers; C1 itself has only /health and /companies.
    @app.get(PREFIX + "/_test/raise/{code}")
    def raise_code(code: str):
        classes = {"E_NOT_FOUND": RequestError, "E_VALIDATION": RequestError, "E_UNAUTHORIZED": RequestError,
                   "E_CONFLICT": RequestError, "E_MISSING_FACT": DataError, "E_SOURCE_UNAVAILABLE": SourceError,
                   "E_STORE_UNAVAILABLE": StoreError, "E_LLM_BUDGET": LLMError}
        raise classes.get(code, AirScenError)("boom", code=code, details={"k": "v"})

    @app.get(PREFIX + "/_test/query")
    def query(limit: int = Query(50, ge=1, le=200)):
        return {"limit": limit}

    @app.post(PREFIX + "/_test/echo")
    def echo(body: dict):
        return {"n": len(body)}

    @app.get(PREFIX + "/_test/crash")
    def crash():
        raise RuntimeError("unexpected")

    return app, repo


@pytest.fixture
def client():
    app, _ = make_app()
    with TestClient(app) as c:
        yield c


def assert_envelope(resp, status: int, code: str):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"}
    err = body["error"]
    assert set(err) == {"code", "message", "details", "request_id"}
    assert err["code"] == code
    assert isinstance(err["message"], str) and err["message"]
    assert isinstance(err["details"], dict)
    assert err["request_id"] == resp.headers["X-Request-ID"]
    return err


# ---- app and lifespan ----

def test_module_exposes_an_app_without_starting_it():
    from fastapi import FastAPI
    assert isinstance(api_module.app, FastAPI)


def test_lifespan_loads_settings_and_migrates_once():
    app, repo = make_app()
    with TestClient(app):
        pass
    assert repo.migrated == 1


# ---- /health ----

def test_health_returns_200_with_health_status(client):
    resp = client.get(PREFIX + "/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["db"] == "ok" and body["status"] in {"ok", "degraded"}
    assert body["llm"]["mode"] == "live"
    assert set(body) >= {"status", "db", "sources", "last_snapshot_age_s", "llm", "worker"}


def test_health_is_503_when_the_database_cannot_be_opened():
    app, _ = make_app(repo=FakeRepo(fail=StoreError("database is locked")))
    with TestClient(app) as c:
        assert_envelope(c.get(PREFIX + "/health"), 503, "E_STORE_UNAVAILABLE")


# ---- /companies ----

def test_companies_lists_configured_companies(client):
    resp = client.get(PREFIX + "/companies")
    assert resp.status_code == 200
    assert resp.json()[0] == {"ticker": "DAL", "cik": 27904, "name": "Delta Air Lines", "peers": ["UAL", "AAL"]}
    assert [c["ticker"] for c in resp.json()] == ["DAL", "UAL"]


def test_companies_lists_a_company_whose_cik_is_not_resolved_yet(client):
    # config.yaml ships UAL and AAL with cik: null (resolved from EDGAR later); the list must still load.
    resp = client.get(PREFIX + "/companies")
    assert resp.status_code == 200
    assert resp.json()[1] == {"ticker": "UAL", "cik": None, "name": "United Airlines Holdings", "peers": ["DAL", "AAL"]}


# ---- request id ----

def test_request_id_is_generated(client):
    rid = client.get(PREFIX + "/health").headers["X-Request-ID"]
    assert uuid.UUID(rid).version == 4


def test_request_id_is_echoed(client):
    assert client.get(PREFIX + "/health", headers={"X-Request-ID": "abc-123"}).headers["X-Request-ID"] == "abc-123"


# ---- error envelope ----

@pytest.mark.parametrize("code,status", [
    ("E_VALIDATION", 400), ("E_UNAUTHORIZED", 401), ("E_NOT_FOUND", 404), ("E_CONFLICT", 409),
    ("E_MISSING_FACT", 422), ("E_SOURCE_UNAVAILABLE", 502), ("E_STORE_UNAVAILABLE", 503),
])
def test_airscen_errors_map_to_their_http_status(client, code, status):
    err = assert_envelope(client.get(f"{PREFIX}/_test/raise/{code}"), status, code)
    assert err["message"] == "boom" and err["details"] == {"k": "v"}


@pytest.mark.parametrize("code", ["E_LLM_BUDGET", "E_SOURCE_RATE_LIMITED", "E_CONFIG", "E_SOMETHING_NEW"])
def test_other_codes_become_500_internal_keeping_the_original(client, code):
    err = assert_envelope(client.get(f"{PREFIX}/_test/raise/{code}"), 500, "E_INTERNAL")
    assert err["details"]["original_code"] == code


def test_unknown_route_is_a_404_envelope(client):
    assert_envelope(client.get(PREFIX + "/no/such/thing"), 404, "E_NOT_FOUND")


def test_query_validation_is_a_400_envelope_with_fields(client):
    err = assert_envelope(client.get(PREFIX + "/_test/query", params={"limit": 500}), 400, "E_VALIDATION")
    assert err["details"]["fields"][0]["loc"][-1] == "limit"


def test_bad_json_body_is_a_400_envelope(client):
    resp = client.post(PREFIX + "/_test/echo", content=b"{not json", headers={"Content-Type": "application/json"})
    assert_envelope(resp, 400, "E_VALIDATION")


def test_unexpected_exception_is_a_500_envelope_without_internals(client):
    err = assert_envelope(client.get(PREFIX + "/_test/crash"), 500, "E_INTERNAL")
    assert "unexpected" not in err["message"]


# ---- body limit ----

def test_body_over_64kb_is_rejected(client):
    big = json.dumps({"x": "a" * (64 * 1024)})
    err = assert_envelope(client.post(PREFIX + "/_test/echo", content=big, headers={"Content-Type": "application/json"}),
                          400, "E_VALIDATION")
    assert "64" in err["message"]


def test_body_under_64kb_is_accepted(client):
    assert client.post(PREFIX + "/_test/echo", json={"x": "a" * 1000}).status_code == 200


# ---- API token ----

def test_no_token_needed_when_api_token_unset(client):
    assert client.get(PREFIX + "/companies").status_code == 200


@pytest.mark.parametrize("headers", [{}, {"X-API-Token": "wrong"}])
def test_401_when_api_token_set_and_header_missing_or_wrong(headers):
    app, _ = make_app(token="s3cret")
    with TestClient(app) as c:
        assert_envelope(c.get(PREFIX + "/health", headers=headers), 401, "E_UNAUTHORIZED")


def test_200_when_api_token_matches():
    app, _ = make_app(token="s3cret")
    with TestClient(app) as c:
        assert c.get(PREFIX + "/health", headers={"X-API-Token": "s3cret"}).status_code == 200


# ---- request log ----

def test_one_json_log_line_per_request(client, caplog):
    with caplog.at_level(logging.INFO, logger="airscen.api"):
        client.get(PREFIX + "/companies", headers={"X-Request-ID": "log-1"})
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "airscen.api"]
    assert len(lines) == 1
    line = lines[0]
    assert (line["method"], line["path"], line["status"], line["request_id"]) == ("GET", PREFIX + "/companies", 200, "log-1")
    assert line["latency_ms"] >= 0 and line["component"] == "api" and line["event"] == "request"
