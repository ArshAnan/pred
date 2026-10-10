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


# ---- errors ----

def test_error_banner_shows_code_message_and_missing_facts(api):
    api.post("/runs").respond(422, json=envelope(
        "E_MISSING_FACT", "Required facts are missing for DAL: variable_debt_share",
        {"missing": ["variable_debt_share"]}, request_id="req-422"))
    at = click_run(start())
    banner = "\n".join(e.value for e in at.error)
    assert "E_MISSING_FACT" in banner and "variable_debt_share" in banner
    assert "req-422" in page_text(at)
    assert not at.metric  # no result shown


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
