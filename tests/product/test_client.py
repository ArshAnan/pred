"""C3: ApiClient against a fake API server (respx). LLD section 7, "Streamlit client rules"."""

import httpx
import pytest
import respx

from airscen.ui.client import ApiClient, ApiError

BASE = "http://127.0.0.1:8000/api/v1"


def envelope(code: str, message: str = "msg", request_id: str = "req-1", details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}, "request_id": request_id}}


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(sleeps, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    return ApiClient(BASE, sleep=sleeps.append)


@pytest.fixture
def api():
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


# ---- success ----

def test_get_returns_json_and_sends_params(client, api):
    route = api.get("/runs").respond(200, json=[{"run_id": "run_1"}])
    assert client.get("/runs", {"ticker": "DAL", "limit": 5}) == [{"run_id": "run_1"}]
    assert route.calls.last.request.url.params["ticker"] == "DAL"


def test_post_sends_json_body_and_idempotency_key(client, api):
    route = api.post("/runs").respond(201, json={"run_id": "run_1"})
    assert client.post("/runs", {"ticker": "DAL"}, key="key-123") == {"run_id": "run_1"}
    req = route.calls.last.request
    assert req.headers["Idempotency-Key"] == "key-123"
    assert req.headers["Content-Type"] == "application/json"
    assert b'"ticker"' in req.content


def test_post_without_key_sends_no_idempotency_header(client, api):
    route = api.post("/admin/alerts/test").respond(200, json={"delivered": True})
    client.post("/admin/alerts/test", None)
    assert "Idempotency-Key" not in route.calls.last.request.headers


# ---- timeouts ----

def timeout_of(route) -> dict:
    return route.calls.last.request.extensions["timeout"]


def test_get_timeouts_connect_2s_read_10s(client, api):
    route = api.get("/health").respond(200, json={})
    client.get("/health")
    assert timeout_of(route)["connect"] == 2 and timeout_of(route)["read"] == 10


def test_post_runs_read_timeout_is_30s(client, api):
    route = api.post("/runs").respond(201, json={})
    client.post("/runs", {}, key="k")
    assert timeout_of(route)["connect"] == 2 and timeout_of(route)["read"] == 30


def test_other_posts_keep_the_10s_read_timeout(client, api):
    route = api.post("/admin/ingest").respond(202, json={"job_id": "j"})
    client.post("/admin/ingest", {"sources": ["markets"]})
    assert timeout_of(route)["read"] == 10


# ---- GET retries ----

@pytest.mark.parametrize("status", [502, 503])
def test_get_retries_twice_on_502_and_503_then_succeeds(client, api, sleeps, status):
    route = api.get("/health").mock(side_effect=[
        httpx.Response(status, json=envelope("E_STORE_UNAVAILABLE")),
        httpx.Response(status, json=envelope("E_STORE_UNAVAILABLE")),
        httpx.Response(200, json={"status": "ok"}),
    ])
    assert client.get("/health") == {"status": "ok"}
    assert route.call_count == 3
    assert sleeps == [0.5, 1.0]


def test_get_gives_up_after_three_attempts(client, api, sleeps):
    route = api.get("/health").respond(503, json=envelope("E_STORE_UNAVAILABLE", "db locked", "req-9"))
    with pytest.raises(ApiError) as exc:
        client.get("/health")
    assert route.call_count == 3 and sleeps == [0.5, 1.0]
    assert (exc.value.code, exc.value.status, exc.value.request_id) == ("E_STORE_UNAVAILABLE", 503, "req-9")


def test_get_retries_connection_errors_then_reports_the_base_url(client, api, sleeps):
    route = api.get("/health").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ApiError) as exc:
        client.get("/health")
    assert route.call_count == 3 and sleeps == [0.5, 1.0]
    assert exc.value.code == "E_UNREACHABLE" and exc.value.status == 0
    assert BASE in exc.value.message


@pytest.mark.parametrize("status,code", [(400, "E_VALIDATION"), (404, "E_NOT_FOUND"), (422, "E_MISSING_FACT"), (500, "E_INTERNAL")])
def test_get_does_not_retry_other_errors(client, api, sleeps, status, code):
    route = api.get("/runs/run_x").respond(status, json=envelope(code))
    with pytest.raises(ApiError) as exc:
        client.get("/runs/run_x")
    assert route.call_count == 1 and sleeps == []
    assert exc.value.code == code and exc.value.status == status


# ---- POST never retries ----

def test_post_does_not_retry_on_503(client, api, sleeps):
    route = api.post("/runs").respond(503, json=envelope("E_STORE_UNAVAILABLE"))
    with pytest.raises(ApiError):
        client.post("/runs", {}, key="k")
    assert route.call_count == 1 and sleeps == []


def test_post_does_not_retry_connection_errors(client, api, sleeps):
    route = api.post("/runs").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ApiError) as exc:
        client.post("/runs", {}, key="k")
    assert route.call_count == 1 and sleeps == []
    assert exc.value.code == "E_UNREACHABLE"


# ---- error details ----

def test_api_error_carries_the_envelope(client, api):
    api.post("/runs").respond(422, json=envelope(
        "E_MISSING_FACT", "Required facts are missing for DAL: variable_debt_share", "req-42",
        {"missing": ["variable_debt_share"]},
    ))
    with pytest.raises(ApiError) as exc:
        client.post("/runs", {}, key="k")
    e = exc.value
    assert (e.code, e.status, e.request_id) == ("E_MISSING_FACT", 422, "req-42")
    assert "variable_debt_share" in e.message and e.details == {"missing": ["variable_debt_share"]}
    assert str(e) == e.message


def test_non_json_error_body_still_gives_an_api_error(client, api):
    api.get("/health").respond(500, text="Internal Server Error", headers={"X-Request-ID": "req-hdr"})
    with pytest.raises(ApiError) as exc:
        client.get("/health")
    assert (exc.value.code, exc.value.status, exc.value.request_id) == ("E_INTERNAL", 500, "req-hdr")


# ---- auth and config ----

def test_sends_api_token_when_set(sleeps, api, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    route = api.get("/health").respond(200, json={})
    ApiClient(BASE, token="s3cret", sleep=sleeps.append).get("/health")
    assert route.calls.last.request.headers["X-API-Token"] == "s3cret"


def test_no_token_header_by_default(client, api):
    route = api.get("/health").respond(200, json={})
    client.get("/health")
    assert "X-API-Token" not in route.calls.last.request.headers


def test_base_url_defaults_to_env_then_lld_default(monkeypatch):
    monkeypatch.delenv("API_BASE_URL", raising=False)
    monkeypatch.delenv("API_TOKEN", raising=False)
    assert ApiClient().base_url == BASE
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9000/api/v1")
    monkeypatch.setenv("API_TOKEN", "from-env")
    c = ApiClient()
    assert c.base_url == "http://127.0.0.1:9000/api/v1" and c.token == "from-env"
