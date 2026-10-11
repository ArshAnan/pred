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

from airscen.types import RunResult, ScenarioSummary
from airscen.ui.client import UNREACHABLE, ApiClient, ApiError

NA = "n/a"


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
        if not st.form_submit_button("Run"):
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
    lines = [f"**{e.code}**: {e.message}"]
    if missing := e.details.get("missing"):
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
    st.dataframe(grid_rows(result), hide_index=True, use_container_width=True)

    st.subheader("Flags")
    st.markdown(", ".join(f"`{f}`" for f in result.flags) if result.flags else "No flags.")

    st.subheader("Brief")
    b = result.brief
    if b.llm_status == "pending":
        st.caption("The LLM brief is still being written; showing the template brief. Rerun to check again.")
    else:
        st.caption(f"LLM brief: {b.llm_status}. Showing the {'LLM' if b.llm_status == 'ready' and b.llm_md else 'template'} brief.")
    st.markdown(b.llm_md if b.llm_status == "ready" and b.llm_md else b.template_md)


def main() -> None:
    st.set_page_config(page_title="AirScen Run", layout="wide")
    st.title("Market-implied scenario run")
    st.caption("A conditional answer given market-implied odds and stated assumptions. Not a forecast or trading signal.")
    health_banner()

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


if __name__ == "__main__":
    main()
