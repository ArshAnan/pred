"""Template brief and the number check (LLD sections 5, 6 and 7).

The template path is pure: it imports only `types` and the standard library (DD-5), so a
brief always exists even when every LLM provider is down (DD-6).
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone

from airscen.types import RunResult

ALWAYS_ASSUMED = ("CRUDE_ONLY", "RESOLVE_DATE_PROXY")

FLAG_TEXT = {
    "STALE_MARKETS": "a market feeding the run had a stale quote and was excluded",
    "FUEL_NO_MARKET_INPUT": "no qualifying WTI market ladder, so the breach probability is not available",
    "RATES_NO_MARKET_INPUT": "no Fed decision market, so rates are fixed at hold",
    "LADDER_INCONSISTENT": "a touch market undercuts its close-above twin; numbers are unchanged",
    "VENUE_DISAGREE": "Kalshi and Polymarket disagree on the Fed decision",
    "PERIOD_MISMATCH": "a peer reports a different period from the focal company",
    "VARIABLE_SHARE_STALE": "the variable-rate share of debt is from an earlier period than total debt",
    "FACT_CONFLICT": "two sources disagree on a figure; the XBRL value is used",
    "REF_WTI_OVERRIDE": "the baseline WTI comes from configuration, not FRED",
    "BACKFILL_INPUTS": "replay uses backfilled quotes, so their trust is capped",
    "BACKFILL_GAP": "some markets had no history for the replay and were excluded",
    "MARGIN_BASIS_MISMATCH": "margins are compared across GAAP and adjusted revenue",
}

ASSUMPTIONS = [
    "`CRUDE_ONLY`: fuel cost moves with crude (WTI); the jet fuel crack spread is not modeled.",
    "`RESOLVE_DATE_PROXY`: the WTI market's resolve date stands in for the whole quarter.",
    "Fuel and rate states are treated as independent: each scenario's probability is the product of the two.",
    "The tail points of the WTI survival curve, one threshold spacing beyond the lowest and highest market, "
    "are an assumption.",
    "The new note adds its first-year interest to each quarter; its tenor does not change the quarterly math.",
    "Market odds are inputs as quoted at the as-of time, not forecasts made by this tool.",
]

NA = "not available"


def template_brief(result: RunResult) -> str:
    """Markdown brief built only from the run's own numbers."""
    req, s = result.inputs, result.summary
    theta = _x(req.coverage_threshold)
    out = [
        f"# {result.company}: can it service a {_usd(req.note_size_usd_m)} {req.tenor_years}-year note?",
        "",
        f"As of {_ts(result.as_of)}, run `{result.run_id}`, data version `{result.data_version}`.",
        "",
        "This is a conditional answer given the market-implied odds visible at that time and the "
        "assumptions below. It is not a forecast, a credit rating or a trading signal.",
        "",
        "## Answer",
        "",
        f"- **Probability that coverage falls below {theta}:** {_pct(s.p_breach)}",
        f"- **Breakeven WTI (USD per barrel):** {_breakevens(s.breakeven_wti_hold, s.breakeven_wti_hike)}",
        f"- **Coverage if the Fed holds:** {_x(s.coverage_p50)} at the median fuel state (P50), "
        f"{_x(s.coverage_p90)} at the high fuel state (P90).",
        f"- **Liquidity:** {_liquidity(s.liquidity_cover, s.liquidity_ok)}",
        "",
        "## Inputs",
        "",
        "| Input | Value |",
        "| --- | --- |",
        f"| New note | {_usd(req.note_size_usd_m)}, {req.tenor_years}-year |",
        f"| Coupon spread over the 10-year Treasury | {req.coupon_spread_bp:g} bp |",
        f"| Coverage threshold | {theta} |",
        f"| Fuel pass-through | {req.fuel_passthrough:.2f} (full pass-through means no offset) |",
        "",
        "## Scenarios",
        "",
        "Coverage is quarterly operating income divided by net interest expense, in USD millions.",
        "",
        "| Fuel | WTI (USD/bbl) | Fed | Probability | Operating income | Net interest | Coverage | Below threshold |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in result.rows:
        out.append(
            f"| {r.fuel_label} | {r.wti:.2f} | {r.rate_label} | {_pct(r.probability)} | "
            f"{r.operating_income_usd_m:,.0f} | {r.net_interest_usd_m:,.2f} | {_x(r.coverage)} | "
            f"{'yes' if r.breach else 'no'} |"
        )

    out += ["", "## Peers", ""]
    if result.peers:
        out += [
            "Same request, fuel curve and rate states; each peer uses its own financials and baseline WTI.",
            "",
            "| Company | Period end | Breach probability | Breakeven WTI (hold) | Coverage P50 | Coverage P90 "
            "| Liquidity cover | Flags |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for p in result.peers:
            ps = p.summary
            out.append(
                f"| {p.ticker} | {p.period_end.isoformat()} | {_pct(ps.p_breach)} | {_num(ps.breakeven_wti_hold)} | "
                f"{_x(ps.coverage_p50)} | {_x(ps.coverage_p90)} | {_x(ps.liquidity_cover)} | "
                f"{', '.join(p.flags) or '-'} |"
            )
    else:
        out.append("No peer results in this run.")

    out += ["", "## Flags", ""]
    flags = [f for f in result.flags if f not in ALWAYS_ASSUMED]
    out += [f"- `{f}`: {FLAG_TEXT.get(f, 'see the flag table in the design')}." for f in flags] or ["No other flags."]

    out += ["", "## Assumptions", ""]
    out += [f"- {a}" for a in ASSUMPTIONS]
    return "\n".join(out) + "\n"


def llm_brief(result, llm):
    raise NotImplementedError  # C9


# ---- number check ----

_ISO_DATE = re.compile(
    r"(?<!\d)(\d{4}-\d{2}-\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
_LIST_MARKER = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
# Not part of an identifier: no letter, digit, "_" or "." before it, and not "word-4" as in model names.
_NUMBER = re.compile(r"(?<![\w.])(?<![A-Za-z]-)(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?!\d)([A-Za-z_]*)")
_UNITS = {"", "x", "b", "bn", "m", "mn", "k", "bp", "pp"}


def check_numbers(md: str, result: RunResult) -> bool:
    """True if every number in `md` is some number in `result`, at the precision written.

    A value v also matches as a percentage (v * 100, when |v| <= 1) and in billions
    (v / 1000, when |v| >= 100). Dates must be dates in the run. Signs are ignored.
    """
    return not _unsupported_numbers(md, result)


def _unsupported_numbers(md: str, result: RunResult) -> list[str]:
    values, dates = _input_values(result)
    candidates = set(values)
    candidates |= {v * 100 for v in values if abs(v) <= 1}
    candidates |= {v / 1000 for v in values if abs(v) >= 100}

    bad = []
    for m in _ISO_DATE.finditer(md):
        if m.group(1) not in dates:
            bad.append(m.group(0))
    text = _LIST_MARKER.sub(" ", _ISO_DATE.sub(" ", md))

    for m in _NUMBER.finditer(text):
        whole, frac, unit = m.groups()
        if unit.lower() not in _UNITS:
            continue  # part of an identifier such as 7f3a9c
        x = float(whole.replace(",", "") + ("." + frac if frac else ""))
        tol = 0.5 * 10 ** -(len(frac) if frac else 0) + 1e-9
        if not any(abs(abs(c) - x) <= tol for c in candidates):
            bad.append(m.group(0))
    return bad


def _input_values(result: RunResult) -> tuple[list[float], set[str]]:
    values: list[float] = []
    dates: set[str] = set()

    def walk(o):
        if isinstance(o, bool) or o is None:
            return
        if isinstance(o, (int, float)):
            if math.isfinite(o):
                values.append(float(o))
        elif isinstance(o, (datetime, date)):
            d = o.astimezone(timezone.utc).date() if isinstance(o, datetime) else o
            dates.add(d.isoformat())
            values.append(float(d.year))
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)

    walk(result.model_dump(exclude={"brief"}))
    return values, dates


# ---- formatting ----

def _usd(v: float) -> str:
    return f"${v:,.0f}M"


def _x(v: float | None) -> str:
    return NA if v is None else f"{v:.2f}x"


def _num(v: float | None) -> str:
    return NA if v is None else f"{v:.2f}"


def _pct(p: float | None) -> str:
    return NA if p is None else f"{p * 100:.1f}%"


def _ts(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _breakevens(hold: float | None, hike: float | None) -> str:
    if hold is None and hike is None:
        return f"{NA}: with this fuel pass-through, fuel price does not move coverage."
    return (
        f"{_num(hold)} if the Fed holds, {_num(hike)} if it hikes. "
        "Coverage falls below the threshold when WTI is above the breakeven."
    )


def _liquidity(cover: float | None, ok: bool | None) -> str:
    if cover is None:
        return f"{NA} (liquidity or debt due within a year is missing)."
    verdict = "enough" if ok else "not enough"
    return f"{_x(cover)} of debt due within a year, {verdict} to cover it."
