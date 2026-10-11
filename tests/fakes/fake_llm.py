"""In-memory LLM provider and cache. Lane B owns this fake."""


class ScriptedProvider:
    """Replay scripted strings or exceptions. Records each call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, system, user, schema, *, temperature, max_tokens, model, json_schema):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "schema": schema,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "model": model,
                "json_schema": json_schema,
            }
        )
        if not self.script:
            raise AssertionError("scripted provider has no response left")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            return item
        return item, 10, 2


def memory_cache():
    store: dict = {}

    def cache_get(key):
        return store.get(key)

    def cache_put(entry):
        store[entry["cache_key"]] = dict(entry)

    return cache_get, cache_put, store
