"""Typed HTTP client for Streamlit (LLD section 7, "Streamlit client rules").

Imports only the standard library and httpx: the UI never touches the store or the engine (DD-1).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000/api/v1"
CONNECT_S = 2.0
READ_S = 10.0
READ_RUNS_POST_S = 30.0       # POST /runs only
GET_RETRY_WAITS_S = (0.5, 1.0)  # two retries after the first attempt
RETRY_STATUSES = {502, 503}
UNREACHABLE = "E_UNREACHABLE"  # client-side only: the API never sends this code


class ApiError(Exception):
    """Error banner data from a non-2xx API response, or an unreachable API (status 0)."""

    def __init__(self, code: str, message: str, request_id: str, status: int, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id
        self.status = status
        self.details = details or {}


class ApiClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = (base_url or os.environ.get("API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.token = token if token is not None else os.environ.get("API_TOKEN") or None
        self._sleep = sleep
        headers = {"X-API-Token": self.token} if self.token else {}
        self._http = httpx.Client(base_url=self.base_url, headers=headers)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET with up to two retries on connection errors, 502 and 503."""
        timeout = httpx.Timeout(READ_S, connect=CONNECT_S)
        waits = iter(GET_RETRY_WAITS_S)
        while True:
            try:
                resp = self._http.get(path, params=params, timeout=timeout)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                wait = next(waits, None)
                if wait is None:
                    raise self._unreachable(e) from e
                self._sleep(wait)
                continue
            if resp.status_code in RETRY_STATUSES:
                wait = next(waits, None)
                if wait is not None:
                    self._sleep(wait)
                    continue
            return self._result(resp)

    def post(self, path: str, body: Any, key: str | None = None) -> Any:
        """POST once; never retried automatically. `key` is the Idempotency-Key header."""
        read = READ_RUNS_POST_S if path.rstrip("/") == "/runs" else READ_S
        headers = {"Idempotency-Key": key} if key else {}
        try:
            resp = self._http.post(path, json=body, headers=headers, timeout=httpx.Timeout(read, connect=CONNECT_S))
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise self._unreachable(e) from e
        return self._result(resp)

    def close(self) -> None:
        self._http.close()

    def _unreachable(self, e: Exception) -> ApiError:
        return ApiError(UNREACHABLE, f"Cannot reach the API at {self.base_url} ({e})", "", 0)

    @staticmethod
    def _result(resp: httpx.Response) -> Any:
        if resp.is_success:
            return resp.json() if resp.content else None
        request_id = resp.headers.get("X-Request-ID", "")
        try:
            err = resp.json()["error"]
            error = ApiError(
                err["code"], err["message"], err.get("request_id") or request_id, resp.status_code, err.get("details"),
            )
        except (ValueError, KeyError, TypeError):  # not our envelope, e.g. a proxy's HTML page
            error = ApiError("E_INTERNAL", resp.text or resp.reason_phrase, request_id, resp.status_code)
        raise error
