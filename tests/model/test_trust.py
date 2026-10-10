"""Quote cleaning and trust score. LLD section 6."""

from datetime import timedelta

import pytest

from airscen.trust import clean, trust_score
from airscen.types import Venue

from tests.model.conftest import AS_OF, make_market, make_snapshot

_OMITTED = object()


def _score(snapshot, cfg, close_time=_OMITTED, now=None):
    if close_time is _OMITTED:
        close_time = AS_OF + timedelta(days=2)
    market = make_market(
        venue=snapshot.venue,
        market_id=snapshot.market_id,
        close_time=close_time,
    )
    return trust_score(market, snapshot, AS_OF if now is None else now, cfg)


def test_each_component_and_the_weighted_sum(trust_cfg):
    perfect = make_snapshot()
    scored = _score(perfect, trust_cfg)
    assert scored.components == {"spread": 1.0, "depth": 1.0, "volume": 1.0, "time": 1.0}
    assert scored.score == pytest.approx(1.0)
    assert scored.mid == pytest.approx(0.50)
    assert scored.excluded_reason is None

    wider = _score(make_snapshot(bid=0.48, ask=0.52), trust_cfg)
    assert wider.components["spread"] == pytest.approx(0.6)
    assert wider.components["depth"] == pytest.approx(1.0)
    assert wider.score == pytest.approx(0.35 * 0.6 + 0.25 + 0.25 + 0.15)

    thin = _score(make_snapshot(bid_size=0, ask_size=0), trust_cfg)
    assert thin.components["depth"] == pytest.approx(0.0)
    assert thin.score == pytest.approx(0.35 + 0.15 + 0.25)

    quiet = _score(make_snapshot(volume_24h=0), trust_cfg)
    assert quiet.components["volume"] == pytest.approx(0.0)

    missing_size = _score(make_snapshot(bid_size=None, ask_size=5000), trust_cfg)
    assert missing_size.components["depth"] == pytest.approx(0.0)
    missing_volume = _score(make_snapshot(volume_24h=None), trust_cfg)
    assert missing_volume.components["volume"] == pytest.approx(0.0)

    one_day = _score(make_snapshot(), trust_cfg, close_time=AS_OF + timedelta(days=1))
    assert one_day.components["time"] == pytest.approx(0.5)
    no_close = _score(make_snapshot(), trust_cfg, close_time=None)
    # make_market drops None only if we pass it; close_time=None must stick.
    assert no_close.components["time"] == pytest.approx(0.0)

    past_close = _score(make_snapshot(), trust_cfg, close_time=AS_OF - timedelta(days=1))
    assert past_close.components["time"] == pytest.approx(0.0)

    deep = _score(make_snapshot(volume_24h=10**9), trust_cfg)
    assert deep.components["volume"] == pytest.approx(1.0)


def test_polymarket_volume_uses_its_own_reference(trust_cfg):
    quote = make_snapshot(venue=Venue.POLYMARKET, market_id="POLY", volume_24h=100_000)
    scored = _score(quote, trust_cfg)
    assert scored.components["volume"] == pytest.approx(1.0)

    smaller = make_snapshot(venue=Venue.POLYMARKET, market_id="POLY", volume_24h=10_000)
    partial = _score(smaller, trust_cfg)
    assert 0 < partial.components["volume"] < 1


def test_weights_come_from_config(trust_cfg):
    trust_cfg["weights"] = {"spread": 1.0, "depth": 0.0, "volume": 0.0, "time": 0.0}
    scored = _score(make_snapshot(bid=0.48, ask=0.52), trust_cfg)
    assert scored.components["spread"] == pytest.approx(0.6)
    assert scored.score == pytest.approx(0.6)


def test_floor_is_inclusive_and_a_wide_market_is_excluded(trust_cfg):
    trust_cfg["weights"] = {"spread": 0.40, "depth": 0.0, "volume": 0.0, "time": 0.0}
    at_floor = _score(make_snapshot(), trust_cfg)
    assert at_floor.score == pytest.approx(0.40)
    assert at_floor.excluded_reason is None

    empty = _score(
        make_snapshot(bid=0.40, ask=0.50, bid_size=0, ask_size=0, volume_24h=0),
        trust_cfg,
        close_time=None,
    )
    assert empty.score < 0.40
    assert empty.excluded_reason == "below_floor"


def test_crossed_and_out_of_range_quotes_are_dropped():
    now = AS_OF
    assert clean(make_snapshot(bid=0.60, ask=0.40), now) is None
    assert clean(make_snapshot(bid=None), now) is None
    assert clean(make_snapshot(ask=None), now) is None
    assert clean(make_snapshot(last=1.2), now) is None
    assert clean(make_snapshot(bid=-0.01), now) is None
    assert clean(make_snapshot(ask=1.01), now) is None
    assert clean(make_snapshot(bid_size=-1), now) is None
    assert clean(make_snapshot(ask_size=-5), now) is None

    kept = make_snapshot(bid=0.42, ask=0.42, last=None)
    assert clean(kept, now) == kept
    stale = make_snapshot(ts=now - timedelta(hours=2))
    assert clean(stale, now) == stale


def test_stale_after_300_seconds_and_the_default_when_config_omits_it(trust_cfg):
    fresh = _score(make_snapshot(ts=AS_OF - timedelta(seconds=300)), trust_cfg)
    assert fresh.excluded_reason is None
    assert fresh.score == pytest.approx(1.0)

    stale = _score(make_snapshot(ts=AS_OF - timedelta(seconds=301)), trust_cfg)
    assert stale.excluded_reason == "stale"
    assert stale.score == pytest.approx(1.0)

    poor = make_snapshot(
        ts=AS_OF - timedelta(seconds=301),
        bid=0.0,
        ask=0.10,
        bid_size=0,
        ask_size=0,
        volume_24h=0,
    )
    both = _score(poor, trust_cfg, close_time=None)
    assert both.excluded_reason == "stale"
    assert both.score < 0.40

    del trust_cfg["stale_after_s"]
    defaulted = _score(make_snapshot(ts=AS_OF - timedelta(seconds=301)), trust_cfg)
    assert defaulted.excluded_reason == "stale"


def test_backfill_score_is_the_fixed_cap(trust_cfg):
    rich = make_snapshot(src="backfill_kalshi", bid_size=10**6, ask_size=10**6, volume_24h=10**6)
    scored = _score(rich, trust_cfg)
    assert scored.score == pytest.approx(0.50)
    assert scored.components["spread"] == pytest.approx(1.0)
    assert scored.excluded_reason is None

    thin = make_snapshot(
        src="backfill_polymarket",
        venue=Venue.POLYMARKET,
        market_id="POLY",
        bid_size=None,
        ask_size=None,
        volume_24h=None,
    )
    capped = _score(thin, trust_cfg, close_time=None)
    assert capped.score == pytest.approx(0.50)
    assert capped.components["depth"] == pytest.approx(0.0)

    old = make_snapshot(src="backfill_kalshi", ts=AS_OF - timedelta(seconds=301))
    stale = _score(old, trust_cfg)
    assert stale.score == pytest.approx(0.50)
    assert stale.excluded_reason == "stale"
