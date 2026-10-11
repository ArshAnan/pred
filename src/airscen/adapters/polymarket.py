"""Polymarket Gamma, CLOB, and Data API payloads to domain types.

`outcomePrices` and `clobTokenIds` may be JSON strings. V2 markets identify
the token with `positionIds`. A last trade of 0.5 with an empty side is ignored.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from airscen.adapters.http import SourceClient
from airscen.types import MarketKind, MarketMeta, Snapshot, SourceError, Venue

_BATCH = 500


def _loads(value):
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") or text.startswith("{"):
            return json.loads(text)
    return value


def _prob(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None
    if not 0 <= parsed <= 1:
        raise SourceError(
            f"Polymarket price {value} is outside [0, 1]",
            code="E_SOURCE_BAD_RESPONSE",
            details={"source": "polymarket"},
        )
    return parsed


def _size(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _epoch(value) -> datetime:
    number = float(value)
    if number > 10_000_000_000:  # milliseconds, as on the CLOB book timestamp
        number /= 1000
    return datetime.fromtimestamp(number, tz=timezone.utc)


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def token_id(market: dict) -> str | None:
    version = str(market.get("version") or "")
    tokens = _loads(market.get("clobTokenIds")) or []
    positions = market.get("positionIds") or []
    if version.startswith("v2") or not tokens:
        if positions:
            return str(positions[0])
    if tokens:
        return str(tokens[0])
    return None


def _outcome(market: dict) -> str | None:
    text = f"{market.get('question') or ''} {market.get('groupItemTitle') or ''}".lower()
    if "fed" not in text and "federal reserve" not in text and "interest rate" not in text:
        return None
    if any(word in text for word in ("cut", "decrease", "lower")):
        return "cut"
    if any(word in text for word in ("hike", "increase", "raise")):
        return "hike"
    if any(word in text for word in ("hold", "unchanged", "maintain")):
        return "hold"
    return None


def market_meta(market: dict) -> MarketMeta:
    outcome = _outcome(market)
    kind = MarketKind.PER_MEETING if outcome else MarketKind.OTHER
    market_id = str(market.get("conditionId") or market.get("id"))
    return MarketMeta(
        venue=Venue.POLYMARKET,
        market_id=market_id,
        token_id=token_id(market),
        title=market.get("question") or market.get("title") or "",
        rules=market.get("description") or "",
        close_time=_dt(market.get("endDate")),
        kind=kind,
        outcome=outcome,
        group_key=market.get("slug") or market.get("conditionId"),
    )


def last_trade(payload: dict) -> float | None:
    """Ignore the placeholder 0.5 that a never-traded token returns with an empty side."""
    price = _prob(payload.get("price"))
    side = payload.get("side")
    if price == 0.5 and not side:
        return None
    return price


def _best(levels: list[dict], pick) -> tuple[float | None, float | None]:
    parsed = []
    for level in levels or []:
        price = _prob(level.get("price"))
        if price is None:
            continue
        parsed.append((price, _size(level.get("size"))))
    if not parsed:
        return None, None
    price, size = pick(parsed)
    return price, size


def snapshot_from_book(book: dict, src: str = "live") -> Snapshot:
    bid, bid_size = _best(book.get("bids") or [], max)
    ask, ask_size = _best(book.get("asks") or [], min)
    last = book.get("last_trade_price")
    if isinstance(last, dict):
        last_price = last_trade(last)
    else:
        last_price = None if last in (None, "") else _prob(last)
    return Snapshot(
        venue=Venue.POLYMARKET,
        market_id=str(book.get("market") or book.get("asset_id")),
        ts=_epoch(book["timestamp"]) if book.get("timestamp") else datetime.now(timezone.utc),
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        last=last_price,
        src=src,
    )


def snapshot_from_history(token_id: str, point: dict, src: str = "live") -> Snapshot:
    return Snapshot(
        venue=Venue.POLYMARKET,
        market_id=token_id,
        ts=_epoch(point["timestamp"]),
        last=_prob(point.get("price")),
        src=src,
    )


class Polymarket:
    def __init__(self, gamma: SourceClient, clob: SourceClient, data: SourceClient):
        self._gamma = gamma
        self._clob = clob
        self._data = data

    def search(self, query):
        payload = self._gamma.get_json("/public-search", {"q": query})
        markets = []
        if isinstance(payload, list):
            markets = payload
        elif isinstance(payload, dict):
            for event in payload.get("events") or []:
                markets.extend(event.get("markets") or [])
            markets.extend(payload.get("markets") or [])
        return [market_meta(market) for market in markets]

    def quotes(self, token_ids):
        found = []
        ids = [str(token) for token in token_ids]
        for start in range(0, len(ids), _BATCH):
            batch = ids[start : start + _BATCH]
            payload = self._clob.post_json("/books", [{"token_id": token} for token in batch])
            books = payload if isinstance(payload, list) else [payload]
            found.extend(snapshot_from_book(book) for book in books)
        return found

    def history(self, token_id, start, end, bucket_s):
        payload = self._data.get_json(
            "/v2/prices-history",
            {
                "token_id": token_id,
                "start": int(start.timestamp()),
                "end": int(end.timestamp()),
                "bucket_seconds": int(bucket_s),
            },
        )
        return [snapshot_from_history(str(token_id), point) for point in payload.get("data") or []]


_api: Polymarket | None = None


def bind(gamma: SourceClient, clob: SourceClient, data: SourceClient) -> Polymarket:
    global _api
    _api = Polymarket(gamma, clob, data)
    return _api


def _bound() -> Polymarket:
    if _api is None:
        raise SourceError("Polymarket adapter is not configured", code="E_SOURCE_UNAVAILABLE")
    return _api


def search(query):
    return _bound().search(query)


def quotes(token_ids):
    return _bound().quotes(token_ids)


def history(token_id, start, end, bucket_s):
    return _bound().history(token_id, start, end, bucket_s)
