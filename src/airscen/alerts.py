"""Decision-anchored alerts (LLD section 6 "Alert rules"; C11).

Every alert key (company:rule:market_id) moves ARMED -> TRIGGERED -> SENDING -> COOLDOWN and goes back
to ARMED only once the cooldown is over AND its condition has cleared. That one state machine gives
dedup (a key that is not ARMED cannot fire) and hysteresis (BREACH_FLIP clears below threshold - 0.02).
State lives in memory in the single API process (DD-2); a restart re-arms everything.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from airscen.types import Alert, RunRequest, RunResult

log = logging.getLogger("airscen.alerts")

SEVERITY = {"BREACH_FLIP": "high", "DRIVER_MOVE": "medium", "DATA_STALE": "low"}
SLACK_WAITS_S = (1.0, 4.0)           # 3 attempts in total
SLACK_TIMEOUT_S = 10.0
STALE_AFTER = timedelta(minutes=15)
MAX_HEADLINES = 3
HEADLINE_HOURS = 24


class SlackSender:
    """Incoming-webhook delivery: 3 attempts, waiting 1 s then 4 s; a 2xx means delivered."""

    def __init__(self, webhook_url: str, post: Callable[..., httpx.Response] = httpx.post,
                 sleep: Callable[[float], None] = time.sleep):
        self.webhook_url, self._post, self._sleep = webhook_url, post, sleep

    def send(self, text: str) -> bool:
        if not self.webhook_url:
            return False  # no webhook configured: saved as failed and shown in the feed
        for attempt, wait in enumerate((0.0, *SLACK_WAITS_S)):
            if attempt:
                self._sleep(wait)
            try:
                if self._post(self.webhook_url, json={"text": text}, timeout=SLACK_TIMEOUT_S).is_success:
                    return True
            except httpx.HTTPError as e:
                log.warning("slack attempt %d failed: %s", attempt + 1, type(e).__name__)
        return False


class AlertEngine:
    def __init__(self, repo: Any, settings: Any, service: Any, slack: SlackSender,
                 clock: Callable[[], datetime] | None = None,
                 headlines: Callable[..., list] | None = None,
                 status_fn: Callable[[], tuple[dict, Any]] | None = None):
        self.repo, self.settings, self.service, self.slack = repo, settings, service, slack
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.headlines, self.status_fn = headlines, status_fn
        self._state: dict[str, str] = {}
        self._cooldown_until: dict[str, datetime] = {}
        self._previous: dict[str, RunResult] = {}

    def state(self, key: str) -> str:
        return self._state.get(key, "ARMED")

    # ---- rules ----

    def evaluate(self, company: str, run: RunResult, previous: RunResult | None) -> list[Alert]:
        """BREACH_FLIP: p_breach rises to the threshold or more from below."""
        cfg = self.settings.alerts
        key = f"{company}:BREACH_FLIP:-"
        p = run.summary.p_breach
        before = previous.summary.p_breach if previous is not None else None
        fire = p is not None and before is not None and before < cfg.breach_threshold <= p
        clear = p is not None and p < cfg.breach_threshold - cfg.hysteresis
        if not self._step(key, fire, clear, self.clock()):
            return []
        return [self._alert("BREACH_FLIP", company, key, run, {
            "before": before, "after": p, "threshold": cfg.breach_threshold,
            "drivers": self._drivers(run.as_of), "query": f"{company} airline jet fuel oil",
        })]

    def driver_moves(self, company: str, run: RunResult, now: datetime) -> list[Alert]:
        """DRIVER_MOVE: a trusted market feeding the run moves >= driver_move_pp within window_min."""
        cfg = self.settings.alerts
        alerts = []
        for q in self.service.driver_quotes(now):
            meta = q.meta
            series = self.repo.snapshot_series(meta.venue, meta.market_id, now - timedelta(minutes=cfg.window_min), now, now)
            mids = [(s.bid + s.ask) / 2 for s in series if s.bid is not None and s.ask is not None]
            move_pp = (max(mids) - min(mids)) * 100 if len(mids) >= 2 else 0.0
            key = f"{company}:DRIVER_MOVE:{meta.market_id}"
            if self._step(key, move_pp >= cfg.driver_move_pp, move_pp < cfg.driver_move_pp, now):
                alerts.append(self._alert("DRIVER_MOVE", company, key, run, {
                    "market_id": meta.market_id, "venue": meta.venue.value, "title": meta.title,
                    "factor": q.label.factor.value if q.label.factor else None, "mechanism": q.label.mechanism,
                    "before": mids[0], "after": mids[-1], "move_pp": round(move_pp, 2),
                    "window_min": cfg.window_min, "query": meta.title,
                }, ts=now))
        return alerts

    def data_stale(self, company: str, run: RunResult | None, now: datetime) -> list[Alert]:
        """DATA_STALE: a source unhealthy for more than 15 minutes; once per incident."""
        if self.status_fn is None:
            return []
        try:
            sources, _ = self.status_fn()
        except Exception:
            log.exception("worker status failed")
            return []
        alerts = []
        for name, s in sorted(sources.items()):
            stale = s.status != "ok" and (s.last_ok_at is None or now - s.last_ok_at > STALE_AFTER)
            key = f"{company}:DATA_STALE:{name}"
            if self._step(key, stale, s.status == "ok", now):
                alerts.append(self._alert("DATA_STALE", company, key, run, {
                    "source": name, "status": s.status, "fail_streak": s.fail_streak,
                    "last_ok_at": s.last_ok_at.isoformat() if s.last_ok_at else None,
                }, ts=now, with_headlines=False))
        return alerts

    # ---- delivery ----

    def deliver(self, alert: Alert) -> Alert:
        """SENDING -> Slack -> COOLDOWN. A failed delivery stays in the feed as `failed`."""
        self._state[alert.dedup_key] = "SENDING"
        self.repo.save_alert(alert)
        sent = self.slack.send(format_alert(alert))
        done = alert.model_copy(update={"status": "sent" if sent else "failed"})
        self.repo.save_alert(done)
        self._state[alert.dedup_key] = "COOLDOWN"
        self._cooldown_until[alert.dedup_key] = alert.ts + timedelta(
            minutes=0 if alert.rule == "DATA_STALE" else self.settings.alerts.cooldown_min)
        return done

    def tick(self, now: datetime, coverage_threshold: float | None = None) -> list[Alert]:
        """The worker's after_market_tick: evaluate every watched company. Never raises.

        coverage_threshold overrides alerts.watch_inputs for this tick only; POST /admin/alerts/tick
        uses it to force a BREACH_FLIP in the demo. Returns the alerts delivered on this tick.
        """
        delivered = []
        for company in self.settings.alerts.watch:
            try:
                w = self.settings.alerts.watch_inputs
                run = self.service.create_run(RunRequest(
                    ticker=company, as_of=now, note_size_usd_m=w.note_size_usd_m, tenor_years=w.tenor_years,
                    coupon_spread_bp=w.coupon_spread_bp, fuel_passthrough=w.fuel_passthrough, include_peers=False,
                    coverage_threshold=coverage_threshold if coverage_threshold is not None else w.coverage_threshold))
                alerts = self.evaluate(company, run, self._previous.get(company))
                alerts += self.driver_moves(company, run, now) + self.data_stale(company, run, now)
                self._previous[company] = run
                delivered += [self.deliver(a) for a in alerts]
            except Exception:
                log.exception("alert evaluation failed for %s", company)
        return delivered

    def send_test(self) -> bool:
        """POST /admin/alerts/test: one plain message through the same Slack path."""
        return self.slack.send("AirScen test alert: the Slack webhook works. No action needed.")

    # ---- helpers ----

    def _step(self, key: str, fire: bool, clear: bool, now: datetime) -> bool:
        """Advance one key's state; True when it moves ARMED -> TRIGGERED (an alert must be sent)."""
        state = self.state(key)
        if state == "COOLDOWN" and now >= self._cooldown_until.get(key, now) and clear:
            self._state[key] = state = "ARMED"
        if state == "ARMED" and fire:
            self._state[key] = "TRIGGERED"
            return True
        return False

    def _alert(self, rule: str, company: str, key: str, run: RunResult | None, payload: dict,
               ts: datetime | None = None, with_headlines: bool = True) -> Alert:
        ts = ts or self.clock()
        query = payload.pop("query", company)
        payload = {"rule": rule, "company": company, "company_name": self._name(company),
                   "as_of": (run.as_of if run else ts).isoformat(), "run_id": run.run_id if run else None,
                   "decision": _decision(run), **payload,
                   "headlines": self._headlines(query, ts) if with_headlines else []}
        return Alert(alert_id=f"alert_{uuid.uuid4().hex[:12]}", ts=ts, company=company,
                     run_id=run.run_id if run else None, rule=rule, dedup_key=key,
                     severity=SEVERITY[rule], payload=payload, status="pending")

    def _name(self, company: str) -> str:
        return next((c.name for c in self.settings.companies if c.ticker == company), company)

    def _headlines(self, query: str, before: datetime) -> list:
        if self.headlines is None:
            return []
        try:
            return list(self.headlines(query, before, HEADLINE_HOURS, MAX_HEADLINES))[:MAX_HEADLINES]
        except Exception as e:  # best effort (LLD): never blocks the alert
            log.warning("headlines failed: %s", type(e).__name__)
            return []

    def _drivers(self, as_of: datetime) -> list[dict]:
        try:
            return [{"market_id": q.meta.market_id, "venue": q.meta.venue.value, "mid": q.trust.mid,
                     "trust": round(q.trust.score, 3)} for q in self.service.driver_quotes(as_of)]
        except Exception:
            log.exception("driver lookup failed")
            return []


def _decision(run: RunResult | None) -> dict | None:
    """The question an alert is about, so the message can say it in plain words."""
    if run is None:
        return None
    return {"note_size_usd_m": run.inputs.note_size_usd_m, "tenor_years": run.inputs.tenor_years,
            "coverage_threshold": run.inputs.coverage_threshold, "p_breach": run.summary.p_breach,
            "breakeven_wti_hold": run.summary.breakeven_wti_hold}


def format_alert(a: Alert) -> str:
    """Slack text for a treasury or credit reader: what changed, by how much, and why. It notifies;
    it never recommends an action. Short on purpose; the rule code and run id stay in the footer."""
    p = a.payload
    name = p.get("company_name") or a.company
    d = p.get("decision") or {}
    theta = f"{d['coverage_threshold']:.1f}x" if d.get("coverage_threshold") is not None else "the threshold"
    if a.rule == "BREACH_FLIP":
        lines = [
            f"*{name}: the planned ${d.get('note_size_usd_m', 0):,.0f}M {d.get('tenor_years', '?')}-year note "
            f"just got riskier to service.*",
            f"Chance that interest coverage drops below {theta}: *{_pct(p['after'])}* (was {_pct(p['before'])}).",
        ]
        if d.get("breakeven_wti_hold") is not None:
            lines.append(f"Why: the oil and Fed markets behind this moved. Coverage falls below {theta} "
                         f"if WTI rises above ${d['breakeven_wti_hold']:,.0f} a barrel.")
    elif a.rule == "DRIVER_MOVE":
        lines = [
            f"*{name}: a market behind the note decision moved fast.*",
            f"{p['title']} went from {_pct(p['before'], 0)} to {_pct(p['after'], 0)} in the last {p['window_min']} min."
            + (f" {p['mechanism'].rstrip('.')}." if p.get("mechanism") else ""),
            f"Chance that coverage drops below {theta} is now {_pct(d.get('p_breach'))}.",
        ]
    else:
        lines = [
            f"*Data check: {p['source']} has been unavailable since {_when(p.get('last_ok_at'))}.*",
            "Answers may rest on older quotes and will say so (STALE_MARKETS). No action needed unless it continues.",
        ]
    lines += [f"• {h}" for h in p.get("headlines", [])]
    lines.append(f"_{_when(p['as_of'])} · {a.rule} · run {p.get('run_id') or '-'} · "
                 "based on prediction-market odds; not a forecast or investment advice._")
    return "\n".join(lines)


def _when(iso: str | None) -> str:
    if not iso:
        return "an unknown time"
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    return f"{t:%b} {t.day}, {t:%H:%M} UTC"


def _pct(v: float | None, digits: int = 1) -> str:
    return "n/a" if v is None else f"{v * 100:.{digits}f}%"
