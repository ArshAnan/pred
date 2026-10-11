"""A5–A8: recorded Kalshi, Polymarket, SEC, and FRED payloads parse offline."""

import json
from datetime import date, datetime, timezone
from pathlib import Path

from airscen.adapters.alpaca import Alpaca
from airscen.adapters.edgar import Edgar
from airscen.adapters.fred import parse_observations
from airscen.adapters.gdelt import Gdelt, parse_articles
from airscen.adapters.kalshi import Kalshi, snapshot_from_market
from airscen.adapters.polymarket import Polymarket, last_trade, market_meta
from airscen.types import MarketKind, SourceError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "vendors"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClient:
    def __init__(self, pages=None, text="", tables=None):
        self.pages = list(pages or [])
        self.text = text
        self.tables = tables or {}
        self.posts = []
        self.gets = []

    def get_json(self, path, params):
        self.gets.append((path, params))
        if self.pages:
            return self.pages.pop(0)
        for key, value in self.tables.items():
            if key in path:
                return value
        raise AssertionError(path)

    def post_json(self, path, body):
        self.posts.append((path, body))
        if self.pages:
            return self.pages.pop(0)
        return []

    def get_text(self, path, params=None):
        self.gets.append(("text", path, params))
        return self.text


def test_kalshi_wti_market_is_a_close_above_threshold():
    payload = _load("kalshi_kxwti_markets.json")
    client = FakeClient(pages=[payload, {"markets": []}])
    markets = Kalshi(client).list_markets("KXWTI")
    assert client.gets[0][1]["series_ticker"] == "KXWTI"
    assert client.gets[0][1]["status"] == "open"
    first = markets[0]
    assert first.market_id == "KXWTI-26OCT1314-T102.99"
    assert first.kind == MarketKind.CLOSE_ABOVE
    assert first.threshold == 102.99
    assert first.threshold_unit == "usd_per_bbl"
    assert first.group_key == "KXWTI-26OCT1314"
    same_day = [m for m in markets if m.group_key == "KXWTI-26OCT1314"]
    assert len(same_day) >= 3
    snap = snapshot_from_market(payload["markets"][0])
    assert 0 <= snap.bid <= snap.ask <= 1


def test_kalshi_fed_outcomes_and_quote_and_candles():
    fed = _load("kalshi_kxfeddecision_markets.json")
    by_id = {meta.market_id: meta for meta in Kalshi(FakeClient(pages=[fed, {"markets": []}])).list_markets("KXFEDDECISION")}
    assert by_id["KXFEDDECISION-28JAN-H0"].outcome == "hold"
    assert by_id["KXFEDDECISION-28JAN-H25"].outcome == "hike"
    assert by_id["KXFEDDECISION-28JAN-C25"].outcome == "cut"
    assert by_id["KXFEDDECISION-28JAN-H0"].kind == MarketKind.PER_MEETING

    quote = Kalshi(FakeClient(pages=[_load("kalshi_market.json")])).quote("KXMVECROSSCATEGORY-S20268195AAAEBAB-6677BFABE11")
    assert quote.bid == 0
    assert quote.ask == 0
    assert 0 <= quote.last <= 1

    start = datetime(2026, 10, 10, tzinfo=timezone.utc)
    end = datetime(2026, 10, 11, tzinfo=timezone.utc)
    candles = Kalshi(FakeClient(pages=[_load("kalshi_candles.json")])).candles(
        "KXFEDDECISION-28JAN-H26", start, end, 60
    )
    assert len(candles) == 1
    assert candles[0].bid == 0.1
    assert candles[0].ask == 0.14
    assert 0 <= candles[0].bid <= candles[0].ask <= 1

    cutoff = Kalshi(FakeClient(pages=[_load("kalshi_historical_cutoff.json")])).historical_cutoff()
    assert cutoff["market_settled_ts"].startswith("2026-08-11")


def test_polymarket_gamma_clob_and_history_parse():
    gamma = _load("polymarket_gamma_markets.json")
    xi = market_meta(gamma[0])
    assert xi.token_id.startswith("32338220190071351435772801779725302244575775216413325951443816017994629993401")
    assert xi.kind == MarketKind.OTHER
    assert xi.venue.value == "polymarket"

    search = Polymarket(
        FakeClient(pages=[_load("polymarket_gamma_search.json")]),
        FakeClient(),
        FakeClient(),
    ).search("fed decision")
    fed = next(meta for meta in search if "50+" in meta.title)
    assert fed.kind == MarketKind.PER_MEETING
    assert fed.outcome == "cut"
    assert fed.token_id

    book = _load("polymarket_clob_books.json")
    quotes = Polymarket(FakeClient(), FakeClient(pages=[book]), FakeClient()).quotes([xi.token_id])
    assert quotes[0].bid == 0.029
    assert quotes[0].ask == 0.03
    assert 0 <= quotes[0].bid <= quotes[0].ask <= 1
    assert quotes[0].last == 0.97

    history = _load("polymarket_prices_history.json")
    points = Polymarket(FakeClient(), FakeClient(), FakeClient(pages=[history])).history(
        xi.token_id,
        datetime(2026, 10, 1, tzinfo=timezone.utc),
        datetime(2026, 10, 11, tzinfo=timezone.utc),
        10800,
    )
    assert points[0].last == 0.0315
    assert points[0].ts.year == 2026
    assert last_trade(_load("polymarket_last_trade.json")) == 0.029
    assert last_trade({"price": "0.5", "side": ""}) is None


def test_quotes_are_batched_at_500():
    clob = FakeClient()
    Polymarket(FakeClient(), clob, FakeClient()).quotes([str(i) for i in range(501)])
    assert [len(body) for _, body in clob.posts] == [500, 1]


def test_edgar_resolves_airlines_and_caches_the_8k(tmp_path):
    edgar = Edgar(
        FakeClient(tables={
            "company_tickers_exchange.json": _load("sec_company_tickers_exchange.json"),
            "submissions/CIK": _load("sec_dal_submissions.json"),
            "us-gaap/Assets.json": _load("sec_dal_assets_concept.json"),
        }),
        document_client=FakeClient(text=(FIXTURES / "sec_dal_8k_2026-10-09.html").read_text(encoding="utf-8")[:200]),
        cache_dir=tmp_path,
    )
    assert edgar.resolve_cik("DAL") == 27904
    assert edgar.resolve_cik("UAL") == 100517
    assert edgar.resolve_cik("AAL") == 6201
    filings = edgar.recent_filings(27904, date(2026, 10, 1))
    forms = sorted(item.form for item in filings)
    assert forms == ["10-Q", "8-K"]
    eight = next(item for item in filings if item.form == "8-K")
    assert eight.accession == "0000027904-26-000035"
    body = edgar.fetch_document(eight)
    assert "EX-99.1" in body or "Document" in body
    edgar.fetch_document(eight)
    assert sum(1 for call in edgar._documents.gets if call[0] == "text") == 1
    facts = edgar.xbrl_facts(27904, ["Assets"])
    assert facts[0]["end"]
    assert facts[0]["filed"]
    assert facts[0]["tag"] == "Assets"


def test_xbrl_skips_a_missing_concept():
    class _Client(FakeClient):
        def get_json(self, path, params):
            if path.endswith("/Liabilities.json"):
                raise SourceError(
                    "edgar returned HTTP 404",
                    code="E_SOURCE_BAD_RESPONSE",
                    details={"status": 404},
                )
            return super().get_json(path, params)

    edgar = Edgar(_Client(tables={"us-gaap/Assets.json": _load("sec_dal_assets_concept.json")}))
    facts = edgar.xbrl_facts(27904, ["Assets", "Liabilities"])
    assert {row["tag"] for row in facts} == {"Assets"}


def test_gdelt_requests_an_artlist_and_ignores_a_non_article_body():
    client = FakeClient(pages=[{"articles": []}])
    rows = Gdelt(client).headlines("Delta Air Lines", datetime(2026, 10, 10, tzinfo=timezone.utc), 24, 3)
    assert rows == []
    _path, params = client.gets[0]
    assert params["mode"] == "artlist"
    assert params["format"] == "json"
    assert params["maxrecords"] == "3"
    rate_limit = (FIXTURES / "gdelt_429.txt").read_text(encoding="utf-8")
    assert parse_articles(rate_limit) == []


def test_alpaca_refuses_to_call_without_keys():
    client = FakeClient()
    try:
        Alpaca(client, "", "").daily_bars(["DAL"], date(2026, 7, 1), date(2026, 7, 12))
    except SourceError as exc:
        assert exc.code == "E_SOURCE_AUTH"
    else:
        raise AssertionError("missing Alpaca keys should fail")
    assert client.gets == []
    barred = Alpaca(FakeClient(pages=[{"bars": {}}]), "key", "secret").daily_bars(
        ["DAL"], date(2026, 7, 1), date(2026, 7, 12),
    )
    assert barred == []


def test_fred_parses_dgs10_and_skips_missing_values():
    payload = _load("fred_dgs10_observations.json")
    payload["observations"].append({
        "realtime_start": "2026-10-09",
        "realtime_end": "2026-10-09",
        "date": "2026-10-07",
        "value": ".",
    })
    rows = parse_observations("DGS10", payload, date(2026, 10, 9))
    assert [(row.obs_date, row.value, row.realtime_start) for row in rows] == [
        (date(2026, 10, 8), 5.22, date(2026, 10, 9))
    ]
    assert parse_observations("DGS10", payload, date(2026, 10, 8)) == []
