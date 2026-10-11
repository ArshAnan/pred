"""LLM client. Fake providers only; no network."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from airscen.llm import LLMClient, _ProviderCallError, _post_chat
from airscen.types import LLMError
from tests.fakes.fake_llm import ScriptedProvider, memory_cache

_SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}},
    "required": ["a"],
    "additionalProperties": False,
}
_CLOCK = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def _client(providers, **kwargs):
    cache_get, cache_put, store = memory_cache()
    clock = {"now": _CLOCK}

    def now():
        return clock["now"]

    client = LLMClient(
        cache_get,
        cache_put,
        chain=kwargs.pop("chain", list(providers)),
        mode=kwargs.pop("mode", "live"),
        providers=providers,
        now=now,
        env={},
        **kwargs,
    )
    return client, store, clock


def test_a_cache_that_already_parsed_json_is_still_a_hit():
    """The real store returns response_json as a dict. A second loads() call must not run."""
    provider = ScriptedProvider(['{"a": 1}'])
    client, store, _ = _client({"snowflake": provider})
    client.complete_json("label_market", "sys", "user", _SCHEMA, "ignored")
    for entry in store.values():
        entry["response_json"] = json.loads(entry["response_json"])
    again = client.complete_json("label_market", "sys", "user", _SCHEMA, "ignored")
    assert again == {"a": 1}
    assert len(provider.calls) == 1


def test_cache_hit_does_not_call_and_uses_temperature_zero():
    provider = ScriptedProvider(['{"a": 1}'])
    client, _, _ = _client({"snowflake": provider})
    first = client.complete_json("label_market", "sys", "user", _SCHEMA, "ignored")
    second = client.complete_json("label_market", "sys", "user", _SCHEMA, "different")
    assert first == second == {"a": 1}
    assert len(provider.calls) == 1
    assert provider.calls[0]["temperature"] == 0
    assert provider.calls[0]["max_tokens"] == 400


def test_fallback_order_skips_a_failing_provider():
    snowflake = ScriptedProvider([_ProviderCallError("down", status=503)])
    gemini = ScriptedProvider(['{"a": 1}'])
    ollama = ScriptedProvider(['{"a": 2}'])
    client, store, _ = _client(
        {"snowflake": snowflake, "gemini": gemini, "ollama": ollama},
        chain=["snowflake", "gemini", "ollama"],
    )
    assert client.complete_json("label_market", "sys", "user", _SCHEMA, "k") == {"a": 1}
    assert len(snowflake.calls) == 1
    assert len(gemini.calls) == 1
    assert ollama.calls == []
    assert gemini.calls[0]["model"] == "gemini-3.5-flash"
    saved = next(iter(store.values()))
    assert saved["provider"] == "gemini"
    assert saved["model"] == "gemini-3.5-flash"
    assert saved["created_at"].endswith("Z")


def test_connection_and_402_move_to_the_next_provider():
    first = ScriptedProvider([_ProviderCallError("reset", status=None)])
    second = ScriptedProvider([_ProviderCallError("credits", status=402)])
    third = ScriptedProvider(['{"a": 3}'])
    client, _, _ = _client(
        {"snowflake": first, "gemini": second, "ollama": third},
        chain=["snowflake", "gemini", "ollama"],
    )
    assert client.complete_json("write_brief", "", "", _SCHEMA, "k") == {"a": 3}
    assert len(third.calls) == 1


def test_repair_retry_then_accepts_valid_json():
    provider = ScriptedProvider(["nope", '{"a": 1}'])
    client, _, _ = _client({"snowflake": provider})
    assert client.complete_json("extract_metrics", "sys", "user", _SCHEMA, "k") == {"a": 1}
    assert len(provider.calls) == 2
    assert "not valid JSON" in provider.calls[1]["user"]
    assert "Return only the corrected JSON." in provider.calls[1]["user"]


def test_failed_repair_moves_to_the_next_provider():
    snowflake = ScriptedProvider(["nope", "still nope"])
    gemini = ScriptedProvider(['{"a": 4}'])
    client, _, _ = _client(
        {"snowflake": snowflake, "gemini": gemini},
        chain=["snowflake", "gemini"],
    )
    assert client.complete_json("label_market", "sys", "user", _SCHEMA, "k") == {"a": 4}
    assert len(snowflake.calls) == 2
    assert len(gemini.calls) == 1


def test_schema_miss_is_repaired_once():
    provider = ScriptedProvider(['{"a": "no"}', '{"a": 1}'])
    client, _, _ = _client({"snowflake": provider})
    assert client.complete_json("label_market", "sys", "user", _SCHEMA, "k") == {"a": 1}
    assert len(provider.calls) == 2


def test_other_4xx_does_not_continue():
    snowflake = ScriptedProvider([_ProviderCallError("bad request", status=400)])
    gemini = ScriptedProvider(['{"a": 1}'])
    client, _, _ = _client(
        {"snowflake": snowflake, "gemini": gemini},
        chain=["snowflake", "gemini"],
    )
    with pytest.raises(LLMError) as caught:
        client.complete_json("label_market", "sys", "user", _SCHEMA, "k")
    assert caught.value.code == "E_LLM_UNAVAILABLE"
    assert gemini.calls == []


def test_hourly_budget_resets_after_an_hour():
    provider = ScriptedProvider(["ok", "ok"])
    client, _, clock = _client({"snowflake": provider}, max_calls_per_hour=1)
    assert client.complete_text("write_brief", "s", "u", "k") == "ok"
    with pytest.raises(LLMError) as caught:
        client.complete_text("write_brief", "s", "other", "k")
    assert caught.value.code == "E_LLM_BUDGET"
    clock["now"] = _CLOCK + timedelta(hours=1)
    assert client.complete_text("write_brief", "s", "other", "k") == "ok"


def test_input_token_budget():
    provider = ScriptedProvider(["ok"])
    client, _, _ = _client({"snowflake": provider}, max_input_tokens=2)
    with pytest.raises(LLMError) as caught:
        client.complete_text("write_brief", "", "x" * 40, "k")
    assert caught.value.code == "E_LLM_BUDGET"
    assert provider.calls == []


def test_cache_only_miss_does_not_call():
    provider = ScriptedProvider(["ok"])
    client, _, _ = _client({"snowflake": provider}, mode="cache_only")
    with pytest.raises(LLMError) as caught:
        client.complete_text("write_brief", "s", "u", "k")
    assert caught.value.code == "E_LLM_UNAVAILABLE"
    assert provider.calls == []


def test_off_does_not_read_the_cache():
    def explode(key):
        raise AssertionError(key)

    client = LLMClient(explode, lambda entry: None, chain=["snowflake"], mode="off", providers={}, env={})
    with pytest.raises(LLMError) as caught:
        client.complete_text("write_brief", "s", "u", "k")
    assert caught.value.code == "E_LLM_UNAVAILABLE"


def test_text_and_json_are_cached_separately():
    provider = ScriptedProvider(["plain", '{"a": 1}'])
    client, store, _ = _client({"snowflake": provider})
    assert client.complete_text("write_brief", "s", "u", "k") == "plain"
    assert client.complete_json("write_brief", "s", "u", _SCHEMA, "k") == {"a": 1}
    assert len(store) == 2
    assert len(provider.calls) == 2


def test_json_array_is_returned():
    schema = {"type": "array", "items": {"type": "integer"}}
    provider = ScriptedProvider(["[1, 2]"])
    client, _, _ = _client({"snowflake": provider})
    assert client.complete_json("extract_metrics", "s", "u", schema, "k") == [1, 2]


def test_all_invalid_output_is_bad_output():
    provider = ScriptedProvider(["nope", "still nope"])
    client, _, _ = _client({"snowflake": provider})
    with pytest.raises(LLMError) as caught:
        client.complete_json("label_market", "s", "u", _SCHEMA, "k")
    assert caught.value.code == "E_LLM_BAD_OUTPUT"
    assert len(provider.calls) == 2


def test_placeholder_gemini_model_is_replaced():
    provider = ScriptedProvider(['{"a": 1}'])
    client, _, _ = _client(
        {"gemini": provider},
        chain=["gemini"],
        models={"gemini": "<current Flash model id>"},
    )
    client.complete_json("label_market", "s", "u", _SCHEMA, "k")
    assert provider.calls[0]["model"] == "gemini-3.5-flash"


def test_snowflake_sends_max_completion_tokens_and_accept_json():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["accept"] = request.headers.get("accept")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )

    cache_get, cache_put, _ = memory_cache()
    client = LLMClient(
        cache_get,
        cache_put,
        chain=["snowflake"],
        env={"SNOWFLAKE_ACCOUNT": "ORG-ACCT", "SNOWFLAKE_PAT": "pat"},
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert client.complete_text("write_brief", "s", "u", "k") == "ok"
    assert seen["url"] == "https://org-acct.snowflakecomputing.com/api/v2/cortex/v1/chat/completions"
    assert seen["accept"] == "application/json"
    assert seen["body"]["max_completion_tokens"] == 1200
    assert "max_tokens" not in seen["body"]
    assert seen["body"]["temperature"] == 0


def test_post_chat_parses_200_and_402_is_retryable():
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "application/json"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    text, tokens_in, tokens_out = _post_chat(
        "https://example.test/chat",
        {"Accept": "application/json"},
        {"model": "m"},
        httpx.Client(transport=httpx.MockTransport(ok)),
    )
    assert (text, tokens_in, tokens_out) == ("ok", 3, 1)

    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"message": "credits"})

    with pytest.raises(_ProviderCallError) as caught:
        _post_chat(
            "https://example.test/chat",
            {},
            {},
            httpx.Client(transport=httpx.MockTransport(denied)),
        )
    assert caught.value.retryable
    assert caught.value.status == 402
