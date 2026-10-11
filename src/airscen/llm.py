"""LLM provider chain, cache, and budget guard (LLD section 7).

Does not open the database. Pass Repository.cache_get and Repository.cache_put
in; this module does not import the store. Until that store exists, tests use
an in-memory cache with the same two call shapes.
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

log = logging.getLogger("airscen.llm")

PROMPT_VERSION = "1"
DEFAULT_CHAIN = ("snowflake", "gemini", "ollama")
DEFAULT_MODELS = {
    "snowflake": "claude-haiku-4-5",
    "gemini": "gemini-3.8-flash",
    "ollama": "llama3.1",
}
OUTPUT_TOKENS = {"extract_metrics": 2_000, "label_market": 400, "write_brief": 1_200}
_HOUR = timedelta(hours=1)


class Completion:
    """One provider response. Tests and the HTTP adapters both return this."""

    def __init__(self, text: str, model: str, tokens_in: int, tokens_out: int):
        self.text = text
        self.model = model
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out


class ProviderCallError(Exception):
    """A provider attempt failed before a usable completion.

    status None is a connection or transport failure. 429, 402 and 5xx move
    the chain to the next provider; any other status stops the chain.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class LLMClient:
    def __init__(
        self,
        cache_get: Callable[[str], dict | None],
        cache_put: Callable[[dict], None],
        chain: list[str] | None = None,
        mode: Literal["live", "cache_only", "off"] = "live",
        *,
        providers: Mapping[str, Callable[..., Completion]] | None = None,
        prompt_version: str = PROMPT_VERSION,
        max_calls_per_hour: int = 120,
        max_input_tokens: int = 20_000,
        now: Callable[[], datetime] | None = None,
        models: Mapping[str, str] | None = None,
    ) -> None:
        """cache_get(key) and cache_put(entry) match the store methods.

        Keyword-only arguments wire tests and settings. The positional
        arguments stay the CP0 contract.
        """
        self._cache_get = cache_get
        self._cache_put = cache_put
        self.chain = list(chain) if chain is not None else _chain_from_env()
        self.mode = mode
        self.prompt_version = prompt_version
        self.max_calls_per_hour = max_calls_per_hour
        self.max_input_tokens = max_input_tokens
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.models = _models(models)
        self._providers = dict(providers) if providers is not None else _live_providers(self.models)
        self._call_times: list[datetime] = []
        self.cache_hits = 0
        self.cache_misses = 0
        self.last_provider: str | None = None

    @staticmethod
    def make_cache_key(task: str, prompt_version: str, payload: dict) -> str:
        """sha256 of task, prompt version and the call input, as hex."""
        blob = json.dumps(
            {"task": task, "prompt_version": prompt_version, "input": payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def complete_json(
        self,
        task: str,
        system: str,
        user: str,
        schema: dict,
        cache_key: str,
    ) -> dict | list:
        """Return a JSON object, or a list when the schema type is array.

        The CP0 stub annotated this as dict. extract_metrics is a JSON list,
        so a list is returned only for an array schema.
        """
        payload = {"system": system, "user": user, "schema": schema}
        value = self._complete("json", task, system, user, schema, payload, cache_key)
        if isinstance(value, (dict, list)):
            return value
        raise LLMError(
            "Cached or model JSON was not an object or a list",
            code="E_LLM_BAD_OUTPUT",
            details={"task": task},
        )

    def complete_text(self, task: str, system: str, user: str, cache_key: str) -> str:
        payload = {"system": system, "user": user}
        value = self._complete("text", task, system, user, None, payload, cache_key)
        if not isinstance(value, str):
            raise LLMError(
                "Cached or model output was not text",
                code="E_LLM_BAD_OUTPUT",
                details={"task": task},
            )
        return value

    @property
    def calls_last_hour(self) -> int:
        cutoff = self._now() - _HOUR
        return sum(1 for ts in self._call_times if ts > cutoff)

    def _complete(
        self,
        kind: Literal["json", "text"],
        task: str,
        system: str,
        user: str,
        schema: dict | None,
        payload: dict,
        cache_key: str,
    ) -> Any:
        if self.mode == "off":
            raise LLMError("LLM mode is off", code="E_LLM_UNAVAILABLE", details={"mode": "off"})

        key = self.make_cache_key(task, self.prompt_version, payload)
        if cache_key != key:
            log.debug(
                "cache_key argument does not match sha256(task, prompt_version, input); using the computed key"
            )
        cached = self._read_cache(key, kind)
        if cached is not None:
            value, row = cached
            self.cache_hits += 1
            self.last_provider = row.get("provider")
            self._log(
                task=task,
                provider=row.get("provider"),
                model=row.get("model"),
                tokens_in=row.get("tokens_in"),
                tokens_out=row.get("tokens_out"),
                latency_ms=0,
                cache_hit=True,
            )
            return value

        self.cache_misses += 1
        if self.mode == "cache_only":
            raise LLMError(
                "No cached LLM response for this input",
                code="E_LLM_UNAVAILABLE",
                details={"mode": "cache_only", "task": task},
            )

        saw_bad_output = False
        last_message = "No LLM provider succeeded"
        for name in self.chain:
            provider = self._providers.get(name)
            if provider is None:
                last_message = f"Provider {name} is not configured"
                continue
            model = self.models.get(name, name)
            try:
                completion = self._call(name, provider, task, system, user, schema, model)
            except ProviderCallError as exc:
                last_message = str(exc)
                if _moves_on(exc):
                    continue
                raise LLMError(
                    last_message,
                    code="E_LLM_UNAVAILABLE",
                    details={"provider": name, "status": exc.status},
                ) from exc
            if kind == "text":
                self._store(key, task, name, completion, completion.text)
                return completion.text
            parsed, error = _decode(completion.text, schema or {})
            if error is None:
                self._store(key, task, name, completion, parsed)
                return parsed
            saw_bad_output = True
            last_message = error
            repair_user = (
                f"{user}\n\nThe previous response failed validation: {error}\n"
                "Reply with only JSON that matches the schema.\n"
                f"Previous response:\n{completion.text}"
            )
            try:
                repaired = self._call(name, provider, task, system, repair_user, schema, model)
            except ProviderCallError as exc:
                last_message = str(exc)
                if _moves_on(exc):
                    continue
                raise LLMError(
                    last_message,
                    code="E_LLM_UNAVAILABLE",
                    details={"provider": name, "status": exc.status},
                ) from exc
            parsed, error = _decode(repaired.text, schema or {})
            if error is None:
                self._store(key, task, name, repaired, parsed)
                return parsed
            saw_bad_output = True
            last_message = error

        code = "E_LLM_BAD_OUTPUT" if saw_bad_output else "E_LLM_UNAVAILABLE"
        raise LLMError(last_message, code=code, details={"task": task, "chain": list(self.chain)})

    def _call(self, name, provider, task, system, user, schema, model) -> Completion:
        tokens = _estimate_tokens(system, user)
        if tokens > self.max_input_tokens:
            raise LLMError(
                f"Input is about {tokens} tokens, above the {self.max_input_tokens} token limit",
                code="E_LLM_BUDGET",
                details={"max_input_tokens": self.max_input_tokens, "tokens": tokens},
            )
        self._reserve_call()
        started = time.perf_counter()
        try:
            completion = provider(
                task=task,
                system=system,
                user=user,
                schema=schema,
                temperature=0,
                model=model,
                max_tokens=OUTPUT_TOKENS.get(task),
            )
        except ProviderCallError as exc:
            self._log(
                task=task,
                provider=name,
                model=model,
                tokens_in=None,
                tokens_out=None,
                latency_ms=_latency_ms(started),
                cache_hit=False,
                error=str(exc),
            )
            raise
        if not isinstance(completion, Completion):
            completion = Completion(
                text=str(getattr(completion, "text", completion)),
                model=str(getattr(completion, "model", model)),
                tokens_in=int(getattr(completion, "tokens_in", tokens)),
                tokens_out=int(getattr(completion, "tokens_out", 0)),
            )
        self.last_provider = name
        self._log(
            task=task,
            provider=name,
            model=completion.model,
            tokens_in=completion.tokens_in,
            tokens_out=completion.tokens_out,
            latency_ms=_latency_ms(started),
            cache_hit=False,
        )
        return completion

    def _reserve_call(self) -> None:
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        cutoff = now - _HOUR
        self._call_times = [ts for ts in self._call_times if ts > cutoff]
        if len(self._call_times) >= self.max_calls_per_hour:
            raise LLMError(
                f"LLM budget exceeded: {self.max_calls_per_hour} calls in the last hour",
                code="E_LLM_BUDGET",
                details={"max_calls_per_hour": self.max_calls_per_hour},
            )
        self._call_times.append(now)

    def _read_cache(self, key: str, kind: str) -> tuple[Any, dict] | None:
        row = self._cache_get(key)
        if not row:
            return None
        raw = row.get("response_json")
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return None
        if kind == "text" and isinstance(value, str):
            return value, row
        if kind == "json" and isinstance(value, (dict, list)):
            return value, row
        return None

    def _store(self, key: str, task: str, provider: str, completion: Completion, value: Any) -> None:
        created = self._now()
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        self._cache_put({
            "cache_key": key,
            "task": task,
            "provider": provider,
            "model": completion.model,
            "response_json": json.dumps(value, ensure_ascii=False),
            "tokens_in": completion.tokens_in,
            "tokens_out": completion.tokens_out,
            "created_at": created.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        })

    def _log(self, **fields: Any) -> None:
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        line = {
            "ts": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": "info",
            "component": "llm",
            "event": "complete",
            **fields,
        }
        log.info(json.dumps(line, default=str))


def _chain_from_env() -> list[str]:
    raw = os.environ.get("LLM_CHAIN", ",".join(DEFAULT_CHAIN))
    names = [part.strip() for part in raw.split(",") if part.strip()]
    return names or list(DEFAULT_CHAIN)


def _models(override: Mapping[str, str] | None) -> dict[str, str]:
    models = dict(DEFAULT_MODELS)
    for name, value in (override or {}).items():
        if value and not str(value).startswith("<"):
            models[name] = value
    return models


def _estimate_tokens(system: str, user: str) -> int:
    return max(1, (len(system) + len(user) + 3) // 4)


def _latency_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _moves_on(exc: ProviderCallError) -> bool:
    status = exc.status
    return status is None or status in {402, 429} or status >= 500


def _decode(text: str, schema: dict) -> tuple[Any, str | None]:
    try:
        value = json.loads(_strip_fence(text))
    except (TypeError, ValueError) as exc:
        return None, f"invalid JSON: {exc}"
    error = _schema_error(schema, value)
    if error:
        return None, error
    return value, None


def _strip_fence(text: str) -> str:
    raw = text.strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _schema_error(schema: dict, value: Any) -> str | None:
    if not schema:
        return None
    expected = schema.get("type")
    if expected and not _type_ok(expected, value):
        return f"expected {expected}"
    if "enum" in schema and value not in schema["enum"]:
        return "value not in enum"
    if isinstance(value, dict):
        required = schema.get("required") or []
        missing = [key for key in required if key not in value]
        if missing:
            return f"missing {', '.join(missing)}"
        properties = schema.get("properties") or {}
        for key, child in properties.items():
            if key in value:
                error = _schema_error(child, value[key])
                if error:
                    return f"{key}: {error}"
        if schema.get("additionalProperties") is False:
            extra = [key for key in value if key not in properties]
            if extra:
                return f"unexpected {', '.join(extra)}"
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            error = _schema_error(schema["items"], item)
            if error:
                return f"[{index}]: {error}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return "below minimum"
        if "maximum" in schema and value > schema["maximum"]:
            return "above maximum"
    return None


def _type_ok(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _live_providers(models: Mapping[str, str]) -> dict[str, Callable[..., Completion]]:
    client = httpx.Client(timeout=httpx.Timeout(30.0, connect=3.0))
    account = os.environ.get("SNOWFLAKE_ACCOUNT", "").strip()
    pat = os.environ.get("SNOWFLAKE_PAT", "").strip()
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    ollama = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
    providers: dict[str, Callable[..., Completion]] = {}
    if account and pat:
        url = f"https://{account}.snowflakecomputing.com/api/v2/cortex/v1/chat/completions"
        providers["snowflake"] = _chat_provider(
            client, url, {"Authorization": f"Bearer {pat}"}, models["snowflake"],
        )
    if gemini_key:
        url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        providers["gemini"] = _chat_provider(
            client, url, {"Authorization": f"Bearer {gemini_key}"}, models["gemini"],
        )
    providers["ollama"] = _chat_provider(client, f"{ollama}/v1/chat/completions", {}, models["ollama"])
    return providers


def _chat_provider(client: httpx.Client, url: str, headers: dict[str, str], default_model: str):
    def complete(*, task, system, user, schema, temperature, model, max_tokens) -> Completion:
        body: dict[str, Any] = {
            "model": model or default_model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": task or "airscen", "schema": schema},
            }
        try:
            response = client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise ProviderCallError(str(exc), status=None) from exc
        except httpx.TransportError as exc:
            raise ProviderCallError(str(exc), status=None) from exc
        if response.status_code >= 400:
            raise ProviderCallError(response.text[:500], status=response.status_code)
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderCallError(f"missing completion: {exc}", status=None) from exc
        usage = data.get("usage") or {}
        text = content if isinstance(content, str) else json.dumps(content)
        return Completion(
            text=text,
            model=str(data.get("model") or model or default_model),
            tokens_in=int(usage.get("prompt_tokens") or _estimate_tokens(system, user)),
            tokens_out=int(usage.get("completion_tokens") or max(1, len(text) // 4)),
        )

    return complete
