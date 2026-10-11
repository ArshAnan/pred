"""LLM client. LLD section 7: cache, fallback, repair, budget, cache_only."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from airscen.llm import LLMClient, ProviderCallError
from airscen.types import LLMError
from tests.fakes.fake_llm import ScriptedProvider

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
AS_OF = datetime(2026, 10, 10, tzinfo=timezone.utc)


class MemoryCache:
    def __init__(self):
        self.rows = {}

    def get(self, key):
        return self.rows.get(key)

    def put(self, entry):
        self.rows[entry["cache_key"]] = entry


class Clock:
    def __init__(self):
        self.t = AS_OF

    def __call__(self):
        return self.t


def _client(providers, mode="live", **kwargs):
    cache = MemoryCache()
    clock = kwargs.pop("now", Clock())
    client = LLMClient(
        cache.get,
        cache.put,
        chain=list(providers),
        mode=mode,
        providers=providers,
        now=clock,
        **kwargs,
    )
    return client, cache, clock


def test_cache_hit_makes_no_call():
    provider = ScriptedProvider(['{"ok": true}'])
    client, cache, _ = _client({"snowflake": provider})
    first = client.complete_json("label_market", "sys", "user", SCHEMA, "")
    assert first == {"ok": True}
    assert len(provider.calls) == 1
    second = client.complete_json("label_market", "sys", "user", SCHEMA, "ignored")
    assert second == {"ok": True}
    assert len(provider.calls) == 1
    assert client.cache_hits == 1
    assert len(cache.rows) == 1


def test_provider_fallback_order_on_retryable_errors():
    first = ScriptedProvider([ProviderCallError("down", status=503)])
    second = ScriptedProvider([ProviderCallError("slow", status=429)])
    third = ScriptedProvider(['{"ok": true}'])
    client, _, _ = _client({"snowflake": first, "gemini": second, "ollama": third})
    assert client.complete_json("label_market", "sys", "user", SCHEMA, "") == {"ok": True}
    assert len(first.calls) == 1
    assert len(second.calls) == 1
    assert len(third.calls) == 1
    assert client.last_provider == "ollama"
    assert [call["temperature"] for call in third.calls] == [0]
    assert third.calls[0]["max_tokens"] == 400


def test_repair_retry_then_accepts_valid_json():
    provider = ScriptedProvider(["not json", '{"ok": true}'])
    client, _, _ = _client({"snowflake": provider})
    assert client.complete_json("extract_metrics", "sys", "user", SCHEMA, "") == {"ok": True}
    assert len(provider.calls) == 2
    assert "failed validation" in provider.calls[1]["user"]
    assert provider.calls[0]["max_tokens"] == 2000


def test_schema_failure_repairs_once_then_moves_on():
    snowflake = ScriptedProvider(['{"ok": "no"}', '{"ok": "still"}'])
    gemini = ScriptedProvider(['{"ok": false}'])
    client, _, _ = _client({"snowflake": snowflake, "gemini": gemini})
    assert client.complete_json("write_brief", "sys", "user", SCHEMA, "") == {"ok": False}
    assert len(snowflake.calls) == 2
    assert len(gemini.calls) == 1
    assert gemini.calls[0]["max_tokens"] == 1200


def test_call_budget_and_the_hour_window():
    provider = ScriptedProvider(['{"ok": true}', '{"ok": true}'])
    clock = Clock()
    client, _, _ = _client({"snowflake": provider}, max_calls_per_hour=1, now=clock)
    client.complete_json("label_market", "sys", "user", SCHEMA, "")
    with pytest.raises(LLMError) as exc:
        client.complete_json("label_market", "sys", "other", SCHEMA, "")
    assert exc.value.code == "E_LLM_BUDGET"
    assert len(provider.calls) == 1
    clock.t = AS_OF + timedelta(hours=1, seconds=1)
    assert client.complete_json("label_market", "sys", "other", SCHEMA, "") == {"ok": True}
    assert len(provider.calls) == 2


def test_input_token_budget_does_not_call():
    provider = ScriptedProvider(['{"ok": true}'])
    client, _, _ = _client({"snowflake": provider}, max_input_tokens=1)
    with pytest.raises(LLMError) as exc:
        client.complete_json("label_market", "sys", "x" * 100, SCHEMA, "")
    assert exc.value.code == "E_LLM_BUDGET"
    assert provider.calls == []


def test_cache_only_miss_does_not_call():
    provider = ScriptedProvider(['{"ok": true}'])
    client, _, _ = _client({"snowflake": provider}, mode="cache_only")
    with pytest.raises(LLMError) as exc:
        client.complete_text("write_brief", "sys", "user", "")
    assert exc.value.code == "E_LLM_UNAVAILABLE"
    assert provider.calls == []


def test_off_mode_does_not_read_the_cache_or_call():
    provider = ScriptedProvider(['{"ok": true}'])
    client, cache, _ = _client({"snowflake": provider}, mode="off")
    key = LLMClient.make_cache_key(
        "label_market", "1", {"system": "sys", "user": "user", "schema": SCHEMA},
    )
    cache.put({
        "cache_key": key,
        "response_json": json.dumps({"ok": True}),
        "provider": "snowflake",
        "model": "m",
    })
    with pytest.raises(LLMError) as exc:
        client.complete_json("label_market", "sys", "user", SCHEMA, key)
    assert exc.value.code == "E_LLM_UNAVAILABLE"
    assert provider.calls == []
    assert client.cache_hits == 0


def test_cache_hit_is_logged(caplog):
    provider = ScriptedProvider(["hello"])
    client, _, _ = _client({"snowflake": provider})
    client.complete_text("write_brief", "sys", "user", "")
    with caplog.at_level("INFO", logger="airscen.llm"):
        client.complete_text("write_brief", "sys", "user", "")
    hits = [json.loads(rec.message) for rec in caplog.records if rec.name == "airscen.llm"]
    assert hits[-1]["cache_hit"] is True
    assert hits[-1]["provider"] == "snowflake"
    assert hits[-1]["component"] == "llm"


def test_a_non_retryable_status_stops_the_chain():
    first = ScriptedProvider([ProviderCallError("nope", status=400)])
    second = ScriptedProvider(['{"ok": true}'])
    client, _, _ = _client({"snowflake": first, "gemini": second})
    with pytest.raises(LLMError) as exc:
        client.complete_json("label_market", "sys", "user", SCHEMA, "")
    assert exc.value.code == "E_LLM_UNAVAILABLE"
    assert second.calls == []


def test_make_cache_key_changes_with_the_prompt_version():
    payload = {"system": "sys", "user": "user"}
    assert LLMClient.make_cache_key("write_brief", "1", payload) != LLMClient.make_cache_key(
        "write_brief", "2", payload,
    )
