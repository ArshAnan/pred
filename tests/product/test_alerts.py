"""C11: alert rules and delivery (LLD section 6 "Alert rules"). Done when state-machine, hysteresis,
dedup and Slack-failure tests pass with fake Slack. Market data is the ILLUSTRATIVE worked example."""

from datetime import timedelta

import httpx
import pytest

from airscen.alerts import AlertEngine, SlackSender
from airscen.config import Settings
from airscen.service import RunService
from airscen.types import DataError, RunRequest, Snapshot, SourceStatus, WorkerStatus
from fake_store import AS_OF, FakeStore, seed_worked_example
from tests.fakes.fake_slack import FakeSlack

KEY = "DAL:BREACH_FLIP:-"
HOOK = "https://hooks.slack.test/services/T/B/x"


class Clock:
    def __init__(self, t=AS_OF):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def store():
    s = FakeStore()
    seed_worked_example(s)
    return s


@pytest.fixture
def slack():
    return FakeSlack()


def engine(store, slack, settings=None, clock=None, headlines=None, status_fn=None, sleeps=None):
    settings = settings or Settings(slack_webhook_url=HOOK)
    clock = clock or Clock()
    service = RunService(repo=store, settings=settings, clock=clock)
    sender = SlackSender(settings.slack_webhook_url, post=slack.post, sleep=(sleeps.append if sleeps is not None else lambda s: None))
    return AlertEngine(repo=store, settings=settings, service=service, slack=sender, clock=clock,
                       headlines=headlines, status_fn=status_fn)


@pytest.fixture
def base_run(store):
    return RunService(repo=store, settings=Settings(), clock=Clock()).create_run(
        RunRequest(ticker="DAL", note_size_usd_m=1000, tenor_years=7, coupon_spread_bp=150, coverage_threshold=6.0))


def with_p(run, p, minutes=0):
    return run.model_copy(update={"summary": run.summary.model_copy(update={"p_breach": p}),
                                  "run_id": f"run_p{p}_{minutes}"})


def step(eng, base_run, prev_p, p, minutes=0):
    """Advance the clock and evaluate one BREACH_FLIP observation; deliver what fires."""
    eng.clock.t = AS_OF + timedelta(minutes=minutes)
    prev = with_p(base_run, prev_p, minutes) if prev_p is not None else None
    return [eng.deliver(a) for a in eng.evaluate("DAL", with_p(base_run, p, minutes), prev)]


# ---- BREACH_FLIP state machine ----

def test_breach_flip_fires_when_p_breach_rises_to_the_threshold(store, slack, base_run):
    eng = engine(store, slack)
    assert eng.state(KEY) == "ARMED"
    [alert] = step(eng, base_run, 0.15, 0.24)
    assert (alert.rule, alert.severity, alert.status, alert.company) == ("BREACH_FLIP", "high", "sent", "DAL")
    assert alert.dedup_key == KEY and alert.run_id == "run_p0.24_0"
    assert alert.payload["before"] == 0.15 and alert.payload["after"] == 0.24
    assert eng.state(KEY) == "COOLDOWN"
    assert store.saved_alerts[alert.alert_id].status == "sent"
    assert "BREACH_FLIP" in slack.texts[0] and "DAL" in slack.texts[0]


@pytest.mark.parametrize("prev_p,p", [(0.25, 0.26), (None, 0.30), (0.15, 0.19), (0.15, None)])
def test_breach_flip_needs_a_crossing_from_below(store, slack, base_run, prev_p, p):
    eng = engine(store, slack)
    assert step(eng, base_run, prev_p, p) == []
    assert eng.state(KEY) == "ARMED"


def test_dedup_no_second_alert_while_cooling_down(store, slack, base_run):
    eng = engine(store, slack)
    step(eng, base_run, 0.15, 0.24, minutes=0)
    assert step(eng, base_run, 0.19, 0.25, minutes=5) == []   # crosses again within the cooldown
    assert len(slack.posts) == 1


def test_hysteresis_re_arms_only_below_threshold_minus_hysteresis(store, slack, base_run):
    eng = engine(store, slack)
    step(eng, base_run, 0.15, 0.24, minutes=0)
    assert step(eng, base_run, 0.24, 0.19, minutes=61) == [] and eng.state(KEY) == "COOLDOWN"  # 0.19 is not < 0.18
    assert step(eng, base_run, 0.19, 0.21, minutes=62) == []                                    # so no new alert
    assert step(eng, base_run, 0.21, 0.17, minutes=63) == [] and eng.state(KEY) == "ARMED"      # cleared: re-armed
    assert len(step(eng, base_run, 0.17, 0.22, minutes=64)) == 1                                # fires again


def test_re_arm_waits_for_the_cooldown_even_if_the_condition_clears(store, slack, base_run):
    eng = engine(store, slack)
    step(eng, base_run, 0.15, 0.24, minutes=0)
    step(eng, base_run, 0.24, 0.10, minutes=30)
    assert eng.state(KEY) == "COOLDOWN"
    step(eng, base_run, 0.10, 0.10, minutes=61)
    assert eng.state(KEY) == "ARMED"


# ---- Slack delivery ----

def test_slack_failure_after_three_attempts_marks_the_alert_failed(store, base_run):
    slack, sleeps = FakeSlack(500, 503, 500), []
    eng = engine(store, slack, sleeps=sleeps)
    [alert] = step(eng, base_run, 0.15, 0.24)
    assert alert.status == "failed" and len(slack.posts) == 3 and sleeps == [1.0, 4.0]
    assert store.saved_alerts[alert.alert_id].status == "failed"  # stays in the feed, marked not delivered
    assert eng.state(KEY) == "COOLDOWN"                            # no retry storm on the next tick


def test_slack_success_on_a_retry(store, base_run):
    slack, sleeps = FakeSlack(httpx.ConnectError("refused"), 200), []
    eng = engine(store, slack, sleeps=sleeps)
    [alert] = step(eng, base_run, 0.15, 0.24)
    assert alert.status == "sent" and len(slack.posts) == 2 and sleeps == [1.0]


def test_without_a_webhook_alerts_are_saved_as_failed(store, slack, base_run):
    eng = engine(store, slack, settings=Settings(slack_webhook_url=""))
    [alert] = step(eng, base_run, 0.15, 0.24)
    assert alert.status == "failed" and slack.posts == []


# ---- headlines (best effort) ----

def test_up_to_three_headlines_are_attached(store, slack, base_run):
    calls = []

    def headlines(query, before, hours, n):
        calls.append((query, before, hours, n))
        return [f"headline {i}" for i in range(5)]
    eng = engine(store, slack, headlines=headlines)
    [alert] = step(eng, base_run, 0.15, 0.24)
    assert alert.payload["headlines"] == ["headline 0", "headline 1", "headline 2"]
    assert calls[0][1] == AS_OF and calls[0][3] == 3


def test_a_headline_failure_never_blocks_the_alert(store, slack, base_run):
    def headlines(*a):
        raise RuntimeError("GDELT down")
    eng = engine(store, slack, headlines=headlines)
    [alert] = step(eng, base_run, 0.15, 0.24)
    assert alert.status == "sent" and alert.payload["headlines"] == []


# ---- DRIVER_MOVE ----

def move_market(store, market_id, mids):
    """Replace one market's quotes with a series ending one minute before AS_OF (ILLUSTRATIVE)."""
    venue = next(v.meta.venue for v in store.market_views if v.meta.market_id == market_id)
    store.snapshots = [s for s in store.snapshots if s.market_id != market_id]
    n = len(mids)
    for i, mid in enumerate(mids):
        store.snapshots.append(Snapshot(venue=venue, market_id=market_id, ts=AS_OF - timedelta(minutes=1 + (n - 1 - i) * 10),
                                        bid=round(mid - 0.005, 4), ask=round(mid + 0.005, 4), bid_size=1000,
                                        ask_size=1000, volume_24h=10_000))


def test_driver_move_fires_per_market_on_a_10_point_move(store, slack, base_run):
    move_market(store, "WTI-ILLUSTRATIVE-90", [0.62, 0.66, 0.74])   # +12 points in 20 minutes
    move_market(store, "WTI-ILLUSTRATIVE-100", [0.35, 0.37, 0.40])  # +5: below the 10-point rule
    eng = engine(store, slack)
    alerts = eng.driver_moves("DAL", base_run, AS_OF)
    assert [a.dedup_key for a in alerts] == ["DAL:DRIVER_MOVE:WTI-ILLUSTRATIVE-90"]
    a = alerts[0]
    assert a.severity == "medium" and a.payload["before"] == pytest.approx(0.62) and a.payload["after"] == pytest.approx(0.74)
    eng.deliver(a)
    assert eng.driver_moves("DAL", base_run, AS_OF + timedelta(minutes=5)) == []   # same market: deduped


def test_driver_move_ignores_moves_older_than_the_window(store, slack, base_run):
    move_market(store, "WTI-ILLUSTRATIVE-90", [0.50] + [0.74] * 7)   # the jump was 70+ minutes ago
    assert engine(store, slack).driver_moves("DAL", base_run, AS_OF) == []


# ---- DATA_STALE ----

def test_data_stale_fires_once_per_incident(store, slack, base_run):
    sources = {"kalshi": SourceStatus(status="degraded", last_ok_at=AS_OF - timedelta(minutes=20), fail_streak=20),
               "fred": SourceStatus(status="ok", last_ok_at=AS_OF)}
    eng = engine(store, slack, status_fn=lambda: (sources, WorkerStatus()))
    [a] = eng.data_stale("DAL", base_run, AS_OF)
    assert (a.rule, a.severity, a.dedup_key) == ("DATA_STALE", "low", "DAL:DATA_STALE:kalshi")
    eng.deliver(a)
    assert eng.data_stale("DAL", base_run, AS_OF + timedelta(hours=3)) == []          # still the same incident
    sources["kalshi"] = SourceStatus(status="ok", last_ok_at=AS_OF + timedelta(hours=4))
    assert eng.data_stale("DAL", base_run, AS_OF + timedelta(hours=4)) == []          # recovered: re-armed
    sources["kalshi"] = SourceStatus(status="down", last_ok_at=AS_OF + timedelta(hours=4))
    assert len(eng.data_stale("DAL", base_run, AS_OF + timedelta(hours=5))) == 1      # a new incident


def test_a_short_outage_is_not_stale_yet(store, slack, base_run):
    sources = {"kalshi": SourceStatus(status="degraded", last_ok_at=AS_OF - timedelta(minutes=10))}
    eng = engine(store, slack, status_fn=lambda: (sources, WorkerStatus()))
    assert eng.data_stale("DAL", base_run, AS_OF) == []


# ---- tick: the worker's after_market_tick ----

def test_tick_runs_watched_companies_and_fires_when_the_answer_flips(store, slack):
    low = Settings(slack_webhook_url=HOOK)  # watch_inputs threshold 3.0: p_breach about 0.074
    eng = engine(store, slack, settings=low)
    eng.tick(AS_OF)
    assert slack.posts == [] and len(store.saved) == 1
    # Demo lever: a higher threshold in config.yaml alerts.watch_inputs flips the answer.
    eng.settings = Settings(slack_webhook_url=HOOK, alerts={"watch_inputs": {"coverage_threshold": 6.0}})
    eng.service.settings = eng.settings
    eng.tick(AS_OF)
    flips = [a for a in store.saved_alerts.values() if a.rule == "BREACH_FLIP"]
    assert len(flips) == 1 and flips[0].status == "sent"
    assert flips[0].payload["before"] < 0.20 <= flips[0].payload["after"]


def test_tick_never_raises(store, slack):
    eng = engine(store, slack)
    eng.service.create_run = lambda req: (_ for _ in ()).throw(DataError("missing", code="E_MISSING_FACT"))
    eng.tick(AS_OF)  # logged, not raised: the worker keeps running
    assert slack.posts == []
