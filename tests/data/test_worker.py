"""Recorder ticks, source health, and discovery with fake adapters."""

from datetime import date, datetime, timedelta, timezone

from airscen.adapters.edgar import FilingRef
from airscen.config import DiscoveryConfig, Settings
from airscen.store import Repository
from airscen.types import (
    Factor,
    MarketKind,
    MarketLabel,
    MarketMeta,
    Metric,
    Snapshot,
    Venue,
)
from airscen.worker import WorkerLoop, build_adapters
from tests.fakes.fake_adapters import (
    FakeEdgar,
    FakeFred,
    FakeGdelt,
    FakeKalshi,
    FakePolymarket,
    FakeAlpaca,
)
from airscen.worker import Adapters

_NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def _label(**kwargs):
    defaults = dict(
        relevant=True,
        factor=Factor.FUEL,
        yes_moves_factor="up",
        label_source="hand",
    )
    defaults.update(kwargs)
    return MarketLabel(**defaults)


def _meta(venue, market_id, token_id=None, title="WTI above 100", kind=MarketKind.CLOSE_ABOVE):
    return MarketMeta(
        venue=venue,
        market_id=market_id,
        token_id=token_id,
        title=title,
        rules="settles on the close",
        close_time=_NOW,
        kind=kind,
    )


def _snap(venue, market_id, bid=0.40, ask=0.42):
    return Snapshot(
        venue=venue,
        market_id=market_id,
        ts=_NOW,
        bid=bid,
        ask=ask,
        bid_size=100,
        ask_size=100,
        last=0.41,
        src="live",
    )


def _worker(repo, kalshi, polymarket, hook, edgar=None, fred=None, settings=None, label_fn=None):
    adapters = Adapters(
        kalshi=kalshi,
        polymarket=polymarket,
        edgar=edgar or FakeEdgar(),
        fred=fred or FakeFred(),
        alpaca=FakeAlpaca(),
        gdelt=FakeGdelt(),
    )
    return WorkerLoop(
        repo,
        adapters,
        settings or Settings(),
        label_fn or (lambda markets: None),
        hook,
    )


def test_three_ticks_skip_a_failing_source_and_call_the_hook():
    repo = Repository(":memory:")
    repo.migrate()
    kalshi_market = _meta(Venue.KALSHI, "KXWTI-26OCT-T100")
    poly_market = _meta(Venue.POLYMARKET, "cond-1", token_id="tok-1", title="WTI crude")
    repo.upsert_market(kalshi_market, _label())
    repo.upsert_market(poly_market, _label(factor=Factor.RATES, yes_moves_factor="down"))
    quote_ts = datetime.now(timezone.utc)
    kalshi = FakeKalshi(quote=_snap(Venue.KALSHI, kalshi_market.market_id).model_copy(update={"ts": quote_ts}))
    polymarket = FakePolymarket(quotes=[_snap(Venue.POLYMARKET, "cond-1")])
    polymarket.fail = True
    hooks = []
    worker = _worker(repo, kalshi, polymarket, hooks.append)
    for _ in range(3):
        worker.tick_markets()
    assert len(hooks) == 3
    sources, status = worker.status()
    assert sources["polymarket"].status == "down"
    assert sources["polymarket"].fail_streak == 3
    assert sources["kalshi"].status == "ok"
    assert sources["kalshi"].fail_streak == 0
    stored, stale = repo.latest_snapshot(Venue.KALSHI, kalshi_market.market_id, hooks[-1])
    assert stored is not None and stored.bid == 0.40
    assert stale is False
    assert repo.latest_snapshot(Venue.POLYMARKET, "cond-1", hooks[-1])[0] is None
    assert status.last_tick_at is not None
    assert status.degraded is False
    assert worker.db_ok() is True
    assert worker.last_snapshot_age_s() is not None


def test_hook_and_label_errors_do_not_stop_the_tick():
    repo = Repository(":memory:")
    repo.migrate()
    market = _meta(Venue.KALSHI, "KXWTI-26OCT-T100")
    repo.upsert_market(market, _label())
    kalshi = FakeKalshi(quote=_snap(Venue.KALSHI, market.market_id))

    def explode(_markets):
        raise RuntimeError("scout is not ready")

    def hook(_as_of):
        raise RuntimeError("alerts are not ready")

    worker = _worker(repo, kalshi, FakePolymarket(), hook, label_fn=explode)
    worker.tick_markets()
    assert repo.latest_snapshot(Venue.KALSHI, market.market_id, datetime.now(timezone.utc))[0] is not None


def test_three_failed_ticks_mark_the_worker_degraded():
    repo = Repository(":memory:")
    repo.migrate()
    worker = _worker(repo, FakeKalshi(), FakePolymarket(), None)

    def boom():
        raise RuntimeError("store is locked")

    worker._record_quotes = boom
    for _ in range(3):
        worker.tick_markets()
    assert worker.status()[1].degraded is True


def test_source_is_degraded_after_fifteen_minutes():
    repo = Repository(":memory:")
    repo.migrate()
    worker = _worker(repo, FakeKalshi(), FakePolymarket(), None)
    worker._last_ok["fred"] = datetime.now(timezone.utc) - timedelta(minutes=16)
    assert worker.status()[0]["fred"].status == "degraded"


def test_discovery_labels_changed_markets_and_keeps_a_hand_label():
    repo = Repository(":memory:")
    repo.migrate()
    wti = _meta(Venue.KALSHI, "KXWTI-26OCT-T100", title="WTI close above 100")
    sports = _meta(
        Venue.POLYMARKET, "sports", token_id="s", title="NBA champion", kind=MarketKind.OTHER,
    )
    fed = _meta(
        Venue.POLYMARKET, "fed", token_id="f",
        title="Will the Federal Reserve cut rates?", kind=MarketKind.PER_MEETING,
    )
    kalshi = FakeKalshi(list_markets=[wti])
    polymarket = FakePolymarket(search=[sports, fed])
    labeled = []

    def label_fn(markets):
        labeled.append([meta.market_id for meta in markets])
        return [
            (meta, _label(factor=Factor.RATES, yes_moves_factor="down"))
            for meta in markets
        ]

    settings = Settings(discovery=DiscoveryConfig(
        kalshi_series=["KXWTI"],
        polymarket_queries=["fed"],
        keywords=["fed", "federal reserve", "wti"],
    ))
    worker = _worker(repo, kalshi, polymarket, None, settings=settings, label_fn=label_fn)
    worker.discover()
    stored = {meta.market_id: label for meta, label in repo.markets()}
    assert "sports" not in stored
    assert stored["KXWTI-26OCT-T100"].label_source == "hand"
    assert stored["fed"].relevant is True
    worker.discover()
    assert len(labeled) == 1
    again = {meta.market_id: label for meta, label in repo.markets()}
    assert again["fed"].label_source == "hand"


def test_discovery_does_not_freeze_markets_the_labeler_has_not_tried_yet():
    repo = Repository(":memory:")
    repo.migrate()
    first = _meta(Venue.KALSHI, "KXWTI-A", title="WTI close above 90")
    second = _meta(Venue.KALSHI, "KXWTI-B", title="WTI close above 100")
    passes = {"n": 0}

    def label_fn(markets):
        passes["n"] += 1
        if passes["n"] == 1:
            label_fn.unattempted = {(second.venue, second.market_id)}
            return [(first, _label())]
        label_fn.unattempted = set()
        return [(meta, _label()) for meta in markets]

    settings = Settings(discovery=DiscoveryConfig(
        kalshi_series=["KXWTI"], polymarket_queries=[], keywords=["wti"],
    ))
    worker = _worker(repo, FakeKalshi(list_markets=[first, second]), FakePolymarket(), None,
                     settings=settings, label_fn=label_fn)
    assert worker.discover() is True
    stored = {meta.market_id for meta, _stored in repo.markets()}
    assert stored == {"KXWTI-A"}
    assert worker.discover() is False
    stored = {meta.market_id for meta, _stored in repo.markets()}
    assert stored == {"KXWTI-A", "KXWTI-B"}


def test_filings_tick_stores_overrides_for_a_new_filing():
    repo = Repository(":memory:")
    repo.migrate()
    ref = FilingRef(
        form="8-K",
        filed=date(2026, 10, 9),
        accession="0000027904-26-000035",
        primary_document="deltaairlinesannouncessept.htm",
        url="https://www.sec.gov/Archives/edgar/data/27904/000002790426000035/deltaairlinesannouncessept.htm",
    )
    edgar = FakeEdgar(recent_filings=[ref], fetch_document="Operating Income 1,454")
    worker = _worker(repo, FakeKalshi(), FakePolymarket(), None, edgar=edgar)
    worker.tick_filings()
    facts, flags = repo.latest_facts("DAL", datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert flags == []
    income = [fact for fact in facts if fact.metric == Metric.OPERATING_INCOME]
    assert income[0].value == 1454


def test_ten_q_xbrl_disagreement_sets_fact_conflict():
    repo = Repository(":memory:")
    repo.migrate()
    ref = FilingRef(
        form="10-Q",
        filed=date(2026, 10, 9),
        accession="0000027904-26-000038",
        primary_document="dal-20260930.htm",
        url="https://www.sec.gov/Archives/edgar/data/27904/000002790426000038/dal-20260930.htm",
    )
    xbrl = [{
        "tag": "Assets",
        "unit": "USD",
        "end": "2026-09-30",
        "value": 90_000_000_000,  # 90,000 million, far from the override 86,048
        "filed": "2026-10-09",
    }]
    edgar = FakeEdgar(recent_filings=[ref], fetch_document="body", xbrl_facts=xbrl)
    worker = _worker(repo, FakeKalshi(), FakePolymarket(), None, edgar=edgar)
    worker.tick_filings()
    _facts, flags = repo.latest_facts("DAL", datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert flags == ["FACT_CONFLICT"]


def test_macro_tick_stores_both_series():
    repo = Repository(":memory:")
    repo.migrate()
    from airscen.types import MacroObs
    dgs = [MacroObs(series_id="DGS10", obs_date=date(2026, 10, 8), value=5.22, realtime_start=date(2026, 10, 9))]
    oil = [MacroObs(series_id="DCOILWTICO", obs_date=date(2026, 10, 8), value=90.0, realtime_start=date(2026, 10, 9))]
    fred = FakeFred(observations={"DGS10": dgs, "DCOILWTICO": oil})
    worker = _worker(repo, FakeKalshi(), FakePolymarket(), None, fred=fred)
    worker.tick_macro()
    as_of = datetime(2026, 10, 11, tzinfo=timezone.utc)
    assert repo.macro_series("DGS10", date(2026, 1, 1), date(2026, 12, 31), as_of)[0].value == 5.22
    assert repo.macro_series("DCOILWTICO", date(2026, 1, 1), date(2026, 12, 31), as_of)[0].value == 90.0


def test_backfill_writes_history_and_reports_gaps():
    repo = Repository(":memory:")
    repo.migrate()
    covered = _meta(Venue.KALSHI, "KXWTI-26OCT-T100")
    empty = _meta(Venue.POLYMARKET, "fed", token_id="tok")
    repo.upsert_market(covered, _label())
    repo.upsert_market(empty, _label(factor=Factor.RATES, yes_moves_factor="up"))
    candle = _snap(Venue.KALSHI, covered.market_id).model_copy(update={
        "ts": datetime(2026, 7, 2, tzinfo=timezone.utc),
        "src": "live",
    })
    kalshi = FakeKalshi(candles=[candle])
    polymarket = FakePolymarket(history=[])
    worker = _worker(repo, kalshi, polymarket, None)
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 12, tzinfo=timezone.utc)
    gaps = worker.backfill(start, end)
    assert [meta.market_id for meta in gaps] == ["fed"]
    stored, _stale = repo.latest_snapshot(Venue.KALSHI, covered.market_id, end)
    assert stored is not None
    assert stored.src == "backfill_kalshi"
    assert kalshi.calls[0][0] == "candles"
    assert kalshi.calls[0][1][3] == 60


def test_polymarket_quotes_are_batched_at_500():
    repo = Repository(":memory:")
    repo.migrate()
    markets = [
        _meta(Venue.POLYMARKET, f"m{i}", token_id=f"t{i}", title="WTI")
        for i in range(501)
    ]
    for meta in markets:
        repo.upsert_market(meta, _label())
    polymarket = FakePolymarket(quotes=[])
    worker = _worker(repo, FakeKalshi(), polymarket, None)
    worker.tick_markets()
    quote_calls = [call for call in polymarket.calls if call[0] == "quotes"]
    assert [len(call[1][0]) for call in quote_calls] == [500, 1]


def test_build_adapters_exposes_each_source():
    adapters = build_adapters(Settings(sec_user_agent="airscen test@example.com"))
    assert callable(adapters.kalshi.quote)
    assert callable(adapters.polymarket.quotes)
    assert callable(adapters.edgar.recent_filings)
    assert callable(adapters.fred.observations)
    assert callable(adapters.gdelt.headlines)
    assert callable(adapters.alpaca.daily_bars)
