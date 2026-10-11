"""LLM provider chain.

Does not open the database and does not import the store. Pass
Repository.cache_get and Repository.cache_put in.

The cache key is sha256(task, prompt_version, input). The cache_key
argument on complete_json and complete_text is accepted for the
contract and is not used as the lookup key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx

from airscen.types import LLMError

logger = logging.getLogger("airscen.llm")

PROMPT_VERSION = "1"
_OUTPUT_TOKENS = {"extract_metrics": 2000, "label_market": 400, "write_brief": 1200}
# gemini-3.8-flash is the id Google tells new keys to use. On 2026-10-10 it
# returned 503, and gemini-3.5-flash answered on the OpenAI-compatible endpoint.
_DEFAULT_MODELS = {
    "snowflake": "claude-haiku-4-5",
    "gemini": "gemini-3.5-flash",
    "ollama": "llama3.1",
}
_PLACEHOLDER_MODELS = {
    "<current Flash model id>": "gemini-3.5-flash",
    "<installed instruct model>": "llama3.1",
}


class _ProviderCallError(Exception):
    """A provider request failed before a usable body was parsed."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status
        self.retryable = status is None or status in (402, 429) or status >= 500


class _SchemaError(Exception):
    pass


class LLMClient:
    def __init__(
        self,
        cache_get: Callable[[str], dict | None],
        cache_put: Callable[[dict], None],
        chain: list[str] | None = None,
        mode: Literal["live", "cache_only", "off"] = "live",
        *,
        providers: Mapping[str, Callable[..., tuple[str, int, int]]] | None = None,
        prompt_version: str = PROMPT_VERSION,
        max_calls_per_hour: int = 120,
        max_input_tokens: int = 20_000,
        models: Mapping[str, str] | None = None,
        now: Callable[[], datetime] | None = None,
        env: Mapping[str, str] | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        """cache_get(key) and cache_put(entry) match the store methods."""
        self.cache_get = cache_get
        self.cache_put = cache_put
        self.mode = mode
        self.prompt_version = prompt_version
        self.max_calls_per_hour = max_calls_per_hour
        self.max_input_tokens = max_input_tokens
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._calls: list[datetime] = []
        self.http_client = http_client
        source = env if env is not None else os.environ
        if chain is None:
            raw = source.get("LLM_CHAIN", "snowflake,gemini,ollama")
            chain = [part.strip() for part in raw.split(",") if part.strip()]
        self.chain = list(chain)
        self.models = dict(_DEFAULT_MODELS)
        if models:
            self.models.update(models)
        for name, model in list(self.models.items()):
            self.models[name] = _PLACEHOLDER_MODELS.get(model, model)
        self.providers = dict(providers) if providers is not None else _live_providers(
            self.chain, source, self.models, http_client
        )

    def complete_json(
        self,
        task: str,
        system: str,
        user: str,
        schema: dict,
        cache_key: str,
    ) -> dict:
        return self._complete(task, system, user, schema, cache_key)

    def complete_text(self, task: str, system: str, user: str, cache_key: str) -> str:
        return self._complete(task, system, user, None, cache_key)

    @staticmethod
    def make_cache_key(
        task: str,
        prompt_version: str,
        system: str,
        user: str,
        schema: dict | None,
    ) -> str:
        payload: dict[str, Any] = {
            "task": task,
            "prompt_version": prompt_version,
            "input": {"system": system, "user": user},
        }
        if schema is not None:
            payload["input"]["schema"] = schema
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _complete(self, task: str, system: str, user: str, schema: dict | None, cache_key: str):
        del cache_key
        if self.mode == "off":
            raise LLMError("LLM mode is off", code="E_LLM_UNAVAILABLE")
        key = self.make_cache_key(task, self.prompt_version, system, user, schema)
        if self.mode == "cache_only":
            hit = self.cache_get(key)
            if hit is None:
                raise LLMError("cache miss", code="E_LLM_UNAVAILABLE")
            self._log(task, None, None, None, None, cache_hit=True)
            return _cached_response(hit)
        hit = self.cache_get(key)
        if hit is not None:
            self._log(task, hit.get("provider"), hit.get("model"), hit.get("tokens_in"), hit.get("tokens_out"), True)
            return _cached_response(hit)

        saw_bad = False
        last_message = "no provider accepted the request"
        for name in self.chain:
            provider = self.providers.get(name)
            model = self.models.get(name, "")
            if provider is None:
                last_message = f"{name} is not configured"
                continue
            prompt = user
            for attempt in (0, 1):
                self._check_budget(system, prompt)
                started = time.perf_counter()
                try:
                    text, tokens_in, tokens_out = provider(
                        system,
                        prompt,
                        schema,
                        temperature=0,
                        max_tokens=_OUTPUT_TOKENS.get(task, 1200),
                        model=model,
                        json_schema=schema is not None and name == "snowflake",
                    )
                except _ProviderCallError as exc:
                    self._log(task, name, model, None, None, False, latency_ms=_elapsed(started), error=str(exc))
                    last_message = str(exc)
                    if exc.retryable:
                        break
                    raise LLMError(str(exc), code="E_LLM_UNAVAILABLE", details={"provider": name, "status": exc.status}) from exc
                self._calls.append(self._now())
                if schema is None:
                    self._store(key, task, name, model, text, tokens_in, tokens_out)
                    self._log(task, name, model, tokens_in, tokens_out, False, latency_ms=_elapsed(started))
                    return text
                try:
                    value = _parse_json(text, schema)
                except _SchemaError as exc:
                    saw_bad = True
                    last_message = str(exc)
                    self._log(task, name, model, tokens_in, tokens_out, False, latency_ms=_elapsed(started), error=str(exc))
                    if attempt == 0:
                        prompt = (
                            "The previous response failed validation: "
                            + str(exc)
                            + "\n"
                            + text
                            + "\nReturn only the corrected JSON."
                        )
                        continue
                    break
                self._store(key, task, name, model, value, tokens_in, tokens_out)
                self._log(task, name, model, tokens_in, tokens_out, False, latency_ms=_elapsed(started))
                return value
        code = "E_LLM_BAD_OUTPUT" if saw_bad else "E_LLM_UNAVAILABLE"
        raise LLMError(last_message, code=code)

    def _check_budget(self, system: str, user: str) -> None:
        cutoff = self._now() - timedelta(hours=1)
        self._calls = [ts for ts in self._calls if ts > cutoff]
        if len(self._calls) >= self.max_calls_per_hour:
            raise LLMError("hourly call budget exceeded", code="E_LLM_BUDGET")
        tokens = (len(system) + len(user) + 3) // 4
        if tokens > self.max_input_tokens:
            raise LLMError(
                "input token budget exceeded",
                code="E_LLM_BUDGET",
                details={"tokens_in": tokens},
            )

    def _store(self, key, task, provider, model, value, tokens_in, tokens_out) -> None:
        self.cache_put(
            {
                "cache_key": key,
                "task": task,
                "provider": provider,
                "model": model,
                "response_json": json.dumps(value),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "created_at": self._now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )

    def _log(self, task, provider, model, tokens_in, tokens_out, cache_hit, latency_ms=None, error=None) -> None:
        record = {
            "component": "llm",
            "event": "completion",
            "task": task,
            "provider": provider,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
            "cache_hit": cache_hit,
        }
        if error:
            record["error"] = error
        logger.info(json.dumps(record))


def _cached_response(hit: dict):
    """The stored completion. The real store already parses JSON; a fake cache may still hold the string."""
    raw = hit.get("response_json")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _parse_json(text: str, schema: dict):
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```").strip()
        if stripped.endswith("```"):
            stripped = stripped[: stripped.rfind("```")].strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise _SchemaError("response is not valid JSON") from exc
    _validate(value, schema, "$")
    return value


def _validate(value, schema: dict, path: str) -> None:
    if not isinstance(schema, dict):
        return
    if "enum" in schema and value not in schema["enum"]:
        raise _SchemaError(f"{path} is not one of {schema['enum']}")
    declared = schema.get("type")
    types = declared if isinstance(declared, list) else [declared] if declared else []
    if types and not any(_matches_type(value, name) for name in types):
        raise _SchemaError(f"{path} does not match type {declared}")
    if "object" in types or (not types and isinstance(value, dict) and "properties" in schema):
        if not isinstance(value, dict):
            raise _SchemaError(f"{path} is not an object")
        required = schema.get("required") or []
        props = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        for key in required:
            if key not in value:
                raise _SchemaError(f"{path}.{key} is required")
        for key, item in value.items():
            if key in props:
                _validate(item, props[key], f"{path}.{key}")
            elif additional is False:
                raise _SchemaError(f"{path}.{key} is not allowed")
            elif isinstance(additional, dict):
                _validate(item, additional, f"{path}.{key}")
    if "array" in types or (not types and isinstance(value, list) and "items" in schema):
        if not isinstance(value, list):
            raise _SchemaError(f"{path} is not an array")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate(item, items, f"{path}[{index}]")


def _matches_type(value, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    return True


def _live_providers(chain, env: Mapping[str, str], models: Mapping[str, str], client):
    built = {}
    for name in chain:
        if name == "snowflake":
            built[name] = _http_provider(
                _snowflake_url(env.get("SNOWFLAKE_ACCOUNT", "")),
                env.get("SNOWFLAKE_PAT", ""),
                {"Authorization": f"Bearer {env.get('SNOWFLAKE_PAT', '')}", "Accept": "application/json"},
                client,
                missing="missing snowflake credentials",
                token_field="max_completion_tokens",
            )
        elif name == "gemini":
            built[name] = _http_provider(
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                env.get("GEMINI_API_KEY", ""),
                {"Authorization": f"Bearer {env.get('GEMINI_API_KEY', '')}", "Accept": "application/json"},
                client,
                missing="missing gemini credentials",
            )
        elif name == "ollama":
            host = (env.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
            built[name] = _http_provider(
                f"{host}/v1/chat/completions",
                "local",
                {"Accept": "application/json"},
                client,
                missing="missing ollama host",
            )
        else:
            built[name] = _missing_provider(f"unknown provider {name}")
    return built


def _snowflake_url(account: str) -> str:
    host = account.strip().removeprefix("https://").removesuffix(".snowflakecomputing.com").strip("/").lower()
    if not host:
        return ""
    return f"https://{host}.snowflakecomputing.com/api/v2/cortex/v1/chat/completions"


def _missing_provider(message: str):
    def call(system, user, schema, *, temperature, max_tokens, model, json_schema):
        del system, user, schema, temperature, max_tokens, model, json_schema
        raise _ProviderCallError(message, status=None)

    return call


def _http_provider(url: str, secret: str, headers: dict, client, *, missing: str, token_field: str = "max_tokens"):
    if not url or not secret:
        return _missing_provider(missing)

    def call(system, user, schema, *, temperature, max_tokens, model, json_schema):
        body: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            token_field: max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_schema and schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema, "strict": True},
            }
        return _post_chat(url, {**headers, "Content-Type": "application/json"}, body, client)

    return call


def _post_chat(url: str, headers: dict, body: dict, client: httpx.Client | None = None):
    owns = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=3.0))
    try:
        try:
            response = client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise _ProviderCallError(str(exc), status=None) from exc
        if response.status_code != 200:
            raise _ProviderCallError(response.text[:300], status=response.status_code)
        payload = response.json()
        if isinstance(payload, list):
            payload = payload[0]
        content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
        usage = payload.get("usage") or {}
        return content or "", int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    finally:
        if owns:
            client.close()
