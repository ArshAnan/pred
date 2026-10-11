"""Streamlit app (LLD section 7, "Streamlit client rules"). C4: the Run page.

Talks to the API only through ApiClient; imports only `types`, the client and the standard
library, never the store or the engine (DD-1). Run with `make ui`.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import date, datetime, timezone
from datetime import time as clock_time

import streamlit as st

from airscen.types import DecisionProof, RunResult, ScenarioSummary
from airscen.ui.client import UNREACHABLE, ApiClient, ApiError

NA = "n/a"

# Institutional desk: dark surfaces, a steel-blue accent, monospace figures.
# Public trading-terminal references (not a copied Mobbin screen) use the same
# idea: dense type, hairline borders, color only for the numbers that matter.
_CSS = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@450;560&family=IBM+Plex+Sans:wght@420;520;600&display=swap');
html, body, [class*="css"] {
  font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
}
[data-testid="stAppViewContainer"] { background: #0c1220; }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stToolbar"] { display: none; }
footer { display: none; }
.block-container { padding-top: 1.4rem; max-width: 1180px; }
[data-testid="stSidebar"] {
  background: #0e1526;
  border-right: 1px solid #243049;
}
[data-testid="stSidebar"] [data-testid="stMarkdown"] p { margin-bottom: 0.15rem; }
.mark {
  font-weight: 600;
  font-size: 1.2rem;
  letter-spacing: -0.03em;
  margin: 0.2rem 0 0;
}
.mark-sub {
  color: #93a4c3;
  font-size: 0.78rem;
  margin: 0 0 0.8rem;
}
[data-testid="stSidebar"] [data-testid="stRadioOption"] {
  padding: 0.45rem 0.7rem;
  border-radius: 8px;
  border: 1px solid transparent;
}
[data-testid="stSidebar"] [data-testid="stRadioOption"] > div > div:first-child { display: none; }
[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] {
  background: #1a2742;
  border-color: #31415f;
}
.mast { margin: 0 0 1rem; }
.kicker {
  margin: 0 0 0.25rem;
  color: #8eb4ff;
  font-size: 0.68rem;
  letter-spacing: 0.16em;
  text-transform: uppercase;
}
.mast h1 {
  margin: 0 0 0.35rem;
  font-size: 1.65rem;
  font-weight: 600;
  letter-spacing: -0.03em;
  line-height: 1.15;
}
.lede { margin: 0; color: #93a4c3; max-width: 46rem; line-height: 1.45; }
[data-testid="stMetric"] {
  background: #141b2d;
  border: 1px solid #243049;
  border-radius: 10px;
  padding: 0.75rem 0.9rem 0.45rem;
}
[data-testid="stMetricLabel"] {
  color: #93a4c3;
  font-size: 0.7rem;
  letter-spacing: 0.07em;
  text-transform: uppercase;
}
[data-testid="stMetricValue"] {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 1.35rem;
  letter-spacing: -0.03em;
}
[data-testid="stForm"] {
  background: #12192b;
  border: 1px solid #243049;
  border-radius: 12px;
  padding: 0.4rem 0.85rem 0.2rem;
}
[data-testid="stDataFrame"] {
  border: 1px solid #243049;
  border-radius: 10px;
  overflow: hidden;
}
.flag {
  display: inline-block;
  margin: 0 0.4rem 0.4rem 0;
  padding: 0.18rem 0.5rem;
  border-radius: 999px;
  border: 1px solid #31415f;
  background: #1a2742;
  color: #d5def2;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 0.72rem;
}
h3 { letter-spacing: -0.02em; }
"""


def apply_theme() -> None:
    st.html(f"<style>{_CSS}</style>")


def masthead(kicker: str, title: str, lede: str) -> None:
    st.html(
        f'<div class="mast"><p class="kicker">{kicker}</p><h1>{title}</h1>'
        f'<p class="lede">{lede}</p></div>'
    )


# ---- formatting (pure; also used by tests) ----

def pct(p: float | None) -> str:
    return NA if p is None else f"{p * 100:.1f}%"


def times(v: float | None) -> str:
    return NA if v is None else f"{v:.2f}x"


def num(v: float | None) -> str:
    return NA if v is None else f"{v:.2f}"


def summary_metrics(s: ScenarioSummary) -> list[tuple[str, str]]:
    return [
        ("Breach probability", pct(s.p_breach)),
        ("Breakeven WTI, hold / hike", f"{num(s.breakeven_wti_hold)} / {num(s.breakeven_wti_hike)}"),
        ("Coverage P50 / P90 (hold)", f"{times(s.coverage_p50)} / {times(s.coverage_p90)}"),
        ("Liquidity cover", times(s.liquidity_cover)),
    ]


def grid_rows(result: RunResult) -> list[dict[str, str]]:
    return [
        {
            "Fuel": r.fuel_label, "WTI": f"{r.wti:.2f}", "Fed": r.rate_label, "Probability": pct(r.probability),
            "Operating income (USD m)": f"{r.operating_income_usd_m:,.0f}",
            "Net interest (USD m)": f"{r.net_interest_usd_m:,.2f}",
            "Coverage": times(r.coverage), "Below threshold": "yes" if r.breach else "no",
        }
        for r in result.rows
    ]


# ---- API access ----

@st.cache_resource
def client() -> ApiClient:
    return ApiClient()


@st.cache_data(ttl=600)
def companies() -> list[dict]:
    return client().get("/companies")


@st.cache_data(ttl=30)
def market_views() -> list[dict]:
    """Labeled markets. Cached for 30 seconds (LLD Streamlit client rules)."""
    return client().get("/markets")


# ---- page sections ----

def health_banner() -> None:
    c = client()
    try:
        health = c.get("/health")
    except ApiError as e:
        st.session_state["health"] = {"error": e.code}
        if e.code == UNREACHABLE:
            st.error(f"Cannot reach the API at {c.base_url}. Start it with `make api`, then retry.")
            if st.button("Retry"):
                st.rerun()
        else:
            st.error(f"API health check failed: {e.code}: {e.message}")
        return
    st.session_state["health"] = health
    if health.get("status") != "ok":
        st.warning(f"API status: {health.get('status')} (database: {health.get('db')})")


def run_form() -> dict | None:
    try:
        tickers = [c["ticker"] for c in companies()]
    except ApiError:
        tickers = []
    with st.form("run_form"):
        st.subheader("Debt serviceability question")
        left, right = st.columns(2)
        with left:
            ticker = st.selectbox("Company", tickers) if tickers else st.text_input("Ticker", "DAL")
            note = st.number_input("New note size (USD m)", min_value=1.0, max_value=50_000.0, value=1000.0, step=100.0)
            tenor = st.number_input("Tenor (years)", min_value=1, max_value=40, value=7, step=1)
            spread = st.number_input("Coupon spread over the 10-year Treasury (bp)", min_value=0.0,
                                     max_value=1000.0, value=150.0, step=5.0)
        with right:
            threshold = st.number_input(
                "Coverage threshold (x)", min_value=0.1, max_value=50.0, value=3.0, step=0.5,
                help="Placeholder default: no covenant has been verified, so set your own.",
            )
            passthrough = st.slider("Fuel pass-through", 0.0, 1.0, 1.0, 0.05, help="1.0 means no offset.")
            peers = st.checkbox("Include peers", value=True)
            use_now = st.checkbox("Use the current time", value=True)
            as_of_date = st.date_input("As of date (UTC)", value=date.today())
            as_of_time = st.time_input("As of time (UTC)", value=clock_time(0, 0))
        if not st.form_submit_button("Run", type="primary"):
            return None
    as_of = None if use_now else datetime.combine(as_of_date, as_of_time, tzinfo=timezone.utc)
    return {
        "ticker": ticker, "as_of": as_of.isoformat().replace("+00:00", "Z") if as_of else None,
        "note_size_usd_m": float(note), "tenor_years": int(tenor), "coupon_spread_bp": float(spread),
        "coverage_threshold": float(threshold), "fuel_passthrough": float(passthrough), "include_peers": bool(peers),
    }


def await_brief(result: RunResult) -> RunResult:
    """Poll GET /runs/{id} every 2 s for up to 60 s while the LLM brief is pending (LLD section 7)."""
    poll_s = float(os.environ.get("AIRSCEN_POLL_S", "2"))
    deadline = time.monotonic() + float(os.environ.get("AIRSCEN_POLL_TIMEOUT_S", "60"))
    with st.spinner("Writing the LLM brief; the numbers above are final."):
        while result.brief.llm_status == "pending" and time.monotonic() < deadline:
            time.sleep(poll_s)
            try:
                result = RunResult.model_validate(client().get(f"/runs/{result.run_id}"))
            except ApiError:
                break
    return result


def submit(body: dict) -> RunResult | None:
    # A fresh Idempotency-Key per new request; the same key when retrying a failed one (LLD section 7).
    pending = st.session_state.get("pending")
    key = pending["key"] if pending and pending["body"] == body else str(uuid.uuid4())
    try:
        result = RunResult.model_validate(client().post("/runs", body, key=key))
    except ApiError as e:
        st.session_state["pending"] = {"body": body, "key": key}
        error_banner(e)
        return None
    st.session_state.pop("pending", None)
    st.session_state["run_id"] = result.run_id
    st.session_state["as_of"] = result.as_of.isoformat()
    return result


def error_banner(e: ApiError) -> None:
    missing = list(e.details.get("missing") or [])
    if e.code == "E_MISSING_FACT" and missing == ["variable_debt_share"]:
        st.warning(
            "This company is not scored. Its filing does not state the variable-rate share of debt, "
            "and that share is not guessed. Delta can be run: the June 30, 2026 release states 22% variable-rate debt."
        )
        if e.request_id:
            st.caption(f"Request ID: {e.request_id}")
        return
    lines = [f"**{e.code}**: {e.message}"]
    if missing:
        lines.append(f"Missing: {', '.join(missing)}. Try `make ingest`, then run again.")
    for f in e.details.get("fields", []):
        lines.append(f"`{'.'.join(str(p) for p in f.get('loc', []))}`: {f.get('msg', '')}")
    st.error("\n\n".join(lines))
    if e.request_id:
        st.caption(f"Request ID: {e.request_id}")


def show_result(result: RunResult) -> None:
    st.info(f"As of {result.as_of.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC · run {result.run_id}")
    for col, (label, value) in zip(st.columns(4), summary_metrics(result.summary)):
        col.metric(label, value)
    if result.summary.liquidity_ok is False:
        st.warning("Liquidity does not cover debt due within a year.")

    st.subheader("Scenarios")
    labels = [f"{r.fuel_label} · {r.rate_label}" for r in result.rows]
    st.bar_chart(
        {"Scenario": labels, "Coverage": [r.coverage for r in result.rows]},
        x="Scenario",
        y="Coverage",
        horizontal=True,
        color="#8eb4ff",
    )
    st.dataframe(grid_rows(result), hide_index=True, width="stretch")

    st.subheader("Flags")
    if result.flags:
        st.markdown(" ".join(f'<span class="flag">{f}</span>' for f in result.flags), unsafe_allow_html=True)
    else:
        st.markdown("No flags.")

    st.caption(
        "The Proof page recomputes these numbers from the filings, yields and quotes, "
        "and checks the written brief number by number."
    )

    st.subheader("Brief")
    b = result.brief
    if b.llm_status == "pending":
        st.caption("The LLM brief is still being written; showing the template brief. Rerun to check again.")
    else:
        st.caption(f"LLM brief: {b.llm_status}. Showing the {'LLM' if b.llm_status == 'ready' and b.llm_md else 'template'} brief.")
    st.markdown(b.llm_md if b.llm_status == "ready" and b.llm_md else b.template_md)


def last_run() -> RunResult | None:
    """The run in this session (GET /runs/{id}; never cached), or None."""
    if not st.session_state.get("run_id"):
        return None
    try:
        return RunResult.model_validate(client().get(f"/runs/{st.session_state['run_id']}"))
    except ApiError as e:
        error_banner(e)
        return None


def period_end(ticker: str, as_of: datetime) -> str:
    """The company's latest operating-income period at as_of, from GET /companies/{ticker}/facts."""
    try:
        facts = client().get(f"/companies/{ticker}/facts", {"as_of": as_of.isoformat().replace("+00:00", "Z")})
    except ApiError:
        return "-"
    ends = [f["period_end"] for f in facts if f["metric"] == "operating_income"]
    return max(ends) if ends else "-"


def peer_rows(result: RunResult, configured: list[str]) -> list[dict[str, str]]:
    def row(ticker: str, period: str, s, flags: list[str], margin: float | None) -> dict[str, str]:
        return {
            "Company": ticker, "Period end": period, "Breach probability": pct(s.p_breach),
            "Operating margin": pct(margin),
            "Breakeven WTI, hold / hike": f"{num(s.breakeven_wti_hold)} / {num(s.breakeven_wti_hike)}",
            "Coverage P50 / P90": f"{times(s.coverage_p50)} / {times(s.coverage_p90)}",
            "Liquidity cover": times(s.liquidity_cover), "Flags": ", ".join(flags) or "-",
        }

    as_of = result.as_of.astimezone(timezone.utc)
    rows = [row(result.company, period_end(result.company, as_of), result.summary,
                [f for f in result.flags if f not in ("CRUDE_ONLY", "RESOLVE_DATE_PROXY")],
                result.operating_margin)]
    by_ticker = {p.ticker: p for p in result.peers}
    for ticker in configured:
        if ticker in by_ticker:
            p = by_ticker[ticker]
            rows.append(row(ticker, p.period_end.isoformat(), p.summary, p.flags, p.operating_margin))
        else:
            rows.append({"Company": ticker, "Period end": "-", "Breach probability": NA,
                         "Operating margin": NA,
                         "Breakeven WTI, hold / hike": NA, "Coverage P50 / P90": NA, "Liquidity cover": NA,
                         "Flags": "Not scored: a required filing input is missing, and it is not guessed"})
    return rows


def peers_page() -> None:
    masthead(
        "Comparison",
        "Peers",
        "Same question, fuel curve and Fed odds; each airline's own financials and baseline WTI. "
        "Operating margin uses adjusted revenue when every compared company has it. "
        "Otherwise the comparison is GAAP and the run carries MARGIN_BASIS_MISMATCH.",
    )
    result = last_run()
    if result is None:
        st.info("Run a question first on the Run page; its peers appear here.")
        return
    try:
        configured = next((c["peers"] for c in companies() if c["ticker"] == result.company), [])
    except ApiError:
        configured = [p.ticker for p in result.peers]
    st.info(f"As of {result.as_of.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC · run {result.run_id}")
    st.dataframe(peer_rows(result, configured), hide_index=True, width="stretch")


def alert_detail(a: dict) -> str:
    p = a.get("payload", {})
    if a["rule"] == "BREACH_FLIP":
        return f"breach probability {pct(p.get('before'))} -> {pct(p.get('after'))}"
    if a["rule"] == "DRIVER_MOVE":
        return f"{p.get('title', p.get('market_id', '?'))} moved {p.get('move_pp', '?')} points"
    return f"source {p.get('source', '?')} is {p.get('status', '?')}"


def alerts_page() -> None:
    masthead(
        "Watch",
        "Alerts",
        "Alerts fire when the answer flips (BREACH_FLIP), a driver market jumps (DRIVER_MOVE) or a data "
        "source is down (DATA_STALE). Undelivered alerts stay here even if Slack failed.",
    )
    if st.button("Send test alert"):
        try:
            client().post("/admin/alerts/test", None)
            st.success("Test alert delivered to Slack.")
        except ApiError as e:
            error_banner(e)
    try:
        feed = client().get("/alerts", {"limit": 200})
    except ApiError as e:
        error_banner(e)
        return
    if not feed:
        st.info("No alerts yet. For the demo, run `make flip` while `make api` is running.")
        return
    st.dataframe([{
        "Time (UTC)": a["ts"].replace("T", " ").replace("Z", ""), "Company": a["company"], "Rule": a["rule"],
        "Severity": a["severity"], "Delivery": "NOT DELIVERED" if a["status"] == "failed" else a["status"],
        "Detail": alert_detail(a), "Run": a.get("run_id") or "-",
    } for a in feed], hide_index=True, width="stretch")


LABEL_TEXT = {"hand": "hand", "llm": "LLM (unvalidated)"}


def exposure_page() -> None:
    masthead(
        "Markets",
        "Exposure map",
        "Markets that bear on the decision, by factor. Factors are ordered by total trust. "
        "LLM-written labels are not validated; hand labels are.",
    )
    try:
        views = market_views()
    except ApiError as e:
        error_banner(e)
        return
    views = [v for v in views if v.get("label") and v["label"].get("relevant")]
    if not views:
        st.info("No markets are labeled yet. Run `make ingest` with `make api` running.")
        return
    weight: dict[str, float] = {}
    for v in views:
        weight[v["label"]["factor"]] = weight.get(v["label"]["factor"], 0.0) + ((v.get("trust") or {}).get("score") or 0.0)
    views.sort(key=lambda v: (-weight[v["label"]["factor"]], -((v.get("trust") or {}).get("score") or 0.0)))
    st.dataframe([{
        "Factor": v["label"]["factor"], "Market": v["meta"]["title"], "Venue": v["meta"]["venue"],
        "YES means": f"{v['label']['factor']} {v['label']['yes_moves_factor']}",
        "Mid": num((v.get("trust") or {}).get("mid")), "Trust": num((v.get("trust") or {}).get("score")),
        "Stale": "yes" if v.get("stale") else "no", "Label": LABEL_TEXT.get(v["label"]["label_source"], "?"),
    } for v in views], hide_index=True, width="stretch")


REPLAY_FIRST_DAY = date(2026, 7, 1)
REPLAY_DEFAULT_DAY = date(2026, 7, 10)  # June-quarter release; the demo replay (CLAUDE.md section 11)
REPLAY_TIME_UTC = clock_time(20, 0)     # after the US close, so that day's filings and quotes are visible


def sensitivity_page() -> None:
    st.title("Assumption sensitivity")
    st.caption("Moves one stated assumption at a time on the last run. The fuel curve and Fed odds stay put. "
               "The alert line is the same 20% breach probability that arms BREACH_FLIP. "
               "This is still conditional on those inputs; it is not a forecast.")
    run_id = st.session_state.get("run_id")
    if not run_id:
        st.info("Run a question first on the Run page. This page sweeps that question.")
        return
    try:
        report = client().get(f"/runs/{run_id}/sensitivity")
    except ApiError as e:
        error_banner(e)
        return
    st.markdown(report["reading"])
    line = report["line"]
    cols = st.columns(3)
    most = next((lever["label"] for lever in report["levers"] if lever["name"] == report["most_sensitive"]), NA)
    cols[0].metric("Stated breach probability", pct(report["baseline_p_breach"]))
    cols[1].metric("Moves the call most", most)
    cols[2].metric("Same side of the alert line", "yes" if report["call_stable"] else "no")
    for lever in report["levers"]:
        st.subheader(lever["label"])
        flip = lever["flip_value"]
        swing = lever["swing"]
        st.caption(
            f"Stated value {num(lever['baseline_value'])} {lever['unit']}. "
            f"Swing {pct(swing)}. "
            + (f"Crosses the {line * 100:.0f}% line at {flip:.2f} {lever['unit']}." if flip is not None
               else "Does not cross the alert line in this sweep.")
        )
        st.dataframe([{
            "Value": f"{point['value']:.2f}",
            "Stated": "yes" if point["is_baseline"] else "",
            "Breach probability": pct(point["p_breach"]),
            "Change": pct(point["delta_p_breach"]),
            "P50 coverage": times(point["coverage_p50"]),
            "Breakeven WTI": num(point["breakeven_wti_hold"]),
            "At or above the line": "" if point["above_line"] is None else ("yes" if point["above_line"] else "no"),
        } for point in lever["points"]], hide_index=True, use_container_width=True)
        chart = [
            {"value": point["value"], "breach probability": point["p_breach"]}
            for point in lever["points"] if point["p_breach"] is not None
        ]
        if chart:
            st.line_chart(chart, x="value", y="breach probability")


def replay_page() -> None:
    masthead(
        "History",
        "Replay",
        "Run the same question as of an earlier time. Only data visible then is used; "
        "backfilled quotes have capped trust and the run says so (BACKFILL_INPUTS).",
    )
    try:
        tickers = [c["ticker"] for c in companies()]
    except ApiError:
        tickers = ["DAL"]
    today = datetime.now(timezone.utc).date()
    ticker = st.selectbox("Company", tickers, key="replay_ticker")
    day = st.slider("As of (UTC date)", min_value=REPLAY_FIRST_DAY, max_value=max(today, REPLAY_DEFAULT_DAY),
                    value=REPLAY_DEFAULT_DAY, format="YYYY-MM-DD")
    as_of = min(datetime.combine(day, REPLAY_TIME_UTC, tzinfo=timezone.utc), datetime.now(timezone.utc))
    st.warning(f"Replay as of {as_of:%Y-%m-%d %H:%M} UTC: later data is invisible to this run.")
    if not st.button("Replay", type="primary"):
        return
    body = {"ticker": ticker, "as_of": as_of.isoformat().replace("+00:00", "Z"), "note_size_usd_m": 1000.0,
            "tenor_years": 7, "coupon_spread_bp": 150.0, "coverage_threshold": 3.0, "fuel_passthrough": 1.0,
            "include_peers": True}
    result = submit(body)
    if result is not None:
        show_result(await_brief(result))


def proof_page() -> None:
    masthead(
        "Proof",
        "Does this answer follow from its inputs?",
        "The page reads the filings, yields and quotes visible at the run's as-of time, applies the "
        "quarterly formulas again, and checks that a language model did not write the decision numbers.",
    )
    result = last_run()
    if result is None:
        st.info("Run a question first on the Run page. The proof checks that run.")
        return
    try:
        proof = DecisionProof.model_validate(client().get(f"/runs/{result.run_id}/proof"))
    except ApiError as e:
        error_banner(e)
        return
    if proof.verdict == "proved":
        st.success("Proved. The stored answer is the one the formulas produce from these inputs.")
    else:
        st.error("Not proved. At least one check failed, so this answer does not follow from the inputs visible now.")
    st.write(proof.statement)
    left, right = st.columns(2)
    left.metric("Checks passed", f"{sum(c.passed for c in proof.checks)} / {len(proof.checks)}")
    right.metric("Language model wrote a decision number", "yes" if proof.decision_numbers_from_llm else "no")
    st.subheader("Checks")
    for check in proof.checks:
        mark = "Pass" if check.passed else "Fail"
        st.markdown(f"**{mark} — {check.title}**")
        st.caption(check.detail)
    st.subheader("Inputs")
    if not proof.inputs:
        st.caption("No inputs could be read for this as-of time.")
        return
    st.dataframe([{
        "Kind": t.kind, "Name": t.name, "Value": t.value, "Source": t.source,
        "Visible at as-of": "yes" if t.as_of_visible else "no",
        "In the formulas": "yes" if t.used_in_decision else "no",
        "Extraction": t.extraction or "-", "Validated": "-" if t.validated is None else ("yes" if t.validated else "no"),
        "Note": t.note,
    } for t in proof.inputs], hide_index=True, width="stretch")


def run_page() -> None:
    masthead(
        "Desk",
        "Market-implied scenario run",
        "A conditional answer given market-implied odds and stated assumptions. Not a forecast or trading signal.",
    )
    body = run_form()
    result = submit(body) if body else None
    if result is not None:
        result = await_brief(result)
    if result is None and body is None and st.session_state.get("run_id"):
        try:
            result = RunResult.model_validate(client().get(f"/runs/{st.session_state['run_id']}"))
        except ApiError as e:
            error_banner(e)
    if result is not None:
        show_result(result)


PAGES = {"Run": run_page, "Proof": proof_page, "Exposure map": exposure_page, "Peers": peers_page,
         "Sensitivity": sensitivity_page, "Alerts": alerts_page, "Replay": replay_page}


def main() -> None:
    st.set_page_config(page_title="AirScen", layout="wide", initial_sidebar_state="expanded")
    apply_theme()
    st.sidebar.html('<p class="mark">AirScen</p><p class="mark-sub">Scenario desk</p>')
    page = st.sidebar.radio("Page", list(PAGES), label_visibility="collapsed")
    health_banner()
    PAGES[page]()


if __name__ == "__main__":
    main()
