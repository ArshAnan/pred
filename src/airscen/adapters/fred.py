"""FRED observations with vintages. A `.` value is a missing observation and is skipped.

Series ids used by the engine are `DGS10` (10-year yield, percent) and
`DCOILWTICO` (WTI spot, dollars per barrel). A live `DCOILWTICO` pull needs
`FRED_API_KEY`; the observations schema below is the one FRED returned for `DGS10`.
"""

from __future__ import annotations

from datetime import date, datetime

from airscen.adapters.http import SourceClient
from airscen.types import MacroObs, SourceError

DGS10 = "DGS10"
DCOILWTICO = "DCOILWTICO"


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def parse_observations(series_id: str, payload: dict, as_of) -> list[MacroObs]:
    as_of_day = _as_date(as_of)
    rows = []
    for obs in payload.get("observations") or []:
        raw = obs.get("value")
        if raw is None or str(raw).strip() in {"", "."}:
            continue
        obs_date = date.fromisoformat(obs["date"])
        vintage = date.fromisoformat(obs.get("realtime_start") or payload["realtime_start"])
        if obs_date > as_of_day or vintage > as_of_day:
            continue
        rows.append(MacroObs(
            series_id=series_id,
            obs_date=obs_date,
            value=float(raw),
            realtime_start=vintage,
        ))
    return rows


class Fred:
    def __init__(self, client: SourceClient, api_key: str):
        self._client = client
        self._api_key = api_key

    def observations(self, series_id, start, end, as_of):
        if not self._api_key:
            raise SourceError(
                "FRED_API_KEY is not set",
                code="E_SOURCE_AUTH",
                details={"source": "fred"},
            )
        payload = self._client.get_json(
            "/series/observations",
            {
                "series_id": series_id,
                "api_key": self._api_key,
                "file_type": "json",
                "observation_start": _as_date(start).isoformat(),
                "observation_end": _as_date(end).isoformat(),
                "realtime_start": "1776-07-04",
                "realtime_end": _as_date(as_of).isoformat(),
            },
        )
        return parse_observations(series_id, payload, as_of)


_api: Fred | None = None


def bind(client: SourceClient, api_key: str) -> Fred:
    global _api
    _api = Fred(client, api_key)
    return _api


def _bound() -> Fred:
    if _api is None:
        raise SourceError("FRED adapter is not configured", code="E_SOURCE_UNAVAILABLE")
    return _api


def observations(series_id, start, end, as_of):
    return _bound().observations(series_id, start, end, as_of)
