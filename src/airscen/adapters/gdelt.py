"""GDELT DOC 2.0 artlist headlines.

Callers ignore failures. This host returned HTTP 429 to the demo machine on
2026-10-11 (one request every five seconds), so no article list was recorded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from airscen.adapters.http import SourceClient
from airscen.types import SourceError


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_articles(payload) -> list[dict]:
    """Pull title, url, and seendate from an artlist object. Anything else is empty."""
    if not isinstance(payload, dict):
        return []
    found = []
    for article in payload.get("articles") or []:
        if not isinstance(article, dict):
            continue
        found.append({
            "title": article.get("title") or "",
            "url": article.get("url") or "",
            "seendate": article.get("seendate") or "",
        })
    return found


class Gdelt:
    def __init__(self, client: SourceClient):
        self._client = client

    def headlines(self, query, before, hours, n):
        end = _as_datetime(before)
        start = end - timedelta(hours=float(hours))
        try:
            payload = self._client.get_json("", {
                "query": query,
                "mode": "artlist",
                "format": "json",
                "maxrecords": str(int(n)),
                "startdatetime": start.strftime("%Y%m%d%H%M%S"),
                "enddatetime": end.strftime("%Y%m%d%H%M%S"),
            })
        except SourceError:
            raise
        return parse_articles(payload)[: int(n)]


_api: Gdelt | None = None


def bind(client: SourceClient) -> Gdelt:
    global _api
    _api = Gdelt(client)
    return _api


def _bound() -> Gdelt:
    if _api is None:
        raise SourceError("GDELT adapter is not configured", code="E_SOURCE_UNAVAILABLE")
    return _api


def headlines(query, before, hours, n):
    return _bound().headlines(query, before, hours, n)
