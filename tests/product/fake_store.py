"""TEMPORARY in-memory stand-in for lane A's store.Repository, used by lane C tests until lane A's
store (or their tests/fakes/fake_repo.py, CP0_DECISIONS R5) is on main. Replace it then.

Return shapes mirror arnav-lane-A's store.py (A2/A3):
  latest_facts(company, as_of)                  -> (list[Fact], set[str] flags)
  latest_snapshot(venue, market_id, as_of)      -> (Snapshot | None, stale: bool)
  markets(factor=None)                          -> list[MarketView] (meta and label only)
  macro_series(series_id, start, end, as_of)    -> list[MacroObs]
  data_version(as_of)                           -> str
  save_run(run), get_run(run_id) -> RunResult | None

`seed_worked_example` loads the LLD section 6 worked example. ILLUSTRATIVE: market ids, quotes,
the WTI ladder, DGS10 = 4.00 (so the coupon is 5.5% at a 150 bp spread) and W_bar = 85 are
assumptions, not live data. Operating facts are Delta's September-quarter 2026 8-K.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from airscen.types import (
    Fact, Factor, MacroObs, MarketKind, MarketLabel, MarketMeta, MarketView, RunResult, Snapshot, Venue,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "worked_example.json"
AS_OF = datetime(2026, 10, 9, 14, 30, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self, stale_after_s: int = 300):
        self.stale_after_s = stale_after_s
        self.facts: list[Fact] = []
        self.market_views: list[MarketView] = []
        self.snapshots: list[Snapshot] = []
        self.macro: list[MacroObs] = []
        self.runs: dict[str, RunResult] = {}

    # ---- reads ----
    def latest_facts(self, company, as_of):
        visible = [f for f in self.facts if f.company == company and f.validated and f.filed_at <= as_of]
        best: dict[tuple, Fact] = {}
        for f in sorted(visible, key=lambda f: f.filed_at):
            best[(f.metric, f.basis)] = f
        return list(best.values()), set()

    def latest_snapshot(self, venue, market_id, as_of):
        rows = [s for s in self.snapshots if s.venue == venue and s.market_id == market_id and s.ts <= as_of]
        if not rows:
            return None, False
        snap = max(rows, key=lambda s: s.ts)
        return snap, (as_of - snap.ts).total_seconds() > self.stale_after_s

    def markets(self, factor=None):
        return [v for v in self.market_views if factor is None or (v.label and v.label.factor == factor)]

    def macro_series(self, series_id, start, end, as_of):
        day = as_of.astimezone(timezone.utc).date()
        rows = [o for o in self.macro if o.series_id == series_id and start <= o.obs_date <= end
                and o.obs_date <= day and o.realtime_start <= day]
        return sorted(rows, key=lambda o: o.obs_date)

    def data_version(self, as_of):
        visible = [f.model_dump_json() for f in self.facts if f.filed_at <= as_of]
        visible += [s.model_dump_json() for s in self.snapshots if s.ts <= as_of]
        return hashlib.sha256(json.dumps(sorted(visible)).encode()).hexdigest()[:8]

    # ---- writes ----
    def save_run(self, run):
        self.runs[run.run_id] = run

    def get_run(self, run_id):
        return self.runs.get(run_id)


# ---- worked example seed ----

def _fact(metric: str, value: float, unit: str, period_end: date, filed_at: datetime) -> Fact:
    return Fact(company="DAL", metric=metric, value=value, unit=unit, basis="GAAP", period_end=period_end,
                filed_at=filed_at, source_url="https://www.sec.gov/Archives/edgar/data/27904/000002790426000035/"
                "deltaairlinesannouncessept.htm", extraction="override", validated=True)


def _market(store: FakeStore, market_id: str, kind: MarketKind, factor: Factor, p: float, close: datetime,
            group_key: str, outcome: str | None = None, threshold: float | None = None) -> None:
    meta = MarketMeta(venue=Venue.KALSHI, market_id=market_id, title=market_id, close_time=close, kind=kind,
                      outcome=outcome, threshold=threshold, group_key=group_key,
                      threshold_unit="usd_per_bbl" if threshold is not None else None)
    label = MarketLabel(relevant=True, factor=factor, yes_moves_factor="up", label_source="hand",
                        mechanism="illustrative test market")
    store.market_views.append(MarketView(meta=meta, label=label))
    # Tight, deep, liquid quote whose mid is exactly p, recorded one minute before AS_OF.
    store.snapshots.append(Snapshot(venue=Venue.KALSHI, market_id=market_id, ts=AS_OF - timedelta(minutes=1),
                                    bid=round(p - 0.005, 4), ask=round(p + 0.005, 4), bid_size=1000,
                                    ask_size=1000, volume_24h=10_000))


def seed_worked_example(store: FakeStore, *, skip_metrics: tuple[str, ...] = (), with_macro: bool = True) -> dict:
    ex = json.loads(FIXTURE.read_text(encoding="utf-8"))
    f = ex["inputs"]["facts"]
    q3, q2 = date(2026, 9, 30), date(2026, 6, 30)
    filed_q3 = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    filed_q2 = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    facts = [
        ("operating_income", f["operating_income_usd_m"], "usd_m", q3, filed_q3),
        ("net_interest_expense", f["net_interest_expense_usd_m"], "usd_m", q3, filed_q3),
        ("fuel_gallons_m", f["fuel_gallons_m"], "gallons_m", q3, filed_q3),
        ("debt_total", f["debt_total_usd_m"], "usd_m", q3, filed_q3),
        ("variable_debt_share", f["variable_debt_share"], "fraction", q2, filed_q2),  # June 30 share
        ("liquidity", f["liquidity_usd_m"], "usd_m", q3, filed_q3),
        ("debt_current", f["debt_current_usd_m"], "usd_m", q3, filed_q3),
    ]
    store.facts += [_fact(*row) for row in facts if row[0] not in skip_metrics]

    rates = ex["inputs"]["rate_quotes"]
    meeting_close = AS_OF + timedelta(days=20)
    for outcome in ("hold", "hike", "cut"):
        _market(store, f"FED-ILLUSTRATIVE-{outcome.upper()}", MarketKind.PER_MEETING, Factor.RATES,
                rates[outcome], meeting_close, "FED-ILLUSTRATIVE", outcome=outcome)
    ladder_close = AS_OF + timedelta(days=60)
    for t in ex["inputs"]["ladder"]["thresholds"]:
        _market(store, f"WTI-ILLUSTRATIVE-{t['k']}", MarketKind.CLOSE_ABOVE, Factor.FUEL, t["p"], ladder_close,
                "WTI-ILLUSTRATIVE", threshold=float(t["k"]))

    if with_macro:
        store.macro.append(MacroObs(series_id="DGS10", obs_date=date(2026, 10, 8), value=4.0,
                                    realtime_start=date(2026, 10, 9)))
        day = date(2026, 7, 1)
        while day <= q3:  # weekday WTI closes at the illustrative W_bar
            if day.weekday() < 5:
                store.macro.append(MacroObs(series_id="DCOILWTICO", obs_date=day, value=float(ex["inputs"]["wti_ref"]),
                                            realtime_start=day + timedelta(days=1)))
            day += timedelta(days=1)
    return ex
