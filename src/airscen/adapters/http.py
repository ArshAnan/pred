"""Shared HTTP policy for vendor adapters (LLD §§7–8)."""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from airscen.types import SourceError


class _Bucket:
    """A small, shared token bucket for one HTTP host."""

    def __init__(self, rate):
        self.base_rate = rate
        self.rate = rate
        self.tokens = max(1.0, rate)
        self.updated = time.monotonic()
        self.slow_until = 0.0
        self.lock = threading.Lock()

    def _refresh(self, now):
        if self.slow_until and now >= self.slow_until:
            self.rate = self.base_rate
            self.slow_until = 0.0
        capacity = max(1.0, self.rate)
        self.tokens = min(capacity, self.tokens + max(0.0, now - self.updated) * self.rate)
        self.updated = now

    def limit(self, rate):
        with self.lock:
            self.base_rate = min(self.base_rate, rate)
            self.rate = min(self.rate, self.base_rate / 2) if self.slow_until else self.base_rate
            self.tokens = min(self.tokens, max(1.0, self.rate))

    def slow_down(self):
        with self.lock:
            now = time.monotonic()
            self._refresh(now)
            self.rate = self.base_rate / 2
            self.tokens = min(self.tokens, max(1.0, self.rate))
            self.slow_until = now + 60

    def acquire(self, deadline):
        while True:
            with self.lock:
                now = time.monotonic()
                self._refresh(now)
                if now >= deadline:
                    return False
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
                wait = (1 - self.tokens) / self.rate
            remaining = deadline - time.monotonic()
            if wait >= remaining:
                return False
            time.sleep(wait)


def _retry_after(header):
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(header)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None


class SourceClient:
    _buckets = {}
    _buckets_lock = threading.Lock()

    def __init__(self, name, base_url, rate_per_s, headers):
        if rate_per_s <= 0:
            raise ValueError("rate_per_s must be positive")
        self.name = name
        self.base_url = base_url
        self._host = urlsplit(base_url).netloc.lower()
        self._client = httpx.Client(base_url=base_url, headers=headers, follow_redirects=True)
        with self._buckets_lock:
            bucket = self._buckets.get(self._host)
            if bucket is None:
                bucket = self._buckets[self._host] = _Bucket(rate_per_s)
            else:
                bucket.limit(rate_per_s)
        self._bucket = bucket
        self._breaker_lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._probing = False

    def _check_breaker(self):
        with self._breaker_lock:
            now = time.monotonic()
            if self._probing or now < self._open_until:
                raise SourceError(f"{self.name} circuit is open", code="E_SOURCE_UNAVAILABLE")
            if self._open_until:
                self._probing = True

    def _success(self):
        with self._breaker_lock:
            self._failures = 0
            self._open_until = 0.0
            self._probing = False

    def _failure(self):
        with self._breaker_lock:
            self._failures += 1
            if self._probing or self._failures >= 5:
                self._open_until = time.monotonic() + 60
            self._probing = False

    def _request_json(self, method, path, *, params=None, body=None):
        self._check_breaker()
        deadline = time.monotonic() + 30
        attempts = 3 if method == "GET" else 1
        read_timeout = 20 if "archives" in path.lower() and self.name.lower() in {"sec", "edgar"} else 10
        last_error = None
        for attempt in range(attempts):
            if not self._bucket.acquire(deadline):
                last_error = SourceError(f"{self.name} call exceeded 30 seconds")
                break
            remaining = deadline - time.monotonic()
            timeout = httpx.Timeout(connect=min(3, remaining), read=min(read_timeout, remaining),
                                    write=min(10, remaining), pool=min(3, remaining))
            retry_after = None
            retryable = False
            try:
                response = self._client.request(method, path, params=params, json=body,
                                                timeout=timeout)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
                last_error = SourceError(f"{self.name} connection or read timed out",
                                         details={"error_type": type(exc).__name__})
                retryable = True
            except httpx.RequestError as exc:
                last_error = SourceError(f"{self.name} request failed",
                                         details={"error_type": type(exc).__name__})
            else:
                retry_after = _retry_after(response.headers.get("Retry-After"))
                if time.monotonic() >= deadline:
                    last_error = SourceError(f"{self.name} call exceeded 30 seconds")
                elif response.status_code == 429:
                    self._bucket.slow_down()
                    last_error = SourceError(f"{self.name} rate limited",
                                             code="E_SOURCE_RATE_LIMITED", details={"status": 429})
                    retryable = True
                elif response.status_code >= 500:
                    last_error = SourceError(f"{self.name} server returned {response.status_code}",
                                             details={"status": response.status_code})
                    retryable = True
                elif response.status_code >= 400:
                    self._success()  # a caller-side 4xx does not indicate a broken source
                    code = "E_SOURCE_AUTH" if response.status_code in (401, 403) else "E_SOURCE_BAD_RESPONSE"
                    raise SourceError(f"{self.name} returned {response.status_code}",
                                      code=code, details={"status": response.status_code})
                else:
                    try:
                        result = response.json()
                    except ValueError as exc:
                        last_error = SourceError(f"{self.name} returned invalid JSON",
                                                 code="E_SOURCE_BAD_RESPONSE")
                    else:
                        self._success()
                        return result
            if not retryable or attempt == attempts - 1:
                break
            delay = retry_after if retry_after is not None else min(8, (0.5 * 2**attempt) * random.uniform(0.8, 1.2))
            if delay >= deadline - time.monotonic():
                break
            time.sleep(delay)
        self._failure()
        raise last_error

    def get_json(self, path, params):
        return self._request_json("GET", path, params=params)

    def post_json(self, path, body):
        return self._request_json("POST", path, body=body)
