"""Resolved Fed, CPI, and WTI rows join to snapshots through the store."""

from datetime import datetime, timezone

from airscen.store import Repository
from airscen.types import MarketKind, MarketMeta, Snapshot, Venue


def test_calibration_rows_join_the_last_price_before_resolution():
    repo = Repository(":memory:")
    repo.migrate()
    market_id = "KXFEDDECISION-26JUL-H0"
    repo.upsert_market(MarketMeta(
        venue=Venue.KALSHI,
        market_id=market_id,
        title="Will the Federal Reserve Hike rates by 0bps at their July 2026 meeting?",
        kind=MarketKind.PER_MEETING,
        outcome="hold",
    ))
    repo.put_snapshots([
        Snapshot(
            venue=Venue.KALSHI,
            market_id=market_id,
            ts=datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc),
            bid=0.80,
            ask=0.84,
            last=0.82,
            src="live",
        ),
        Snapshot(
            venue=Venue.KALSHI,
            market_id=market_id,
            ts=datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc),
            bid=0.99,
            ask=1.0,
            last=1.0,
            src="live",
        ),
    ])
    rows = repo.calibration_rows()
    hold = next(row for row in rows if row["market_id"] == market_id)
    assert hold["outcome"] == "yes"
    assert hold["last_price"] == 0.82
    assert hold["snapshot_last"] == 0.82
    assert hold["snapshot_ts"] < hold["resolved_at"]
    assert {row["venue"] for row in rows} == {"kalshi"}
    assert any(row["market_id"].startswith("KXWTI-") for row in rows)
    assert any(row["market_id"].startswith("KXCPI") for row in rows)
