"""C9: LLM brief (LLD section 7 "LLM contract", write_brief) and its number check, then
finish_brief in the service: ready, rejected and failed (with a scripted LLM stand-in)."""

import json

import pytest

from airscen import brief
from airscen.config import Settings
from airscen.service import RunService
from airscen.types import LLMError, RunRequest
from fake_llm_client import ScriptedLLM
from fake_store import AS_OF, FakeStore, seed_worked_example

GOOD = ("**DAL can service a $1B 7-year note in most scenarios.** The breach probability is 29.7% "
        "(coverage below 6.00x). Breakeven WTI is 103.16 if the Fed holds and 102.77 if it hikes. "
        "Operating income of 1,454 [F6] covers net interest of 146 [F5] about 9.96x at P10. "
        "Flags: VARIABLE_SHARE_STALE, CRUDE_ONLY. This is conditional, not a forecast.")
INVENTED = GOOD + " Under a deeper shock coverage drops to 2.41x."


@pytest.fixture
def store():
    s = FakeStore()
    seed_worked_example(s)
    return s


def svc(store, llm):
    return RunService(repo=store, settings=Settings(), llm=llm, clock=lambda: AS_OF)


def new_run(service):
    return service.create_run(RunRequest(ticker="DAL", note_size_usd_m=1000, tenor_years=7,
                                         coupon_spread_bp=150, coverage_threshold=6.0))


# ---- llm_brief ----

def test_llm_brief_sends_the_run_numbers_and_sourced_facts(store):
    llm = ScriptedLLM(GOOD)
    run = new_run(svc(store, None))
    facts, _ = store.latest_facts("DAL", AS_OF)
    md = brief.llm_brief(run, llm, facts)
    assert md == GOOD
    call = llm.calls[0]
    assert call["task"] == "write_brief"
    for rule in ["400 words", "Use only numbers", "not a forecast", "[F"]:
        assert rule in call["system"], rule
    payload = json.loads(call["user"])
    assert payload["run"]["summary"]["p_breach"] == pytest.approx(0.297, abs=0.0005)
    assert "brief" not in payload["run"]  # the template is not fed back to the model
    ids = {f["id"]: f for f in payload["facts"]}
    assert ids["F6"]["metric"] == "operating_income" and ids["F6"]["value"] == 1454
    assert ids["F6"]["source_url"].startswith("https://www.sec.gov/")


def test_check_numbers_accepts_numbers_from_the_sourced_facts(store):
    run = new_run(svc(store, None))
    facts, _ = store.latest_facts("DAL", AS_OF)
    assert not brief.check_numbers("Operating income was 1,454.", run)
    assert brief.check_numbers("Operating income was 1,454 [F6].", run, facts)


def test_brief_problems_reports_invented_numbers_and_length(store):
    run = new_run(svc(store, None))
    facts, _ = store.latest_facts("DAL", AS_OF)
    assert brief.brief_problems(GOOD, run, facts) == []
    assert any("2.41" in p for p in brief.brief_problems(INVENTED, run, facts))
    long = " ".join(["word"] * 401)
    assert any("401 words" in p for p in brief.brief_problems(long, run, facts))


def test_the_same_question_sends_the_same_input_so_the_llm_cache_can_hit(store):
    # C14: run_id, created_at and data_version change every run; they must not reach the model,
    # or LLM_MODE=cache_only could never find a warmed brief.
    llm = ScriptedLLM(GOOD, GOOD)
    service = svc(store, None)
    facts, _ = store.latest_facts("DAL", AS_OF)
    first, second = new_run(service), new_run(service)
    second = second.model_copy(update={"created_at": AS_OF.replace(hour=16), "data_version": "other"})
    assert first.run_id != second.run_id
    brief.llm_brief(first, llm, facts)
    brief.llm_brief(second, llm, facts)
    assert llm.calls[0]["user"] == llm.calls[1]["user"]
    run_part = json.loads(llm.calls[0]["user"])["run"]
    assert not {"run_id", "created_at", "data_version", "brief"} & set(run_part)


# ---- finish_brief ----

def test_a_run_with_an_llm_starts_pending(store):
    run = new_run(svc(store, ScriptedLLM()))
    assert run.brief.llm_status == "pending" and run.brief.llm_md is None


def test_finish_brief_ready_when_the_numbers_check_out(store):
    service = svc(store, ScriptedLLM(GOOD))
    run = new_run(service)
    service.finish_brief(run.run_id)
    saved = store.get_run(run.run_id)
    assert saved.brief.llm_status == "ready" and saved.brief.llm_md == GOOD
    assert saved.brief.template_md == run.brief.template_md  # the template is kept


@pytest.mark.parametrize("md", [INVENTED, " ".join(["word"] * 401)])
def test_finish_brief_rejected_keeps_the_template(store, md):
    service = svc(store, ScriptedLLM(md))
    run = new_run(service)
    service.finish_brief(run.run_id)
    saved = store.get_run(run.run_id)
    assert saved.brief.llm_status == "rejected" and saved.brief.llm_md is None
    assert saved.brief.template_md == run.brief.template_md


@pytest.mark.parametrize("error", [LLMError("all providers failed", code="E_LLM_UNAVAILABLE"), RuntimeError("bug")])
def test_finish_brief_failed_when_the_llm_fails(store, error):
    service = svc(store, ScriptedLLM(error))
    run = new_run(service)
    service.finish_brief(run.run_id)
    assert store.get_run(run.run_id).brief.llm_status == "failed"


def test_finish_brief_does_nothing_without_an_llm_or_when_not_pending(store):
    service = svc(store, None)
    run = new_run(service)
    service.finish_brief(run.run_id)
    assert store.get_run(run.run_id).brief.llm_status == "skipped"

    llm = ScriptedLLM(GOOD)
    service = svc(store, llm)
    run = new_run(service)
    service.finish_brief(run.run_id)
    service.finish_brief(run.run_id)  # already ready: no second call
    assert len(llm.calls) == 1
