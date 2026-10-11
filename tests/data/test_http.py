"""A4: retry, no retry on 4xx, and the circuit breaker. The clock is fake."""

from pathlib import Path

import httpx
import pytest
import respx

from airscen.adapters.http import _BUCKETS, SourceClient
from airscen.types import SourceError

URL = "https://kalshi.test/markets"


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


@pytest.fixture(autouse=True)
def _clear_buckets():
    _BUCKETS.clear()
    yield
    _BUCKETS.clear()


def _client(clock, tmp_path, **kwargs):
    return SourceClient(
        "kalshi",
        "https://kalshi.test",
        kwargs.pop("rate", 100),
        {"User-Agent": "airscen-test"},
        clock=clock,
        sleep=clock.sleep,
        jitter=lambda: 0,
        error_dir=tmp_path,
        **kwargs,
    )


def test_retries_a_500_then_returns_json(tmp_path):
    clock = FakeClock()
    with respx.mock:
        route = respx.get(URL).mock(side_effect=[
            httpx.Response(500, text="nope"),
            httpx.Response(200, json={"markets": []}),
        ])
        body = _client(clock, tmp_path).get_json("/markets", {"limit": 1})
    assert body == {"markets": []}
    assert route.call_count == 2
    assert clock.t == pytest.approx(0.5)


def test_retry_after_replaces_the_backoff(tmp_path):
    clock = FakeClock()
    with respx.mock:
        respx.get(URL).mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "slow"}),
            httpx.Response(200, json={"ok": True}),
        ])
        client = _client(clock, tmp_path, rate=10)
        assert client.get_json("/markets", None) == {"ok": True}
    assert clock.t == pytest.approx(2)
    assert client._bucket.rate == 5


def test_other_4xx_is_not_retried(tmp_path):
    clock = FakeClock()
    with respx.mock:
        route = respx.get(URL).mock(return_value=httpx.Response(404, json={"error": "missing"}))
        with pytest.raises(SourceError) as exc:
            _client(clock, tmp_path).get_json("/markets", None)
    assert exc.value.code == "E_SOURCE_BAD_RESPONSE"
    assert route.call_count == 1
    assert clock.t == 0


def test_malformed_json_is_a_bad_response(tmp_path):
    clock = FakeClock()
    with respx.mock:
        route = respx.get(URL).mock(return_value=httpx.Response(200, content=b"not-json"))
        with pytest.raises(SourceError) as exc:
            _client(clock, tmp_path).get_json("/markets", None)
    assert exc.value.code == "E_SOURCE_BAD_RESPONSE"
    assert route.call_count == 1
    assert list(Path(tmp_path).glob("*.body"))


def test_circuit_opens_and_a_probe_closes_it(tmp_path):
    clock = FakeClock()
    with respx.mock:
        route = respx.get(URL).mock(return_value=httpx.Response(503, text="down"))
        client = _client(clock, tmp_path)
        for _ in range(2):
            with pytest.raises(SourceError):
                client.get_json("/markets", None)
        open_calls = route.call_count
        with pytest.raises(SourceError) as exc:
            client.get_json("/markets", None)
        assert exc.value.code == "E_SOURCE_UNAVAILABLE"
        assert exc.value.details.get("circuit") == "open"
        assert route.call_count == open_calls

        clock.t += 60
        route.mock(return_value=httpx.Response(200, json={"ok": True}))
        assert client.get_json("/markets", None) == {"ok": True}
        assert client.get_json("/markets", None) == {"ok": True}
        assert route.call_count == open_calls + 2


def test_a_failed_probe_reopens_the_circuit(tmp_path):
    clock = FakeClock()
    with respx.mock:
        route = respx.get(URL).mock(return_value=httpx.Response(500, text="down"))
        client = _client(clock, tmp_path)
        for _ in range(2):
            with pytest.raises(SourceError):
                client.get_json("/markets", None)
        with pytest.raises(SourceError):
            client.get_json("/markets", None)
        clock.t += 60
        with pytest.raises(SourceError):
            client.get_json("/markets", None)
        after_probe = route.call_count
        with pytest.raises(SourceError) as exc:
            client.get_json("/markets", None)
        assert exc.value.details.get("circuit") == "open"
        assert route.call_count == after_probe


def test_get_text_returns_the_body(tmp_path):
    clock = FakeClock()
    with respx.mock:
        respx.get("https://kalshi.test/filing").mock(return_value=httpx.Response(200, text="<html>8-K</html>"))
        body = _client(clock, tmp_path).get_text("/filing", None)
    assert body == "<html>8-K</html>"


def test_post_json_sends_a_body(tmp_path):
    clock = FakeClock()
    with respx.mock:
        route = respx.post("https://kalshi.test/books").mock(
            return_value=httpx.Response(200, json={"n": 1})
        )
        body = _client(clock, tmp_path).post_json("/books", {"tokens": ["a"]})
    assert body == {"n": 1}
    assert route.call_count == 1
