"""TEMPORARY stand-in for lane B's llm.LLMClient, used by lane C tests until lane B's client is on main.
After that, use the real LLMClient with lane B's tests/fakes/fake_llm.py ScriptedProvider (R5).

Same surface as LLD section 7 "LLM contract": complete_text(task, system, user, cache_key) -> str,
raising LLMError when every provider fails. Each script item is a string to return or an exception to raise.
"""

from __future__ import annotations


class ScriptedLLM:
    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []

    def complete_text(self, task: str, system: str, user: str, cache_key: str) -> str:
        self.calls.append({"task": task, "system": system, "user": user, "cache_key": cache_key})
        if not self.script:
            raise AssertionError("ScriptedLLM has no response left")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
