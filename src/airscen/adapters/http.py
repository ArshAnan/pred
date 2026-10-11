"""Shared HTTP client: timeouts, retry, a per-host token bucket, and a circuit breaker.

LLD section 8. Tests pass a clock and a sleep function so they do not wait on the wall clock.
"""

from __future__ import annotations

import random
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from airscen.types import SourceError

_BUCKETS: dict[str, "TokenBucket"] = {}
_BUCKET_LOCK = threading.Lock()
_BACKOFF_S = (0.5, 1.0)


class TokenBucket:
    def __init__(self, rate: float, clock, sleep):
        self.base_rate = rate
        self.rate = rate
        self.clock = clock
        self.sleep = sleep
        self.tokens = rate
        self.updated = clock()
        self.half_until: float | None = None

    def halve(self) -> None:
        self.rate = self.base_rate / 2 if self.base_rate else 0
        self.half_until = self.clock() + 60
        self.tokens = min(self.tokens, self.rate)

    def acquire(self) -> None:
        now = self.clock()
        if self.half_until is not None and now >= self.half_until:
            self.rate = self.base_rate
            self.half_until = None
        rate = self.rate
        if rate <= 0:
            return
        self.tokens = min(rate, self.tokens + (now - self.updated) * rate)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return
        wait = (1 - self.tokens) / rate
        self.sleep(wait)
        self.tokens = 0
        self.updated = self.clock()


def _bucket(host: str, rate: float, clock, sleep) -> TokenBucket:
    with _BUCKET_LOCK:
        bucket = _BUCKETS.get(host)
        if bucket is None:
            bucket = TokenBucket(rate, clock, sleep)
            _BUCKETS[host] = bucket
        return bucket


class SourceClient:
    def __init__(
        self,
        name,
        base_url,
        rate_per_s,
        headers,
        *,
        read_timeout: float = 10,
        clock=None,
        sleep=None,
        jitter=None,
        error_dir: Path | None = None,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self._read_timeout = read_timeout
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._jitter = jitter or (lambda: random.uniform(-0.2, 0.2))
        self._error_dir = Path(error_dir) if error_dir is not None else Path("data/raw/errors")
        self._host = urlparse(self.base_url).netloc or self.name
        self._bucket = _bucket(self._host, float(rate_per_s), self._clock, self._sleep)
        self._fail_streak = 0
        self._open_until: float | None = None
        self._probing = False
        timeout = httpx.Timeout(30.0, connect=3.0, read=read_timeout, write=10.0, pool=3.0)
        self._http = httpx.Client(base_url=self.base_url, headers=self.headers, timeout=timeout)

    def get_json(self, path, params):
        return self._request("GET", path, params=params)

    def post_json(self, path, body):
        return self._request("POST", path, json=body)

    def get_text(self, path, params=None) -> str:
        """Same retry policy as get_json, for HTML filings that are not JSON."""
        return self._request("GET", path, params=params, decode="text")

    def _request(self, method: str, path: str, params=None, json=None, decode: str = "json"):
        started = self._clock()
        for attempt in range(3):
            self._ensure_circuit_closed()
            self._raise_if_deadline(started)
            self._bucket.acquire()
            try:
                response = self._http.request(method, path, params=params, json=json)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self._record_failure()
                if attempt == 2 or self._circuit_open():
                    raise SourceError(
                        f"{self.name} request failed: {exc}",
                        code="E_SOURCE_UNAVAILABLE",
                        details={"source": self.name},
                    ) from exc
                self._pause(attempt, None, started)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if response.status_code == 429:
                    self._bucket.halve()
                self._record_failure()
                if attempt == 2 or self._circuit_open():
                    code = "E_SOURCE_RATE_LIMITED" if response.status_code == 429 else "E_SOURCE_UNAVAILABLE"
                    self._save_error(response)
                    raise SourceError(
                        f"{self.name} returned HTTP {response.status_code}",
                        code=code,
                        details={"source": self.name, "status": response.status_code},
                    )
                self._pause(attempt, response, started)
                continue
            if response.status_code in (401, 403):
                self._save_error(response)
                raise SourceError(
                    f"{self.name} rejected the credentials",
                    code="E_SOURCE_AUTH",
                    details={"source": self.name, "status": response.status_code},
                )
            if response.status_code >= 400:
                self._save_error(response)
                raise SourceError(
                    f"{self.name} returned HTTP {response.status_code}",
                    code="E_SOURCE_BAD_RESPONSE",
                    details={"source": self.name, "status": response.status_code},
                )
            self._record_success()
            if decode == "text":
                return response.text
            return self._decode(response)
        raise SourceError(f"{self.name} request failed", code="E_SOURCE_UNAVAILABLE", details={"source": self.name})

    def _decode(self, response: httpx.Response):
        try:
            return response.json()
        except ValueError as exc:
            self._save_error(response)
            raise SourceError(
                f"{self.name} returned malformed JSON",
                code="E_SOURCE_BAD_RESPONSE",
                details={"source": self.name},
            ) from exc

    def _pause(self, attempt: int, response: httpx.Response | None, started: float) -> None:
        wait = self._backoff(attempt, response)
        remaining = 30 - (self._clock() - started)
        if wait > remaining:
            raise SourceError(
                f"{self.name} exceeded the 30s deadline",
                code="E_SOURCE_UNAVAILABLE",
                details={"source": self.name},
            )
        if wait > 0:
            self._sleep(wait)

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            header = response.headers.get("Retry-After")
            if header:
                try:
                    return max(0.0, float(header))
                except ValueError:
                    pass
        base = _BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)]
        return min(8.0, max(0.0, base * (1 + self._jitter())))

    def _raise_if_deadline(self, started: float) -> None:
        if self._clock() - started > 30:
            raise SourceError(
                f"{self.name} exceeded the 30s deadline",
                code="E_SOURCE_UNAVAILABLE",
                details={"source": self.name},
            )

    def _ensure_circuit_closed(self) -> None:
        now = self._clock()
        if self._open_until is not None and now < self._open_until:
            raise SourceError(
                f"{self.name} circuit is open",
                code="E_SOURCE_UNAVAILABLE",
                details={"source": self.name, "circuit": "open"},
            )
        self._probing = self._open_until is not None

    def _circuit_open(self) -> bool:
        return self._open_until is not None and self._clock() < self._open_until

    def _record_success(self) -> None:
        self._fail_streak = 0
        self._open_until = None
        self._probing = False

    def _record_failure(self) -> None:
        if self._probing:
            self._open_until = self._clock() + 60
            self._fail_streak = 0
            self._probing = False
            return
        self._fail_streak += 1
        if self._fail_streak >= 5:
            self._open_until = self._clock() + 60
            self._fail_streak = 0

    def _save_error(self, response: httpx.Response) -> None:
        try:
            self._error_dir.mkdir(parents=True, exist_ok=True)
            stamp = str(self._clock()).replace(".", "_")
            path = self._error_dir / f"{self.name}-{response.status_code}-{stamp}.body"
            path.write_bytes(response.content)
        except OSError:
            return
