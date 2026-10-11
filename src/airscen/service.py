"""Run orchestration (LLD sections 5 and 6, "Inputs and required data").

create_run reads everything at one as_of, calls the pure engine (trust, factors, scenario),
attaches the template brief and saves the run. The repo, settings and adapters arrive as
constructor arguments so tests can pass fakes; this module never opens the database itself.
"""

from __future__ import annotations

import statistics
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from airscen import factors, scenario, trust
from airscen.brief import template_brief
from airscen.types import (
    Basis, Brief, DataError, Fact, Factor, Metric, RequestError, RunRequest, RunResult, ScenarioInputs,
)

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
        if req.ticker not in {c.ticker for c in self.settings.companies}:
            raise RequestError(f"Unknown ticker {req.ticker}", code="E_NOT_FOUND", details={"ticker": req.ticker})
        now = self.clock()
        as_of = req.as_of or now  # one as_of for every read below (DD-4)
        flags: list[str] = []

        baseline, fact_flags = self._baseline(req.ticker, as_of)
        flags += sorted(fact_flags)
        missing = [m.value for m in REQUIRED if m not in baseline]
        dgs10 = self._latest_dgs10(as_of)
        if dgs10 is None:
            missing.append(DGS10)
        wti_ref = self._wti_ref(baseline, as_of, flags)
        if wti_ref is None:
            missing.append(WTI)
        if missing:
            raise DataError(
                f"Required inputs are missing for {req.ticker}: {', '.join(missing)}",
                code="E_MISSING_FACT", details={"missing": missing},
            )

        v, d = baseline[Metric.VARIABLE_DEBT_SHARE], baseline[Metric.DEBT_TOTAL]
        if v.period_end < d.period_end:
            flags.append("VARIABLE_SHARE_STALE")

        rate_quotes, fuel_quotes = self._factor_quotes(as_of, flags)
        rates, rate_flags = factors.rate_states(rate_quotes)
        curve, fuel_flags = factors.fuel_curve(fuel_quotes, as_of, self.settings.fuel.horizon_days)
        flags += rate_flags + fuel_flags + list(ALWAYS_FLAGS)

        rows, summary = scenario.run_scenarios(ScenarioInputs(
            company=req.ticker, as_of=as_of, note_size_usd_m=req.note_size_usd_m, tenor_years=req.tenor_years,
            coupon=(dgs10 + req.coupon_spread_bp / 100) / 100,
            coverage_threshold=req.coverage_threshold, fuel_passthrough=req.fuel_passthrough,
            baseline=baseline, wti_ref=wti_ref, rates=rates, fuel=curve,
        ))

        result = RunResult(
            run_id=f"run_{uuid.uuid4().hex[:12]}", company=req.ticker, as_of=as_of, created_at=now,
            inputs=req.model_copy(update={"as_of": as_of}), rows=rows, summary=summary,
            peers=[],  # C10
            flags=list(dict.fromkeys(flags)), data_version=self.repo.data_version(as_of),
            brief=Brief(template_md="", llm_md=None, llm_status="pending" if self.llm else "skipped"),
        )
        result = result.model_copy(update={"brief": result.brief.model_copy(
            update={"template_md": template_brief(result)})})
        self.repo.save_run(result)
        return result

    def get_run(self, run_id: str) -> RunResult:
        run = self.repo.get_run(run_id)
        if run is None:
            raise RequestError(f"Unknown run {run_id}", code="E_NOT_FOUND", details={"run_id": run_id})
        return run

    def finish_brief(self, run_id):
        raise NotImplementedError  # C9

    # ---- inputs ----

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

    def _factor_quotes(self, as_of: datetime, flags: list[str]):
        cfg = {**self.settings.trust.model_dump(), "stale_after_s": self.settings.poll.stale_after_s}
        rates, fuel = [], []
        for view in self.repo.markets():
            meta, label = view.meta, view.label
            if label is None or not label.relevant or label.factor not in (Factor.RATES, Factor.FUEL):
                continue
            if meta.close_time is not None and meta.close_time <= as_of:
                continue  # resolved before as_of
            snap, stale = self.repo.latest_snapshot(meta.venue, meta.market_id, as_of)
            if snap is None or trust.clean(snap, as_of) is None:
                continue
            score = trust.trust_score(meta, snap, as_of, cfg)
            if stale or score.excluded_reason == "stale":
                flags.append("STALE_MARKETS")
                continue
            if score.excluded_reason is not None:
                continue
            if snap.src != "live":
                flags.append("BACKFILL_INPUTS")
            quote = factors.FactorQuote(meta=meta, label=label, snapshot=snap, trust=score)
            (rates if label.factor == Factor.RATES else fuel).append(quote)
        return rates, fuel


def _quarter(period_end: date) -> tuple[date, date]:
    """The calendar quarter that contains period_end."""
    start = date(period_end.year, 3 * ((period_end.month - 1) // 3) + 1, 1)
    end = date(start.year + (start.month == 10), (start.month + 2) % 12 + 1, 1) - timedelta(days=1)
    return start, end
