"""Background loops.

Dependencies are constructor arguments so this module never imports the
product lane. `label_fn` is scout, wrapped by the API lifespan. `after_market_tick`
is the alert evaluator. Both are caught by the loop.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from airscen.adapters.alpaca import Alpaca
from airscen.adapters.edgar import Edgar
from airscen.adapters.gdelt import Gdelt
from airscen.adapters.fred import DCOILWTICO, DGS10, Fred
from airscen.adapters.http import SourceClient
from airscen.adapters.kalshi import Kalshi
from airscen.adapters.polymarket import Polymarket
from airscen.config import Settings
from airscen.extract import extract_facts, load_overrides
from airscen.store import Repository, market_content_hash
from airscen.types import (
    Basis,
    Extraction,
    Fact,
    MarketLabel,
    MarketMeta,
    Metric,
    Snapshot,
    SourceStatus,
    Venue,
    WorkerStatus,
)

log = logging.getLogger("airscen.worker")

_DEGRADED_AFTER_S = 900
_QUOTE_BATCH = 500
_DISCOVERY_S = 6 * 60 * 60
_DISCOVERY_RETRY_S = 30
_BACKFILL_CHUNK = timedelta(days=14)
_MACRO_LOOKBACK = timedelta(days=800)
_FILING_LOOKBACK = timedelta(days=180)
_SOURCES = ("kalshi", "polymarket", "edgar", "fred", "alpaca", "gdelt")
_XBRL_TAGS = {
    "Assets": Metric.TOTAL_ASSETS,
    "Liabilities": Metric.TOTAL_LIABILITIES,
    "StockholdersEquity": Metric.EQUITY,
}


@dataclass(frozen=True)
class Adapters:
    """One object per source. Each object exposes that adapter's LLD functions.

    A module works, because `kalshi.quote` is the function `quote`. Tests pass
    fakes with the same attributes. The worker does not construct clients.
    """

    kalshi: Any
    polymarket: Any
    edgar: Any
    fred: Any
    alpaca: Any
    gdelt: Any


def build_adapters(settings: Settings) -> Adapters:
    """Clients for a later API lifespan. Adapters do not import config."""
    edgar_headers = {"User-Agent": settings.sec_user_agent, "Accept": "application/json"}
    return Adapters(
        kalshi=Kalshi(SourceClient(
            "kalshi", "https://external-api.kalshi.com/trade-api/v2", 10, {},
        )),
        polymarket=Polymarket(
            SourceClient("polymarket-gamma", "https://gamma-api.polymarket.com", 10, {}),
            SourceClient("polymarket-clob", "https://clob.polymarket.com", 10, {}),
            SourceClient("polymarket-data", "https://data-api.polymarket.com", 5, {}),
        ),
        edgar=Edgar(
            SourceClient("edgar", "https://data.sec.gov", 5, edgar_headers),
            SourceClient(
                "edgar-docs", "https://www.sec.gov", 5,
                {"User-Agent": settings.sec_user_agent},
                read_timeout=20,
            ),
        ),
        fred=Fred(
            SourceClient("fred", "https://api.stlouisfed.org/fred", 2, {}),
            settings.fred_api_key,
        ),
        alpaca=Alpaca(
            SourceClient(
                "alpaca", "https://data.alpaca.markets/v2", 3,
                {
                    "APCA-API-KEY-ID": settings.alpaca_key_id,
                    "APCA-API-SECRET-KEY": settings.alpaca_secret_key,
                },
            ),
            settings.alpaca_key_id,
            settings.alpaca_secret_key,
        ),
        gdelt=Gdelt(SourceClient(
            "gdelt", "https://api.gdeltproject.org/api/v2/doc/doc", 1, {},
        )),
    )


class WorkerLoop:
    def __init__(
        self,
        repo: Repository,
        adapters: Adapters,
        settings: Settings,
        label_fn: Callable[[list[MarketMeta]], None],
        after_market_tick: Callable[[datetime], None] | None = None,
    ):
        self._repo = repo
        self._adapters = adapters
        self._settings = settings
        self._label_fn = label_fn
        self._after = after_market_tick
        self._stop = False
        self._thread: threading.Thread | None = None
        self._last_ok: dict[str, datetime | None] = {name: None for name in _SOURCES}
        self._streak: dict[str, int] = {name: 0 for name in _SOURCES}
        self._last_tick: datetime | None = None
        self._tick_failures = 0
        self._degraded = False
        self._filings_since = (datetime.now(timezone.utc) - _FILING_LOOKBACK).date()

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="airscen-worker", daemon=True)
        self._thread.start()

    def stop(self):
        """Ask the loop to exit. The tick already running finishes first."""
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None

    def tick_markets(self):
        self._run_tick(self._record_quotes)

    def tick_filings(self):
        self._run_tick(self._ingest_filings)

    def tick_macro(self):
        self._run_tick(self._ingest_macro)

    def discover(self) -> bool:
        """List and search, then label markets whose title, rules, or close time changed.

        Returns True when some candidates were not labeled this pass. Their content hash is left
        unchanged so the next pass can try again.
        """
        found: list[tuple[MarketMeta, bool]] = []
        for series in self._settings.discovery.kalshi_series:
            if self._stop:
                return False
            try:
                for meta in self._adapters.kalshi.list_markets(series) or []:
                    found.append((meta, True))
                self._mark_ok("kalshi")
            except Exception:
                log.exception("kalshi discovery failed for %s", series)
                self._mark_fail("kalshi")
        for query in self._settings.discovery.polymarket_queries:
            if self._stop:
                return False
            try:
                for meta in self._adapters.polymarket.search(query) or []:
                    found.append((meta, False))
                self._mark_ok("polymarket")
            except Exception:
                log.exception("polymarket discovery failed for %s", query)
                self._mark_fail("polymarket")
        kept = self._prefilter(found)
        changed = [
            meta for meta in kept
            if self._repo.content_hash_for(meta.venue, meta.market_id) != market_content_hash(meta)
        ]
        if hasattr(self._label_fn, "unattempted"):
            self._label_fn.unattempted = set()
        labels = self._labels_for(changed)
        unattempted = set(getattr(self._label_fn, "unattempted", ()) or ())
        changed_ids = {(meta.venue, meta.market_id) for meta in changed}
        for meta in kept:
            if self._stop:
                return bool(unattempted)
            key = (meta.venue, meta.market_id)
            if key in unattempted:
                continue
            label = labels.get(key) if key in changed_ids else None
            if not isinstance(label, MarketLabel):
                label = None
            self._repo.upsert_market(meta, label)
        return bool(unattempted)

    def backfill(self, start: datetime, end: datetime) -> list[MarketMeta]:
        """Write 60-minute Kalshi candles and 3-hour Polymarket history.

        Returns markets that produced no rows, so a later run can set BACKFILL_GAP.
        """
        gaps = []
        for meta, label in self._repo.markets():
            if self._stop:
                break
            if label is None or not label.relevant:
                continue
            try:
                wrote = self._backfill_one(meta, start, end)
            except Exception:
                log.exception("backfill failed for %s %s", meta.venue, meta.market_id)
                wrote = False
            if not wrote:
                gaps.append(meta)
        return gaps

    def status(self) -> tuple[dict[str, SourceStatus], WorkerStatus]:
        sources = {name: self._source_status(name) for name in _SOURCES}
        return sources, WorkerStatus(last_tick_at=self._last_tick, degraded=self._degraded)

    def last_snapshot_age_s(self) -> float | None:
        newest = self._repo.newest_snapshot_ts()
        if newest is None:
            return None
        return (self._now() - newest).total_seconds()

    def db_ok(self) -> bool:
        return self._repo.ping()

    def _loop(self) -> None:
        next_discover = 0.0
        next_market = 0.0
        next_filing = 0.0
        next_macro = 0.0
        while not self._stop:
            now = time.monotonic()
            if now >= next_discover:
                incomplete = bool(self.discover())
                next_discover = now + (_DISCOVERY_RETRY_S if incomplete else _DISCOVERY_S)
            if self._stop:
                break
            if now >= next_market:
                self.tick_markets()
                next_market = now + self._settings.poll.market_s
            if self._stop:
                break
            if now >= next_filing:
                self.tick_filings()
                next_filing = now + self._settings.poll.filings_s
            if self._stop:
                break
            if now >= next_macro:
                self.tick_macro()
                next_macro = now + self._settings.poll.macro_s
            self._sleep_until(min(next_discover, next_market, next_filing, next_macro))

    def _sleep_until(self, deadline: float) -> None:
        while not self._stop and time.monotonic() < deadline:
            time.sleep(0.05)

    def _run_tick(self, body: Callable[[], None]) -> None:
        try:
            body()
        except Exception:
            log.exception("worker tick failed")
            self._tick_failures += 1
            if self._tick_failures >= 3:
                self._degraded = True
        else:
            self._tick_failures = 0
            self._degraded = False
        self._last_tick = self._now()

    def _record_quotes(self) -> None:
        as_of = self._now()
        relevant = [
            meta for meta, label in self._repo.markets()
            if label is not None and label.relevant
        ]
        snaps: list[Snapshot] = []
        kalshi = [meta for meta in relevant if meta.venue == Venue.KALSHI]
        polymarket = [meta for meta in relevant if meta.venue == Venue.POLYMARKET and meta.token_id]
        if kalshi:
            snaps.extend(self._guard("kalshi", lambda: self._kalshi_quotes(kalshi)))
        if polymarket and not self._stop:
            snaps.extend(self._guard("polymarket", lambda: self._polymarket_quotes(polymarket)))
        known = {(meta.venue, meta.market_id) for meta in relevant}
        by_token = {meta.token_id: meta for meta in polymarket}
        cleaned = []
        for snap in snaps:
            if snap is None:
                continue
            if (snap.venue, snap.market_id) not in known and snap.market_id in by_token:
                snap = snap.model_copy(update={"market_id": by_token[snap.market_id].market_id})
            if (snap.venue, snap.market_id) not in known:
                continue
            kept = self._clean(snap)
            if kept is not None:
                cleaned.append(kept)
        if cleaned:
            self._repo.put_snapshots(cleaned)
        if self._after is not None:
            try:
                self._after(as_of)
            except Exception:
                log.exception("after_market_tick failed")

    def _kalshi_quotes(self, markets: list[MarketMeta]) -> list[Snapshot]:
        rows = []
        for meta in markets:
            if self._stop:
                break
            rows.append(self._adapters.kalshi.quote(meta.market_id))
        return rows

    def _polymarket_quotes(self, markets: list[MarketMeta]) -> list[Snapshot]:
        rows = []
        tokens = [meta.token_id for meta in markets]
        for start in range(0, len(tokens), _QUOTE_BATCH):
            if self._stop:
                break
            rows.extend(self._adapters.polymarket.quotes(tokens[start:start + _QUOTE_BATCH]) or [])
        return rows

    def _ingest_filings(self) -> None:
        overrides = load_overrides()
        saw_edgar = False
        newest = self._filings_since
        for company in self._settings.companies:
            if self._stop or not company.cik:
                continue
            try:
                refs = self._adapters.edgar.recent_filings(company.cik, self._filings_since) or []
            except Exception:
                log.exception("edgar filings failed for %s", company.ticker)
                self._mark_fail("edgar")
                continue
            saw_edgar = True
            for ref in refs:
                if self._stop:
                    break
                try:
                    text = self._adapters.edgar.fetch_document(ref)
                    facts = extract_facts(company.ticker, text, ref, llm=None)
                except Exception:
                    log.exception("extract failed for %s", company.ticker)
                    facts = [fact for fact in overrides if fact.company == company.ticker]
                if getattr(ref, "form", "") == "10-Q":
                    facts = self._with_xbrl(company.cik, ref, facts)
                if facts:
                    self._repo.upsert_facts(facts)
                filed = getattr(ref, "filed", None)
                if isinstance(filed, datetime):
                    filed = filed.date()
                if isinstance(filed, date) and filed > newest:
                    newest = filed
        if saw_edgar:
            self._mark_ok("edgar")
            self._filings_since = newest

    def _with_xbrl(self, cik: int, ref, facts: list[Fact]) -> list[Fact]:
        """Persist XBRL totals beside the extracted facts so a disagreement is visible."""
        try:
            rows = self._adapters.edgar.xbrl_facts(cik, list(_XBRL_TAGS)) or []
        except Exception:
            log.exception("xbrl cross-check failed")
            return facts
        extra = []
        for row in rows:
            metric = _XBRL_TAGS.get(row.get("tag"))
            if metric is None or row.get("value") is None or not row.get("end"):
                continue
            unit = str(row.get("unit") or "")
            if "share" in unit.lower():
                continue
            period_end = date.fromisoformat(str(row["end"])[:10])
            if not any(fact.period_end == period_end and fact.metric == metric for fact in facts):
                continue
            filed = row.get("filed")
            filed_at = _as_datetime(filed) if filed else _ref_filed(ref)
            try:
                extra.append(Fact(
                    company=facts[0].company,
                    metric=metric,
                    value=float(row["value"]) / 1_000_000,
                    unit="usd_m",
                    basis=Basis.GAAP,
                    period_end=period_end,
                    filed_at=filed_at,
                    source_url=_ref_url(ref),
                    extraction=Extraction.XBRL,
                    validated=True,
                ))
            except (TypeError, ValueError):
                continue
        return facts + extra

    def _ingest_macro(self) -> None:
        as_of = self._now()
        start = as_of - _MACRO_LOOKBACK
        wrote = False
        for series_id in (DGS10, DCOILWTICO):
            if self._stop:
                return
            try:
                rows = self._adapters.fred.observations(series_id, start, as_of, as_of) or []
            except Exception:
                log.exception("fred failed for %s", series_id)
                self._mark_fail("fred")
                continue
            if rows:
                self._repo.put_macro(rows)
            wrote = True
        if wrote:
            self._mark_ok("fred")

    def _backfill_one(self, meta: MarketMeta, start: datetime, end: datetime) -> bool:
        if meta.venue == Venue.KALSHI:
            candles = self._adapters.kalshi.candles(meta.market_id, start, end, 60) or []
            rows = [
                candle.model_copy(update={"src": "backfill_kalshi", "market_id": meta.market_id})
                for candle in candles
            ]
        elif meta.venue == Venue.POLYMARKET and meta.token_id:
            points = []
            cursor = start
            while cursor < end:
                chunk_end = min(end, cursor + _BACKFILL_CHUNK)
                if chunk_end <= cursor:
                    break
                points.extend(self._adapters.polymarket.history(
                    meta.token_id, cursor, chunk_end, 10800,
                ) or [])
                cursor = chunk_end
            rows = [
                point.model_copy(update={"src": "backfill_polymarket", "market_id": meta.market_id})
                for point in points
            ]
        else:
            return False
        if not rows:
            return False
        self._repo.put_snapshots(rows)
        return True

    def _prefilter(self, found: list[tuple[MarketMeta, bool]]) -> list[MarketMeta]:
        kept = []
        seen: set[tuple] = set()
        keywords = [word.lower() for word in self._settings.discovery.keywords]
        for meta, from_series in found:
            key = (meta.venue, meta.market_id)
            if key in seen:
                continue
            text = f"{meta.title} {meta.rules}".lower()
            on_topic = from_series or any(word in text for word in keywords)
            if not on_topic:
                continue
            seen.add(key)
            kept.append(meta)
        return kept

    def _labels_for(self, markets: list[MarketMeta]) -> dict[tuple, MarketLabel]:
        if not markets:
            return {}
        try:
            result = self._label_fn(markets)
        except Exception:
            log.exception("label_fn failed")
            return {}
        labels: dict[tuple, MarketLabel] = {}
        if isinstance(result, dict):
            items = result.items()
        elif isinstance(result, list):
            items = []
            for item in result:
                if isinstance(item, tuple) and len(item) == 2:
                    meta, label = item
                    items.append(((meta.venue, meta.market_id), label))
        else:
            return {}
        for key, label in items:
            if isinstance(label, MarketLabel):
                labels[key] = label
        return labels

    def _guard(self, source: str, fetch) -> list:
        try:
            rows = fetch()
        except Exception:
            log.exception("%s failed", source)
            self._mark_fail(source)
            return []
        self._mark_ok(source)
        return [row for row in rows if row is not None]

    def _clean(self, snap: Snapshot) -> Snapshot | None:
        try:
            from airscen.trust import clean
        except NotImplementedError:
            return snap
        try:
            return clean(snap, self._now())
        except NotImplementedError:
            return snap

    def _source_status(self, name: str) -> SourceStatus:
        last = self._last_ok[name]
        streak = self._streak[name]
        if last is None:
            status = "down" if streak else "ok"
        elif (self._now() - last).total_seconds() > _DEGRADED_AFTER_S:
            status = "degraded"
        else:
            status = "ok"
        return SourceStatus(status=status, last_ok_at=last, fail_streak=streak)

    def _mark_ok(self, source: str) -> None:
        self._last_ok[source] = self._now()
        self._streak[source] = 0

    def _mark_fail(self, source: str) -> None:
        self._streak[source] = self._streak.get(source, 0) + 1

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _ref_url(ref) -> str:
    return str(getattr(ref, "url", ref))


def _ref_filed(ref) -> datetime:
    filed = getattr(ref, "filed", None)
    if isinstance(filed, datetime):
        return _as_datetime(filed)
    if isinstance(filed, date):
        return datetime(filed.year, filed.month, filed.day, 12, tzinfo=timezone.utc)
    return datetime.now(timezone.utc)
