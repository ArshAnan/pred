"""A4: deterministic HTTP policy checks with an in-process fake server."""

import httpx
import pytest

from airscen.adapters import http as source_http
from airscen.types import SourceError


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(source_http.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(source_http.time, "sleep", fake.sleep)
    monkeypatch.setattr(source_http.random, "uniform", lambda low, high: 1.0)
    source_http.SourceClient._buckets.clear()
    return fake


def source(name, handler, rate=100):
    base_url = f"https://{name}.example.test"
    client = source_http.SourceClient(name, base_url, rate, {"X-Test": "yes"})
    client._client.close()
    client._client = httpx.Client(base_url=base_url, headers={"X-Test": "yes"},
                                  transport=httpx.MockTransport(handler))
    return client


def test_get_retries_server_failure_then_returns_json(clock):
    statuses = [503, 502, 200]
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers["X-Test"]))
        status = statuses.pop(0)
        return httpx.Response(status, json={"ok": True} if status == 200 else {})

    client = source("retry", handler)
    assert client.get_json("/markets", {"limit": 1}) == {"ok": True}
    assert seen == [("GET", "/markets", "yes")] * 3
    assert clock.sleeps == [0.5, 1.0]


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
def test_get_retries_connection_and_read_timeouts(clock, failure):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure("temporary", request=request)
        return httpx.Response(200, json={"ok": True})

    client = source("transport-error", handler)
    assert client.get_json("/markets", None) == {"ok": True}
    assert calls == 2
    assert clock.sleeps == [0.5]


def test_other_4xx_is_not_retried_and_post_is_never_retried(clock):
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(404 if request.method == "GET" else 503, json={})

    client = source("no-retry", handler)
    with pytest.raises(SourceError) as error:
        client.get_json("/missing", None)
    assert error.value.code == "E_SOURCE_BAD_RESPONSE"
    with pytest.raises(SourceError):
        client.post_json("/books", {"tokens": ["a"]})
    assert calls == ["GET", "POST"]
    assert clock.sleeps == []


def test_retry_after_wins_and_429_halves_host_rate(clock):
    responses = [httpx.Response(429, headers={"Retry-After": "2"}),
                 httpx.Response(200, json={"ok": True})]
    client = source("rate", lambda request: responses.pop(0), rate=2)
    assert client.get_json("/markets", None) == {"ok": True}
    assert clock.sleeps == [2.0]
    bucket = source_http.SourceClient._buckets["rate.example.test"]
    assert bucket.rate == 1
    clock.now += 61
    responses.append(httpx.Response(200, json={"ok": True}))
    assert client.get_json("/markets", None) == {"ok": True}
    assert bucket.rate == 2


def test_token_bucket_limits_requests_per_host_across_clients(clock):
    handler = lambda request: httpx.Response(200, json={"ok": True})
    first = source("shared", handler, rate=1)
    second = source("shared", handler, rate=1)
    first.get_json("/one", None)
    second.get_json("/two", None)
    assert clock.sleeps == [1.0]


def test_breaker_opens_after_five_failed_calls_and_one_probe_closes_it(clock):
    calls = 0
    healthy = False

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200 if healthy else 503, json={"ok": healthy})

    client = source("breaker", handler)
    for _ in range(5):
        with pytest.raises(SourceError):
            client.get_json("/markets", None)
    assert calls == 15  # three read attempts per failed logical call
    with pytest.raises(SourceError) as error:
        client.get_json("/markets", None)
    assert error.value.code == "E_SOURCE_UNAVAILABLE"
    assert calls == 15  # open circuit fails without HTTP
    clock.now += 61
    healthy = True
    assert client.get_json("/markets", None) == {"ok": True}
    assert client.get_json("/markets", None) == {"ok": True}
    assert calls == 17


def test_failed_probe_reopens_breaker_for_another_minute(clock):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client = source("failed-probe", handler)
    for _ in range(5):
        with pytest.raises(SourceError):
            client.get_json("/markets", None)
    clock.now += 61
    with pytest.raises(SourceError):
        client.get_json("/markets", None)  # the only half-open probe
    assert calls == 18
    with pytest.raises(SourceError):
        client.get_json("/markets", None)
    assert calls == 18


def test_timeouts_and_whole_call_deadline(clock):
    observed = []

    def handler(request):
        observed.append(request.extensions["timeout"])
        return httpx.Response(503, headers={"Retry-After": "40"})

    client = source("sec", handler)
    with pytest.raises(SourceError):
        client.get_json("/Archives/filing.json", None)
    assert observed[0]["connect"] == 3
    assert observed[0]["read"] == 20
    assert len(observed) == 1  # retry would exceed the 30-second call deadline
    assert clock.now <= 30
