"""Kalshi REST payloads to MarketMeta and Snapshot.

Dollar prices are strings and sizes end in `_fp`. Kind, threshold, and outcome
come from fields on the payload (strike type, event ticker, subtitle).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from airscen.adapters.http import SourceClient
from airscen.types import MarketKind, MarketMeta, Snapshot, SourceError, Venue

_CANDLE_PERIODS = {1, 60, 1440}


def _decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _prob(value) -> float | None:
    number = _decimal(value)
    if number is None:
        return None
    parsed = float(number)
    if not 0 <= parsed <= 1:
        raise SourceError(
            f"Kalshi price {value} is outside [0, 1]",
            code="E_SOURCE_BAD_RESPONSE",
            details={"source": "kalshi"},
        )
    return parsed


def _size(value) -> float | None:
    number = _decimal(value)
    return None if number is None else float(number)


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def _epoch(seconds: int | float) -> datetime:
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc)


def _outcome(market: dict) -> str | None:
    subtitle = (market.get("yes_sub_title") or "").lower()
    ticker = market.get("ticker") or ""
    event = market.get("event_ticker") or ""
    if not (event.startswith("KXFEDDECISION") or "federal reserve" in (market.get("title") or "").lower()):
        return None
    if "maintain" in subtitle or ticker.endswith("-H0") or "0bps" in subtitle:
        return "hold"
    if "cut" in subtitle or "-C" in ticker:
        return "cut"
    if "hike" in subtitle or "-H" in ticker:
        return "hike"
    return None


def _kind(market: dict, outcome: str | None) -> MarketKind:
    if outcome is not None:
        return MarketKind.PER_MEETING
    title = (market.get("title") or "").lower()
    rules = (market.get("rules_primary") or "").lower()
    if "at any time" in rules or "touch" in title:
        return MarketKind.TOUCH
    if market.get("strike_type") == "greater" or (market.get("floor_strike") is not None and "above" in title):
        return MarketKind.CLOSE_ABOVE
    return MarketKind.OTHER


def market_meta(market: dict) -> MarketMeta:
    outcome = _outcome(market)
    kind = _kind(market, outcome)
    threshold = market.get("floor_strike")
    unit = None
    title = market.get("title") or ""
    if kind == MarketKind.CLOSE_ABOVE and ("bbl" in title.lower() or "wti" in title.lower()):
        unit = "usd_per_bbl"
    return MarketMeta(
        venue=Venue.KALSHI,
        market_id=market["ticker"],
        title=title,
        rules=market.get("rules_primary") or "",
        close_time=_dt(market.get("close_time")),
        kind=kind,
        outcome=outcome,
        threshold=float(threshold) if threshold is not None and kind == MarketKind.CLOSE_ABOVE else None,
        threshold_unit=unit,
        group_key=market.get("event_ticker"),
    )


def snapshot_from_market(market: dict, src: str = "live") -> Snapshot:
    ts = _dt(market.get("updated_time")) or _dt(market.get("close_time")) or datetime.now(timezone.utc)
    return Snapshot(
        venue=Venue.KALSHI,
        market_id=market["ticker"],
        ts=ts,
        bid=_prob(market.get("yes_bid_dollars")),
        ask=_prob(market.get("yes_ask_dollars")),
        bid_size=_size(market.get("yes_bid_size_fp")),
        ask_size=_size(market.get("yes_ask_size_fp")),
        last=_prob(market.get("last_price_dollars")),
        volume_24h=_size(market.get("volume_24h_fp")),
        open_interest=_size(market.get("open_interest_fp")),
        src=src,
    )


def snapshot_from_candle(ticker: str, candle: dict, src: str = "live") -> Snapshot:
    bid = (candle.get("yes_bid") or {}).get("close_dollars")
    ask = (candle.get("yes_ask") or {}).get("close_dollars")
    price = candle.get("price") or {}
    last = price.get("close_dollars", price.get("previous_dollars"))
    return Snapshot(
        venue=Venue.KALSHI,
        market_id=ticker,
        ts=_epoch(candle["end_period_ts"]),
        bid=_prob(bid),
        ask=_prob(ask),
        last=_prob(last),
        volume_24h=_size(candle.get("volume_fp")),
        open_interest=_size(candle.get("open_interest_fp")),
        src=src,
    )


class Kalshi:
    def __init__(self, client: SourceClient):
        self._client = client

    def list_markets(self, series):
        found = []
        cursor = None
        seen: set[str] = set()
        for _ in range(20):
            params = {"series_ticker": series, "status": "open", "limit": 200}
            if cursor:
                if cursor in seen:
                    break
                seen.add(cursor)
                params["cursor"] = cursor
            payload = self._client.get_json("/markets", params)
            for market in payload.get("markets") or []:
                found.append(market_meta(market))
            cursor = payload.get("cursor") or None
            if not cursor:
                break
        return found

    def quote(self, ticker):
        payload = self._client.get_json(f"/markets/{ticker}", None)
        market = payload.get("market", payload)
        return snapshot_from_market(market)

    def candles(self, ticker, start, end, period_min):
        if int(period_min) not in _CANDLE_PERIODS:
            raise SourceError(
                f"Kalshi candlestick period must be 1, 60, or 1440 minutes, not {period_min}",
                code="E_SOURCE_BAD_RESPONSE",
                details={"source": "kalshi"},
            )
        series = ticker.split("-", 1)[0]
        payload = self._client.get_json(
            f"/series/{series}/markets/{ticker}/candlesticks",
            {
                "period_interval": int(period_min),
                "start_ts": int(start.timestamp()),
                "end_ts": int(end.timestamp()),
            },
        )
        return [snapshot_from_candle(ticker, candle) for candle in payload.get("candlesticks") or []]

    def historical_cutoff(self):
        return self._client.get_json("/historical/cutoff", None)


_api: Kalshi | None = None


def bind(client: SourceClient) -> Kalshi:
    global _api
    _api = Kalshi(client)
    return _api


def _bound() -> Kalshi:
    if _api is None:
        raise SourceError("Kalshi adapter is not configured", code="E_SOURCE_UNAVAILABLE")
    return _api


def list_markets(series):
    return _bound().list_markets(series)


def quote(ticker):
    return _bound().quote(ticker)


def candles(ticker, start, end, period_min):
    return _bound().candles(ticker, start, end, period_min)


def historical_cutoff():
    return _bound().historical_cutoff()
