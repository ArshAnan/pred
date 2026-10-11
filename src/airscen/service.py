"""Run orchestration (LLD sections 5 and 6, "Inputs and required data").

create_run reads everything at one as_of, calls the pure engine (trust, factors, scenario),
attaches the template brief and saves the run. The repo, settings and adapters arrive as
constructor arguments so tests can pass fakes; this module never opens the database itself.
"""

from __future__ import annotations

import logging
import statistics
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from airscen import factors, scenario, trust
from airscen.brief import brief_problems, llm_brief, repair_brief, template_brief
from airscen.types import (
    AirScenError, AnswerPath, AnswerPathPoint, AnswerPathRequest, Basis, Brief, DataError, Fact, Factor, MarketMeta,
    MarketView, Metric, PeerResult, RequestError, RunRequest, RunResult, RunSummary, ScenarioInputs, Snapshot,
    SourceError, StoreError, TrustResult, Venue, path_times,
)

log = logging.getLogger("airscen.service")

REQUIRED = (
    Metric.OPERATING_INCOME, Metric.NET_INTEREST_EXPENSE, Metric.FUEL_GALLONS_M,
    Metric.DEBT_TOTAL, Metric.VARIABLE_DEBT_SHARE,
)
DGS10 = "DGS10"
WTI = "DCOILWTICO"
DGS10_LOOKBACK_DAYS = 30
MIN_WTI_OBS = 30
ALWAYS_FLAGS = ("CRUDE_ONLY", "RESOLVE_DATE_PROXY")


class RunService:
    def __init__(
        self,
        repo: Any,
        settings: Any,
        adapters: Any = None,
        llm: Any = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.repo = repo
        self.settings = settings
        self.adapters = adapters
        self.llm = llm
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def create_run(self, req: RunRequest) -> RunResult:
        prepared = self._prepare(req)

        def scenarios(company: _CompanyInputs):
            # Peers share the request, fuel curve and rate states; facts and W_bar are their own (LLD section 6).
            return scenario.run_scenarios(self._inputs(req, prepared, company))

        rows, summary = scenarios(prepared.focal)
        if req.include_peers:
            peers, peer_books = self._peers(req, prepared.focal, prepared.as_of, scenarios)
        else:
            peers, peer_books = [], []
        peers, focal_margin, margin_flags = self._with_margins(prepared.focal, peers, peer_books)
        flags = list(dict.fromkeys([*prepared.flags, *margin_flags]))

        result = RunResult(
            run_id=f"run_{uuid.uuid4().hex[:12]}", company=req.ticker, as_of=prepared.as_of,
            created_at=prepared.now, inputs=req.model_copy(update={"as_of": prepared.as_of}),
            rows=rows, summary=summary, peers=peers, flags=flags,
            data_version=self.repo.data_version(prepared.as_of),
            brief=Brief(template_md="", llm_md=None, llm_status="pending" if self.llm else "skipped"),
            operating_margin=focal_margin,
        )
        result = result.model_copy(update={"brief": result.brief.model_copy(
            update={"template_md": template_brief(result)})})
        self.repo.save_run(result)
        return result

    def answer_path(self, req: AnswerPathRequest) -> AnswerPath:
        """Recompute one question at each as_of in the window. Nothing is saved.

        A point with a missing fact or Treasury yield is a gap. A quote older than the
        live freshness window is still used, and the point is marked not fresh, because
        a daily sample would otherwise be empty whenever the recorder was quiet for five
        minutes. POST /runs keeps dropping those quotes.
        """
        self._check_ticker(req.ticker)
        times = path_times(req.start, req.end, req.step_hours)
        if len(times) > 48:
            raise RequestError(
                "At most 48 points; widen step_hours or shorten the window",
                code="E_VALIDATION", details={"points": len(times)},
            )
        line = float(self.settings.alerts.breach_threshold)
        points: list[AnswerPathPoint] = []
        prev_oi: float | None = None
        prev_ni: float | None = None
        for as_of in times:
            run_req = RunRequest(
                ticker=req.ticker, as_of=as_of, note_size_usd_m=req.note_size_usd_m,
                tenor_years=req.tenor_years, coupon_spread_bp=req.coupon_spread_bp,
                coverage_threshold=req.coverage_threshold, fuel_passthrough=req.fuel_passthrough,
                include_peers=False,
            )
            try:
                prepared = self._prepare(run_req, allow_stale=True)
            except DataError as exc:
                points.append(AnswerPathPoint(as_of=as_of, status="gap", gap_reason=exc.message))
                prev_oi = prev_ni = None
                continue
            rows, summary = scenario.run_scenarios(self._inputs(run_req, prepared, prepared.focal))
            hold = next(row for row in rows if row.rate_label == "hold" and row.fuel_label == "P50")
            fuel_change = funding_change = None
            if prev_oi is not None and prev_ni not in (None, 0) and hold.net_interest_usd_m != 0:
                fuel_change = (hold.operating_income_usd_m - prev_oi) / hold.net_interest_usd_m
                funding_change = prev_oi / hold.net_interest_usd_m - prev_oi / prev_ni
            p = summary.p_breach
            flags = list(dict.fromkeys(prepared.flags))
            points.append(AnswerPathPoint(
                as_of=as_of, status="ok", p_breach=p,
                breakeven_wti_hold=summary.breakeven_wti_hold, coverage_p50=summary.coverage_p50,
                operating_income_p50_usd_m=hold.operating_income_usd_m,
                net_interest_p50_usd_m=hold.net_interest_usd_m,
                coverage_change_from_fuel=fuel_change, coverage_change_from_funding=funding_change,
                call=None if p is None else ("on_or_above_line" if p >= line else "below_line"),
                fresh=not any(flag in flags for flag in ("STALE_MARKETS", "FUEL_NO_MARKET_INPUT", "RATES_NO_MARKET_INPUT")),
                flags=flags,
            ))
            prev_oi, prev_ni = hold.operating_income_usd_m, hold.net_interest_usd_m
        crossings: list[datetime] = []
        previous = None
        for point in points:
            if point.call is None:
                continue
            if previous is not None and point.call != previous:
                crossings.append(point.as_of)
            previous = point.call
        n_ok = sum(point.status == "ok" for point in points)
        return AnswerPath(
            ticker=req.ticker, line=line, points=points, crossings=crossings,
            n_ok=n_ok, n_gap=len(points) - n_ok,
            reading=_path_reading(points, crossings, line, n_ok, len(points) - n_ok),
        )

    def get_run(self, run_id: str) -> RunResult:
        run = self.repo.get_run(run_id)
        if run is None:
            raise RequestError(f"Unknown run {run_id}", code="E_NOT_FOUND", details={"run_id": run_id})
        return run

    def finish_brief(self, run_id: str) -> None:
        """Background step after POST /runs: try the LLM brief and keep it only if it is faithful.

        ready: within 400 words and every number traces to the run or its facts. rejected: it does not,
        so the template stays. failed: every provider failed (LLMError) or something else broke.
        """
        if self.llm is None:
            return
        run = self.get_run(run_id)
        if run.brief.llm_status != "pending":
            return
        facts, _ = self.repo.latest_facts(run.company, run.as_of)
        try:
            md = llm_brief(run, self.llm, facts)
        except AirScenError as e:
            log.warning("brief for %s failed: %s %s", run_id, e.code, e.message)
            self.repo.update_run_brief(run_id, None, "failed")
            return
        except Exception:
            log.exception("brief for %s failed", run_id)
            self.repo.update_run_brief(run_id, None, "failed")
            return
        problems = brief_problems(md, run, facts)
        if any(item.startswith("numbers not in the input") for item in problems):
            try:
                repaired = repair_brief(md, problems, run, self.llm, facts)
            except Exception:
                log.exception("brief repair for %s failed", run_id)
                repaired = ""
            if repaired and not brief_problems(repaired, run, facts):
                self.repo.update_run_brief(run_id, repaired, "ready")
                return
            if repaired:
                again = brief_problems(repaired, run, facts)
                log.warning("brief repair for %s still rejected: %s", run_id, "; ".join(again))
        if problems:
            log.warning("brief for %s rejected: %s", run_id, "; ".join(problems))
            self.repo.update_run_brief(run_id, None, "rejected")
        else:
            self.repo.update_run_brief(run_id, md, "ready")

    # ---- reads for the API (every one takes an as_of; DD-4) ----

    def list_runs(self, ticker: str | None = None, limit: int = 50) -> list[RunSummary]:
        return [
            RunSummary(run_id=r.run_id, company=r.company, as_of=r.as_of, created_at=r.created_at,
                       p_breach=r.summary.p_breach, flags=r.flags, llm_status=r.brief.llm_status)
            for r in self._runs(ticker, limit)
        ]

    def facts(self, ticker: str, as_of: datetime) -> list[Fact]:
        self._check_ticker(ticker)
        facts, _ = self.repo.latest_facts(ticker, as_of)
        return sorted(facts, key=lambda f: (f.metric.value, f.basis.value))

    def market_views(self, as_of: datetime, factor: Factor | None = None, min_trust: float | None = None) -> list[MarketView]:
        views = []
        for meta, label in self.repo.markets(factor):
            snap, stale, score = self._score(meta, as_of)
            if min_trust is not None and (score is None or score.score < min_trust):
                continue
            views.append(MarketView(meta=meta, label=label, latest=snap, trust=score, stale=stale))
        return views

    def history(self, venue: Venue, market_id: str, start: datetime, end: datetime, as_of: datetime) -> list[Snapshot]:
        if not any(m.venue == venue and m.market_id == market_id for m, _ in self.repo.markets()):
            raise RequestError(f"Unknown market {venue.value}/{market_id}", code="E_NOT_FOUND",
                               details={"venue": venue.value, "market_id": market_id})
        return self.repo.snapshot_series(venue, market_id, start, end, as_of)

    def driver_quotes(self, as_of: datetime) -> list[factors.FactorQuote]:
        """The trusted rate and fuel markets that would feed a run at as_of (used by DRIVER_MOVE)."""
        rates, fuel = self._factor_quotes(as_of, [])
        return rates + fuel

    def _runs(self, ticker: str | None, limit: int) -> list[RunResult]:
        lister = getattr(self.repo, "runs", None)
        if lister is None:  # lane A's store has no list method yet; GET /runs needs one
            raise StoreError("Listing runs needs Repository.runs(ticker, limit) in the store (lane A)",
                             details={"missing": "Repository.runs"})
        return lister(ticker=ticker, limit=limit)

    def _check_ticker(self, ticker: str) -> None:
        if ticker not in {c.ticker for c in self.settings.companies}:
            raise RequestError(f"Unknown ticker {ticker}", code="E_NOT_FOUND", details={"ticker": ticker})

    def _prepare(self, req: RunRequest, *, allow_stale: bool = False) -> "_Prepared":
        """Facts, coupon yield, fuel curve and rate states at one as_of."""
        self._check_ticker(req.ticker)
        now = self.clock()
        as_of = req.as_of or now  # one as_of for every read below (DD-4)
        focal = self._company_inputs(req.ticker, as_of)
        dgs10 = self._latest_dgs10(as_of)
        missing = focal.missing_facts + ([DGS10] if dgs10 is None else []) + ([WTI] if focal.wti_ref is None else [])
        if missing:
            raise DataError(
                f"Required inputs are missing for {req.ticker}: {', '.join(missing)}",
                code="E_MISSING_FACT", details={"missing": missing},
            )
        market_flags: list[str] = []
        rate_quotes, fuel_quotes = self._factor_quotes(as_of, market_flags, allow_stale=allow_stale)
        rates, rate_flags = factors.rate_states(rate_quotes)
        curve, fuel_flags = factors.fuel_curve(fuel_quotes, as_of, self.settings.fuel.horizon_days)
        flags = focal.flags + market_flags + rate_flags + fuel_flags + list(ALWAYS_FLAGS)
        return _Prepared(now=now, as_of=as_of, focal=focal, dgs10=float(dgs10), rates=rates, curve=curve, flags=flags)

    def _inputs(self, req: RunRequest, prepared: "_Prepared", company: "_CompanyInputs") -> ScenarioInputs:
        return ScenarioInputs(
            company=company.ticker, as_of=prepared.as_of, note_size_usd_m=req.note_size_usd_m,
            tenor_years=req.tenor_years, coupon=(prepared.dgs10 + req.coupon_spread_bp / 100) / 100,
            coverage_threshold=req.coverage_threshold, fuel_passthrough=req.fuel_passthrough,
            baseline=company.baseline, wti_ref=company.wti_ref, rates=prepared.rates, fuel=prepared.curve,
        )

    # ---- inputs ----

    def _company_inputs(self, ticker: str, as_of: datetime) -> "_CompanyInputs":
        """One company's baseline facts, W_bar and company-level flags at as_of."""
        baseline, fact_flags = self._baseline(ticker, as_of)
        flags = sorted(fact_flags)
        missing = [m.value for m in REQUIRED if m not in baseline]
        wti_ref = self._wti_ref(baseline, as_of, flags)
        if not missing:
            v, d = baseline[Metric.VARIABLE_DEBT_SHARE], baseline[Metric.DEBT_TOTAL]
            if v.period_end < d.period_end:
                flags.append("VARIABLE_SHARE_STALE")
        return _CompanyInputs(ticker, baseline, wti_ref, flags, missing)

    def _peers(self, req: RunRequest, focal: "_CompanyInputs", as_of: datetime, scenarios):
        company = next(c for c in self.settings.companies if c.ticker == req.ticker)
        focal_period = focal.baseline[Metric.OPERATING_INCOME].period_end
        peers = []
        books = []
        for ticker in company.peers:
            peer = self._company_inputs(ticker, as_of)
            if peer.missing_facts or peer.wti_ref is None:
                # Left out rather than failing the focal run; the Peers page shows it as "no result".
                log.info("peer %s left out of %s run: missing %s", ticker, req.ticker,
                         peer.missing_facts + ([WTI] if peer.wti_ref is None else []))
                continue
            _, summary = scenarios(peer)
            period = peer.baseline[Metric.OPERATING_INCOME].period_end
            flags = peer.flags + (["PERIOD_MISMATCH"] if period != focal_period else [])
            peers.append(PeerResult(ticker=ticker, period_end=period, summary=summary, flags=list(dict.fromkeys(flags))))
            books.append((ticker, peer.baseline))
        return peers, books

    def _with_margins(self, focal: "_CompanyInputs", peers: list[PeerResult], peer_books: list[tuple]):
        """Operating margin for the focal company and each peer that has revenue.

        Adjusted revenue is used only when every company in the comparison has it.
        A GAAP fallback sets MARGIN_BASIS_MISMATCH. No revenue means no margin and no flag.
        """
        compared = scenario.compare_margins([(focal.ticker, focal.baseline), *peer_books])
        if not compared.margins:
            return peers, None, []
        updated = [
            peer.model_copy(update={"operating_margin": compared.margins.get(peer.ticker)})
            for peer in peers
        ]
        return updated, compared.margins.get(focal.ticker), list(compared.flags)

    def _baseline(self, company: str, as_of: datetime) -> tuple[dict[Metric, Fact], set[str]]:
        facts, flags = self.repo.latest_facts(company, as_of)
        baseline: dict[Metric, Fact] = {}
        for f in sorted(facts, key=lambda f: f.basis != Basis.GAAP):  # GAAP first (LLD section 6 symbols)
            baseline.setdefault(f.metric, f)
        return baseline, set(flags)

    def _latest_dgs10(self, as_of: datetime) -> float | None:
        day = as_of.astimezone(timezone.utc).date()
        obs = self.repo.macro_series(DGS10, day - timedelta(days=DGS10_LOOKBACK_DAYS), day, as_of)
        return obs[-1].value if obs else None

    def _wti_ref(self, baseline: dict[Metric, Fact], as_of: datetime, flags: list[str]) -> float | None:
        anchor = baseline.get(Metric.OPERATING_INCOME)
        if anchor is not None:
            start, end = _quarter(anchor.period_end)
            obs = self.repo.macro_series(WTI, start, end, as_of)
            if len(obs) >= MIN_WTI_OBS:
                return statistics.fmean(o.value for o in obs)
        override = self.settings.fuel.wti_ref_override
        if override is not None:
            flags.append("REF_WTI_OVERRIDE")
            return float(override)
        return None

    def _score(self, meta: MarketMeta, as_of: datetime) -> tuple[Snapshot | None, bool, TrustResult | None]:
        """Latest snapshot at as_of, whether it is stale, and its trust (None if missing or unclean)."""
        stale_after_s = self.settings.poll.stale_after_s
        snap, stale = self.repo.latest_snapshot(meta.venue, meta.market_id, as_of, stale_after_s=stale_after_s)
        if snap is None or trust.clean(snap, as_of) is None:
            return snap, stale, None
        cfg = {**self.settings.trust.model_dump(), "stale_after_s": self.settings.poll.stale_after_s}
        score = trust.trust_score(meta, snap, as_of, cfg)
        return snap, stale or score.excluded_reason == "stale", score

    def _factor_quotes(self, as_of: datetime, flags: list[str], *, allow_stale: bool = False):
        rates, fuel = [], []
        for meta, label in self.repo.markets():
            if label is None or not label.relevant or label.factor not in (Factor.RATES, Factor.FUEL):
                continue
            if meta.close_time is not None and meta.close_time <= as_of:
                continue  # resolved before as_of
            snap, stale, score = self._score(meta, as_of)
            if score is None:
                continue
            if stale or score.excluded_reason == "stale":
                flags.append("STALE_MARKETS")
                if not allow_stale or score.score < self.settings.trust.floor:
                    continue
                # The path keeps a quote the live run would drop, once it clears the trust floor.
                score = score.model_copy(update={"excluded_reason": None})
            elif score.excluded_reason is not None:
                continue
            if snap.src != "live":
                flags.append("BACKFILL_INPUTS")
            quote = factors.FactorQuote(meta=meta, label=label, snapshot=snap, trust=score)
            (rates if label.factor == Factor.RATES else fuel).append(quote)
        return rates, fuel


@dataclass(frozen=True)
class _Prepared:
    now: datetime
    as_of: datetime
    focal: "_CompanyInputs"
    dgs10: float
    rates: Any
    curve: Any
    flags: list[str]


def _path_reading(points: list[AnswerPathPoint], crossings: list[datetime], line: float, n_ok: int, n_gap: int) -> str:
    sentences = [f"{len(points)} points, {n_ok} computed and {n_gap} missing an input visible at that time."]
    if crossings:
        when = ", ".join(ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") for ts in crossings)
        sentences.append(f"The breach probability crosses the {line * 100:.0f}% alert line at {when}.")
    elif n_ok >= 2 and any(point.call is not None for point in points):
        sentences.append(f"The computed points stay on the same side of the {line * 100:.0f}% alert line.")
    elif n_ok == 0:
        sentences.append("No point had the facts and Treasury yield needed to price the note.")
    stale = sum(1 for point in points if point.status == "ok" and not point.fresh)
    if stale:
        noun = "point" if stale == 1 else "points"
        sentences.append(
            f"{stale} computed {noun} used a quote older than the live freshness window and "
            f"{'is' if stale == 1 else 'are'} marked not fresh. "
            "A Run at that time would have dropped the stale quote."
        )
    last = next((point for point in reversed(points) if point.coverage_change_from_fuel is not None), None)
    if last is not None and last.coverage_change_from_funding is not None:
        sentences.append(
            f"From the previous computed point to {last.as_of.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC, "
            f"P50 hold coverage changed {last.coverage_change_from_fuel:+.2f}x from operating income "
            f"and {last.coverage_change_from_funding:+.2f}x from net interest "
            "(the note, the coupon, and any hike)."
        )
    return " ".join(sentences)


@dataclass(frozen=True)
class _CompanyInputs:
    ticker: str
    baseline: dict
    wti_ref: float | None
    flags: list[str]
    missing_facts: list[str]


SOURCE_GROUPS = ("markets", "filings", "macro")
BACKFILL = "backfill"

# A job runner does the actual work for one source group (lane A's worker ticks and backfill):
# runner(tickers or None, (start, end) window or None). It raises an AirScenError on failure.
JobRunner = Callable[[Optional[list[str]], Optional[tuple[date, date]]], None]


class IngestJobs:
    """Background ingest jobs for POST /admin/ingest (LLD section 7, server rule 4).

    One job per source group at a time; a group already running is a 409. State is in memory:
    one API process (DD-2), and a restart forgets finished jobs, which is fine for admin status.
    """

    def __init__(self, runners: dict[str, JobRunner], clock: Callable[[], datetime]):
        self.runners = runners
        self.clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._running: dict[str, str] = {}  # source group -> job_id

    def start(self, sources: list[str], tickers: list[str] | None, window: tuple[date, date] | None) -> str:
        groups = [BACKFILL] if window else list(sources)
        for g in groups:
            if g not in self.runners:
                raise SourceError(f"No ingest job is wired for '{g}' yet", code="E_SOURCE_UNAVAILABLE",
                                  details={"source": g})
        with self._lock:
            for g in groups:
                if g in self._running:
                    raise RequestError(f"An ingest job for {g} is already running", code="E_CONFLICT",
                                       details={"job_id": self._running[g], "source": g})
            job_id = f"job_{uuid.uuid4().hex[:10]}"
            self._jobs[job_id] = {
                "job_id": job_id, "status": "queued", "sources": groups, "tickers": tickers,
                "window": f"{window[0]}..{window[1]}" if window else None,
                "progress": {"done": 0, "total": len(groups)}, "errors": [],
                "started_at": self.clock(), "finished_at": None,
            }
            for g in groups:
                self._running[g] = job_id
        threading.Thread(target=self._run, args=(job_id, groups, tickers, window), daemon=True).start()
        return job_id

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise RequestError(f"Unknown job {job_id}", code="E_NOT_FOUND", details={"job_id": job_id})
            return {**job, "progress": dict(job["progress"]), "errors": list(job["errors"])}

    def _run(self, job_id: str, groups: list[str], tickers, window) -> None:
        job = self._jobs[job_id]
        job["status"] = "running"
        try:
            for g in groups:
                try:
                    self.runners[g](tickers, window)
                except AirScenError as e:
                    job["errors"].append({"source": g, "code": e.code, "message": e.message})
                except Exception:
                    log.exception("ingest job %s: %s failed", job_id, g)
                    job["errors"].append({"source": g, "code": "E_INTERNAL",
                                          "message": "Unexpected error; see the API log"})
                job["progress"]["done"] += 1
        finally:
            failed = {e["source"] for e in job["errors"]}
            job["status"] = "succeeded" if not failed else "failed" if failed >= set(groups) else "partial"
            job["finished_at"] = self.clock()
            with self._lock:
                for g in groups:
                    self._running.pop(g, None)


def _quarter(period_end: date) -> tuple[date, date]:
    """The calendar quarter that contains period_end."""
    start = date(period_end.year, 3 * ((period_end.month - 1) // 3) + 1, 1)
    end = date(start.year + (start.month == 10), (start.month + 2) % 12 + 1, 1) - timedelta(days=1)
    return start, end
