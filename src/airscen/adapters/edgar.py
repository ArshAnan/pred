"""SEC submissions, filing documents, and company-concept XBRL.

CIKs come from `company_tickers_exchange.json`. Documents are cached under
`data/raw/edgar/` and are fetched as text, not JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from airscen.adapters.http import SourceClient
from airscen.types import SourceError

_TICKER_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
_WANTED_FORMS = ("8-K", "10-Q")


@dataclass(frozen=True)
class FilingRef:
    form: str
    filed: date
    accession: str
    primary_document: str
    url: str


def _cik_text(cik: int) -> str:
    return f"{int(cik):010d}"


def _archive_url(cik: int, accession: str, document: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}"


def _on_or_after(filed: date, since) -> bool:
    if since is None:
        return True
    if isinstance(since, datetime):
        since = since.date()
    return filed >= since


class Edgar:
    def __init__(self, client: SourceClient, document_client: SourceClient | None = None, cache_dir: Path | None = None):
        self._client = client
        self._documents = document_client or client
        self._cache = Path(cache_dir) if cache_dir is not None else Path("data/raw/edgar")
        self._tickers: dict[str, int] | None = None

    def resolve_cik(self, ticker: str) -> int:
        key = ticker.upper()
        table = self._ticker_table()
        if key not in table:
            raise SourceError(
                f"SEC has no CIK for {ticker}",
                code="E_SOURCE_BAD_RESPONSE",
                details={"source": "edgar", "ticker": ticker},
            )
        return table[key]

    def recent_filings(self, cik, since):
        payload = self._client.get_json(f"/submissions/CIK{_cik_text(int(cik))}.json", None)
        recent = (payload.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        found = []
        for index, form in enumerate(forms):
            if form not in _WANTED_FORMS:
                continue
            filed = date.fromisoformat(recent["filingDate"][index])
            if not _on_or_after(filed, since):
                continue
            accession = recent["accessionNumber"][index]
            document = recent["primaryDocument"][index]
            found.append(FilingRef(
                form=form,
                filed=filed,
                accession=accession,
                primary_document=document,
                url=_archive_url(int(cik), accession, document),
            ))
        return found

    def fetch_document(self, ref) -> str:
        url = ref.url if isinstance(ref, FilingRef) else str(ref)
        name = ref.accession if isinstance(ref, FilingRef) else _cache_name(url)
        self._cache.mkdir(parents=True, exist_ok=True)
        path = self._cache / name
        if path.is_file() and path.stat().st_size:
            return path.read_text(errors="replace")
        body = self._documents.get_text(url, None)
        path.write_text(body)
        return body

    def xbrl_facts(self, cik, tags):
        rows = []
        for tag in tags:
            try:
                payload = self._client.get_json(
                    f"/api/xbrl/companyconcept/CIK{_cik_text(int(cik))}/us-gaap/{tag}.json",
                    None,
                )
            except SourceError as exc:
                # us-gaap/Liabilities.json is NoSuchKey for Delta. Keep the tags that exist.
                if exc.details.get("status") == 404 or "404" in exc.message:
                    continue
                raise
            for unit, facts in (payload.get("units") or {}).items():
                for fact in facts:
                    rows.append({
                        "tag": payload.get("tag") or tag,
                        "unit": unit,
                        "end": fact.get("end"),
                        "value": fact.get("val"),
                        "filed": fact.get("filed"),
                        "form": fact.get("form"),
                        "fy": fact.get("fy"),
                        "fp": fact.get("fp"),
                        "accn": fact.get("accn"),
                    })
        return rows

    def _ticker_table(self) -> dict[str, int]:
        if self._tickers is None:
            payload = self._client.get_json(_TICKER_URL, None)
            fields = payload.get("fields") or []
            self._tickers = {}
            for row in payload.get("data") or []:
                item = dict(zip(fields, row))
                ticker = item.get("ticker")
                cik = item.get("cik")
                if ticker and cik is not None:
                    self._tickers[str(ticker).upper()] = int(cik)
        return self._tickers


def _cache_name(url: str) -> str:
    return url.rstrip("/").split("/")[-1] or "document.html"


_api: Edgar | None = None


def bind(client: SourceClient, document_client: SourceClient | None = None, cache_dir: Path | None = None) -> Edgar:
    global _api
    _api = Edgar(client, document_client, cache_dir)
    return _api


def _bound() -> Edgar:
    if _api is None:
        raise SourceError("EDGAR adapter is not configured", code="E_SOURCE_UNAVAILABLE")
    return _api


def resolve_cik(ticker):
    return _bound().resolve_cik(ticker)


def recent_filings(cik, since):
    return _bound().recent_filings(cik, since)


def fetch_document(ref):
    return _bound().fetch_document(ref)


def xbrl_facts(cik, tags):
    return _bound().xbrl_facts(cik, tags)
