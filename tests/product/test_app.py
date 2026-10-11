"""C4: Streamlit Run page against a faked API that returns lane B's worked example.

Done when the page renders the golden numbers (LLD section 6 worked example; ILLUSTRATIVE).
AppTest runs app.py in-process; respx fakes the HTTP API the page calls.
"""

import json
from pathlib import Path

import httpx
import pytest
import respx
import streamlit as st
from streamlit.testing.v1 import AppTest

from airscen.types import RunRequest
from stub_api import worked_example_result

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8000/api/v1"
HEALTH = {"status": "ok", "db": "ok", "sources": {}, "last_snapshot_age_s": None,
          "llm": {"mode": "off", "last_provider": None, "calls_last_hour": 0, "cache_hit_rate": None},
          "worker": {"last_tick_at": None, "degraded": False}}
COMPANIES = [{"ticker": "DAL", "cik": 27904, "name": "Delta Air Lines", "peers": ["UAL", "AAL"]}]


def run_json(body: dict, run_id: str = "run_golden") -> dict:
    return json.loads(worked_example_result(RunRequest(**body), run_id=run_id).model_dump_json())


def envelope(code, message, details=None, request_id="req-1"):
    return {"error": {"code": code, "message": message, "details": details or {}, "request_id": request_id}}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("API_BASE_URL", BASE)
    monkeypatch.delenv("API_TOKEN", raising=False)
    st.cache_data.clear()
    st.cache_resource.clear()


@pytest.fixture
def api():
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.get("/health").respond(200, json=HEALTH)
        router.get("/companies").respond(200, json=COMPANIES)
        yield router


def start() -> AppTest:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=20)
    at.run()
    assert not at.exception, at.exception
    return at


def click_run(at: AppTest) -> AppTest:
    next(b for b in at.button if b.label == "Run").click().run()
    assert not at.exception, at.exception
    return at


def page_text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [m.value for m in at.caption] + [m.value for m in at.info]
    parts += [f"{m.label} {m.value}" for m in at.metric]
    return "\n".join(parts)


def posted_run(api):
    def handler(request: httpx.Request):
        return httpx.Response(201, json=run_json(json.loads(request.content)))
    return api.post("/runs").mock(side_effect=handler)


# ---- golden numbers ----

def test_run_page_renders_the_golden_numbers(api):
    posted_run(api)
    at = click_run(start())
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Breach probability"] == "29.7%"
    assert metrics["Breakeven WTI, hold / hike"] == "103.16 / 102.77"
    assert metrics["Coverage P50 / P90 (hold)"] == "7.49x / 3.47x"
    assert metrics["Liquidity cover"] == "2.08x"

    grid = at.dataframe[0].value
    assert len(grid) == 6
    assert list(grid["Coverage"]) == ["9.96x", "7.49x", "3.47x", "9.85x", "7.41x", "3.43x"]
    assert list(grid["WTI"]) == ["80.00", "94.44", "118.00", "80.00", "94.44", "118.00"]
    assert list(grid["Probability"]) == ["24.9%", "33.2%", "24.9%", "5.1%", "6.8%", "5.1%"]
    assert list(grid["Below threshold"]) == ["no", "no", "yes", "no", "no", "yes"]


def test_run_page_shows_flags_brief_and_as_of(api):
    posted_run(api)
    at = click_run(start())
    text = page_text(at)
    for flag in ["VARIABLE_SHARE_STALE", "CRUDE_ONLY", "RESOLVE_DATE_PROXY"]:
        assert flag in text
    assert "# DAL: can it service a $1,000M 7-year note?" in text  # the template brief
    banner = "\n".join(i.value for i in at.info)
    assert "As of 2026-10-09 14:30 UTC" in banner and "run_golden" in banner


def test_form_sends_the_run_request(api):
    route = posted_run(api)
    click_run(start())
    req = route.calls.last.request
    body = json.loads(req.content)
    assert body == {"ticker": "DAL", "as_of": None, "note_size_usd_m": 1000.0, "tenor_years": 7,
                    "coupon_spread_bp": 150.0, "coverage_threshold": 3.0, "fuel_passthrough": 1.0,
                    "include_peers": True}
    assert req.headers["Idempotency-Key"]


# ---- LLM brief polling (C9) ----

def brief_state(run: dict, status: str, md: str | None = None) -> dict:
    return run | {"brief": run["brief"] | {"llm_status": status, "llm_md": md}}


def test_page_polls_until_the_llm_brief_is_ready(api, monkeypatch):
    monkeypatch.setenv("AIRSCEN_POLL_S", "0")
    body = {"ticker": "DAL", "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150}
    run = run_json(body)
    api.post("/runs").respond(201, json=brief_state(run, "pending"))
    polls = api.get(f"/runs/{run['run_id']}").mock(side_effect=[
        httpx.Response(200, json=brief_state(run, "pending")),
        httpx.Response(200, json=brief_state(run, "ready", "**LLM brief:** coverage stays above 6.00x.")),
    ])
    at = click_run(start())
    assert polls.call_count == 2
    assert "**LLM brief:** coverage stays above 6.00x." in [m.value for m in at.markdown]
    assert any("LLM brief: ready" in c.value for c in at.caption)


def test_page_shows_the_template_when_the_llm_brief_takes_too_long(api, monkeypatch):
    monkeypatch.setenv("AIRSCEN_POLL_S", "0")
    monkeypatch.setenv("AIRSCEN_POLL_TIMEOUT_S", "0.05")
    body = {"ticker": "DAL", "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150}
    run = run_json(body)
    api.post("/runs").respond(201, json=brief_state(run, "pending"))
    api.get(f"/runs/{run['run_id']}").respond(200, json=brief_state(run, "pending"))
    at = click_run(start())
    assert "# DAL: can it service a $1,000M 7-year note?" in page_text(at)
    assert any("still being written" in c.value for c in at.caption)


# ---- Peers page (C10) ----

def with_peers(run: dict, *peers: tuple[str, str, list[str]]) -> dict:
    """SYNTHETIC peer rows for the page test: Delta's summary under another ticker."""
    return run | {"peers": [{"ticker": t, "period_end": p, "summary": run["summary"], "flags": f} for t, p, f in peers]}


def open_page(at: AppTest, name: str) -> AppTest:
    at.sidebar.radio[0].set_value(name).run()
    assert not at.exception, at.exception
    return at


def test_peers_page_shows_the_three_airline_table_with_flags(api):
    companies = COMPANIES + [{"ticker": "UAL", "cik": None, "name": "United Airlines Holdings", "peers": ["DAL", "AAL"]},
                             {"ticker": "AAL", "cik": None, "name": "American Airlines Group", "peers": ["DAL", "UAL"]}]
    api.get("/companies").respond(200, json=companies)
    run = with_peers(run_json({"ticker": "DAL", "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150}),
                     ("UAL", "2026-09-30", []), ("AAL", "2026-06-30", ["PERIOD_MISMATCH"]))
    api.get(f"/runs/{run['run_id']}").respond(200, json=run)
    facts = api.get("/companies/DAL/facts").respond(200, json=[
        {"company": "DAL", "metric": "operating_income", "value": 1454, "unit": "usd_m", "basis": "GAAP",
         "period_end": "2026-09-30", "filed_at": "2026-10-09T12:00:00Z", "source_url": "https://www.sec.gov/x",
         "extraction": "override", "validated": True}])
    at = start()
    at.session_state["run_id"] = run["run_id"]
    open_page(at, "Peers")
    table = at.dataframe[0].value
    assert "Operating margin" in table.columns
    assert list(table["Company"]) == ["DAL", "UAL", "AAL"]
    assert list(table["Breach probability"]) == ["29.7%", "29.7%", "29.7%"]
    assert list(table["Period end"]) == ["2026-09-30", "2026-09-30", "2026-06-30"]
    assert "PERIOD_MISMATCH" in list(table["Flags"])[2]
    assert facts.calls.last.request.url.params["as_of"] == "2026-10-09T14:30:00Z"  # the run's own as_of


def test_peers_page_marks_configured_peers_without_a_result(api):
    api.get("/companies").respond(200, json=COMPANIES + [
        {"ticker": "UAL", "cik": None, "name": "United Airlines Holdings", "peers": ["DAL", "AAL"]}])
    run = run_json({"ticker": "DAL", "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150})
    api.get(f"/runs/{run['run_id']}").respond(200, json=run)
    api.get("/companies/DAL/facts").respond(200, json=[])
    at = start()
    at.session_state["run_id"] = run["run_id"]
    open_page(at, "Peers")
    table = at.dataframe[0].value
    assert list(table["Company"]) == ["DAL", "UAL", "AAL"]
    assert list(table["Period end"])[0] == "-"  # no facts visible: shown as unknown, not guessed
    assert "Not scored" in list(table["Flags"])[1]


def test_peers_page_without_a_run_asks_for_one(api):
    at = open_page(start(), "Peers")
    assert any("Run a question first" in i.value for i in at.info)


# ---- Alerts page (C12) ----

ALERTS = [
    {"alert_id": "alert_2", "ts": "2026-10-09T15:00:00Z", "company": "DAL", "run_id": "run_2", "rule": "BREACH_FLIP",
     "dedup_key": "DAL:BREACH_FLIP:-", "severity": "high", "status": "failed",
     "payload": {"before": 0.074, "after": 0.297, "threshold": 0.2}},
    {"alert_id": "alert_1", "ts": "2026-10-09T14:00:00Z", "company": "DAL", "run_id": None, "rule": "DATA_STALE",
     "dedup_key": "DAL:DATA_STALE:kalshi", "severity": "low", "status": "sent",
     "payload": {"source": "kalshi", "status": "degraded"}},
]


def test_alerts_page_lists_the_feed_and_marks_undelivered_alerts(api):
    api.get("/alerts").respond(200, json=ALERTS)
    at = open_page(start(), "Alerts")
    table = at.dataframe[0].value
    assert list(table["Rule"]) == ["BREACH_FLIP", "DATA_STALE"]
    assert list(table["Delivery"]) == ["NOT DELIVERED", "sent"]
    assert "7.4% -> 29.7%" in list(table["Detail"])[0]


def test_alerts_page_sends_a_test_alert(api):
    api.get("/alerts").respond(200, json=[])
    test = api.post("/admin/alerts/test").respond(200, json={"delivered": True})
    at = open_page(start(), "Alerts")
    next(b for b in at.button if b.label == "Send test alert").click().run()
    assert test.call_count == 1 and any("delivered" in s.value for s in at.success)


def test_alerts_page_shows_a_failed_test_alert(api):
    api.get("/alerts").respond(200, json=[])
    api.post("/admin/alerts/test").respond(502, json=envelope("E_SOURCE_UNAVAILABLE", "Slack did not accept the test alert"))
    at = open_page(start(), "Alerts")
    next(b for b in at.button if b.label == "Send test alert").click().run()
    assert "E_SOURCE_UNAVAILABLE" in "\n".join(e.value for e in at.error)


# ---- Exposure map (C13) ----

def market(mid_id, factor, label_source, score, mid, kind="close_above", outcome=None, stale=False):
    """ILLUSTRATIVE market rows for the page test."""
    return {"meta": {"venue": "kalshi", "market_id": mid_id, "token_id": None, "title": f"{mid_id} title", "rules": "",
                     "close_time": "2026-12-01T00:00:00Z", "kind": kind, "outcome": outcome, "threshold": None,
                     "threshold_unit": None, "group_key": "G"},
            "label": {"relevant": True, "factor": factor, "yes_moves_factor": "up", "mechanism": "m",
                      "citation": None, "label_source": label_source},
            "latest": None,
            "trust": {"venue": "kalshi", "market_id": mid_id, "score": score, "mid": mid, "components": {},
                      "excluded_reason": None},
            "stale": stale}


def test_exposure_map_ranks_factors_by_trust_and_marks_llm_labels(api):
    api.get("/markets").respond(200, json=[
        market("FED-HIKE", "rates", "hand", 0.70, 0.17, kind="per_meeting", outcome="hike"),
        market("WTI-90", "fuel", "llm", 0.95, 0.62), market("WTI-100", "fuel", "hand", 0.90, 0.35),
    ])
    at = open_page(start(), "Exposure map")
    table = at.dataframe[0].value
    assert list(table["Factor"]) == ["fuel", "fuel", "rates"]      # fuel's total trust outranks rates
    assert list(table["Label"]) == ["LLM (unvalidated)", "hand", "hand"]
    assert list(table["Trust"]) == ["0.95", "0.90", "0.70"]
    assert list(table["YES means"])[0] == "fuel up"


def test_exposure_map_with_no_markets(api):
    api.get("/markets").respond(200, json=[])
    at = open_page(start(), "Exposure map")
    assert any("No markets" in i.value for i in at.info)


# ---- Replay (C13) ----

def test_replay_at_july_10_shows_a_run_with_backfill_inputs(api):
    body = {"ticker": "DAL", "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150}

    def handler(request: httpx.Request):
        sent = json.loads(request.content)
        run = run_json(sent | {"as_of": sent["as_of"]})
        return httpx.Response(201, json=run | {"flags": run["flags"] + ["BACKFILL_INPUTS"]})
    route = api.post("/runs").mock(side_effect=handler)
    at = open_page(start(), "Replay")
    assert at.slider[-1].value.isoformat() == "2026-07-10"          # the demo replay date by default
    next(b for b in at.button if b.label == "Replay").click().run()
    sent = json.loads(route.calls.last.request.content)
    assert sent["as_of"] == "2026-07-10T20:00:00Z" and sent["ticker"] == "DAL"
    assert any("Replay as of 2026-07-10 20:00 UTC" in w.value for w in at.warning)
    assert "BACKFILL_INPUTS" in page_text(at)


# ---- errors ----

def test_error_banner_shows_code_message_and_missing_facts(api):
    api.post("/runs").respond(422, json=envelope(
        "E_MISSING_FACT", "Required facts are missing for DAL: variable_debt_share",
        {"missing": ["variable_debt_share"]}, request_id="req-422"))
    at = click_run(start())
    banner = "\n".join(w.value for w in at.warning)
    assert "not scored" in banner and "variable-rate share" in banner
    assert "req-422" in page_text(at)
    assert not at.metric  # no result shown
    assert not at.error


def test_retry_after_a_failure_reuses_the_idempotency_key(api):
    route = api.post("/runs").mock(side_effect=[
        httpx.Response(503, json=envelope("E_STORE_UNAVAILABLE", "database is locked")),
        httpx.Response(201, json=run_json({"ticker": "DAL", "note_size_usd_m": 1000, "tenor_years": 7,
                                           "coupon_spread_bp": 150})),
    ])
    at = click_run(start())
    assert at.error
    click_run(at)
    keys = [c.request.headers["Idempotency-Key"] for c in route.calls]
    assert len(keys) == 2 and keys[0] == keys[1]
    assert at.metric


def test_each_new_run_gets_a_fresh_idempotency_key(api):
    route = posted_run(api)
    at = click_run(start())
    click_run(at)
    keys = [c.request.headers["Idempotency-Key"] for c in route.calls]
    assert len(keys) == 2 and keys[0] != keys[1]


# ---- health banner ----

def test_health_banner_when_the_api_is_unreachable():
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.get("/health").mock(side_effect=httpx.ConnectError("refused"))
        router.get("/companies").mock(side_effect=httpx.ConnectError("refused"))
        at = start()
    banner = "\n".join(e.value for e in at.error)
    assert BASE in banner
    assert any(b.label == "Retry" for b in at.button)


def test_health_banner_when_the_database_is_down():
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.get("/health").respond(503, json=envelope("E_STORE_UNAVAILABLE", "The database is unavailable"))
        router.get("/companies").respond(200, json=COMPANIES)
        at = start()
    assert "E_STORE_UNAVAILABLE" in "\n".join(e.value for e in at.error)


def test_no_banner_when_healthy(api):
    at = start()
    assert not at.error and not at.warning


# ---- Proof page ----

def test_proof_page_shows_a_proved_verdict_and_the_inputs(api):
    run = run_json({"ticker": "DAL", "note_size_usd_m": 1000, "tenor_years": 7, "coupon_spread_bp": 150})
    api.get(f"/runs/{run['run_id']}").respond(200, json=run)
    api.get(f"/runs/{run['run_id']}/proof").respond(200, json={
        "run_id": run["run_id"], "company": "DAL", "as_of": run["as_of"], "verdict": "proved",
        "statement": "The breach probability comes from the quarterly formulas.",
        "checks": [{
            "code": "scenario_rows", "title": "Quarterly formulas match every scenario row",
            "passed": True, "detail": "Hold at the median fuel state: coverage 7.49.",
        }],
        "inputs": [{
            "kind": "fact", "name": "operating_income", "value": "1454 usd_m",
            "source": "https://www.sec.gov/example", "as_of_visible": True, "used_in_decision": True,
            "extraction": "override", "validated": True, "note": "basis=GAAP",
        }],
        "llm_status": "skipped", "decision_numbers_from_llm": False,
    })
    at = start()
    at.session_state["run_id"] = run["run_id"]
    open_page(at, "Proof")
    assert any("Proved" in s.value for s in at.success)
    text = page_text(at)
    assert "Quarterly formulas match every scenario row" in text
    assert "coverage 7.49" in text
    table = at.dataframe[0].value
    assert list(table["Name"]) == ["operating_income"]
    assert list(table["In the formulas"]) == ["yes"]
