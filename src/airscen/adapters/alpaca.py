"""Alpaca daily bars for the evaluation sanity check.

`GET /v2/stocks/bars` is the path the data API documents for multi-symbol bars.
On 2026-10-11 an unauthenticated call to that path and to a bogus path both
returned nginx 401, so this checkout could not tell a missing route from a
missing key. No bar body was recorded.
"""

from __future__ import annotations

from datetime import date, datetime

from airscen.adapters.http import SourceClient
from airscen.types import SourceError


def _day(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def parse_bars(payload) -> list[dict]:
    """Return one row per bar. An empty or unexpected payload is an empty list."""
    if not isinstance(payload, dict):
        return []
    bars = payload.get("bars") or {}
    if not isinstance(bars, dict):
        return []
    rows = []
    for symbol, series in bars.items():
        for bar in series or []:
            if not isinstance(bar, dict):
                continue
            rows.append({
                "symbol": symbol,
                "t": bar.get("t"),
                "o": bar.get("o"),
                "h": bar.get("h"),
                "l": bar.get("l"),
                "c": bar.get("c"),
                "v": bar.get("v"),
            })
    return rows


class Alpaca:
    def __init__(self, client: SourceClient, key_id: str = "", secret: str = ""):
        self._client = client
        self._key_id = key_id
        self._secret = secret

    def daily_bars(self, symbols, start, end):
        if not self._key_id or not self._secret:
            raise SourceError(
                "Alpaca credentials are not set",
                code="E_SOURCE_AUTH",
                details={"source": "alpaca"},
            )
        if isinstance(symbols, str):
            symbol_text = symbols
        else:
            symbol_text = ",".join(symbols)
        payload = self._client.get_json("/stocks/bars", {
            "symbols": symbol_text,
            "timeframe": "1Day",
            "start": _day(start),
            "end": _day(end),
            "feed": "iex",
        })
        return parse_bars(payload)


_api: Alpaca | None = None


def bind(client: SourceClient, key_id: str = "", secret: str = "") -> Alpaca:
    global _api
    _api = Alpaca(client, key_id, secret)
    return _api


def _bound() -> Alpaca:
    if _api is None:
        raise SourceError("Alpaca adapter is not configured", code="E_SOURCE_UNAVAILABLE")
    return _api


def daily_bars(symbols, start, end):
    return _bound().daily_bars(symbols, start, end)
