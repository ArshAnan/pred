"""C10: peers use the same request, fuel curve and rate states as the focal company, but their own
facts and their own baseline WTI; PERIOD_MISMATCH when their period differs (LLD section 6 "Peers").
Peer data here is SYNTHETIC (seed_peer copies Delta's illustrative facts under another ticker)."""

from datetime import date

import pytest

from airscen.config import Settings
from airscen.service import RunService
from airscen.types import RunRequest
from fake_store import AS_OF, FakeStore, seed_peer, seed_worked_example


def run(store, include_peers=True):
    svc = RunService(repo=store, settings=Settings(), clock=lambda: AS_OF)
    return svc.create_run(RunRequest(ticker="DAL", note_size_usd_m=1000, tenor_years=7, coupon_spread_bp=150,
                                     coverage_threshold=6.0, include_peers=include_peers))


@pytest.fixture
def store():
    s = FakeStore()
    seed_worked_example(s)
    return s


def test_a_peer_with_the_same_facts_gets_the_same_answer(store):
    seed_peer(store, "UAL")
    r = run(store)
    assert [p.ticker for p in r.peers] == ["UAL"]
    peer = r.peers[0]
    assert peer.summary == r.summary            # same request, same fuel curve and rate states
    assert peer.period_end == date(2026, 9, 30)
    assert "PERIOD_MISMATCH" not in peer.flags


def test_a_peer_uses_its_own_baseline_wti(store):
    seed_peer(store, "UAL", period_end=date(2026, 6, 30), wti=80.0)
    peer = run(store).peers[0]
    # W* = W_bar + 42 (OI0 - theta NI) / (kappa G): same facts, W_bar 80 instead of 85, so 5 lower
    assert peer.summary.breakeven_wti_hold == pytest.approx(103.16 - 5, abs=0.01)
    assert peer.summary.p_breach > 0.297         # a lower breakeven is more likely to be crossed


def test_a_peer_with_another_period_is_flagged(store):
    seed_peer(store, "AAL", period_end=date(2026, 6, 30))
    r = run(store)
    assert r.peers[0].ticker == "AAL" and r.peers[0].period_end == date(2026, 6, 30)
    assert "PERIOD_MISMATCH" in r.peers[0].flags
    assert "PERIOD_MISMATCH" not in r.flags      # the focal run itself is not mismatched


def test_peers_follow_the_configured_order(store):
    seed_peer(store, "AAL")
    seed_peer(store, "UAL")
    assert [p.ticker for p in run(store).peers] == ["UAL", "AAL"]  # config.yaml: DAL peers [UAL, AAL]


def test_a_peer_missing_inputs_is_left_out_and_the_run_still_succeeds(store):
    seed_peer(store, "UAL", skip_metrics=("variable_debt_share",))
    r = run(store)
    assert r.peers == [] and r.summary.p_breach == pytest.approx(0.297, abs=0.0005)


def test_peers_are_skipped_when_not_requested(store):
    seed_peer(store, "UAL")
    assert run(store, include_peers=False).peers == []


def test_peer_flags_are_its_own(store):
    seed_peer(store, "UAL")
    peer = run(store).peers[0]
    assert "VARIABLE_SHARE_STALE" not in peer.flags  # the synthetic peer's share is from the same period
    assert "CRUDE_ONLY" not in peer.flags            # run-level assumptions stay on the run
