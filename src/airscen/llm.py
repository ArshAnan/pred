"""LLM provider chain. CP0 stub; behavior lands in B7.

Does not open the database. Pass Repository.cache_get and Repository.cache_put
in; this module does not import the store.
"""

from collections.abc import Callable
from typing import Literal


class LLMClient:
    def __init__(
        self,
        cache_get: Callable[[str], dict | None],
        cache_put: Callable[[dict], None],
        chain: list[str] | None = None,
        mode: Literal["live", "cache_only", "off"] = "live",
    ) -> None:
        """cache_get(key) and cache_put(entry) match the store methods."""
        raise NotImplementedError

    def complete_json(
        self,
        task: str,
        system: str,
        user: str,
        schema: dict,
        cache_key: str,
    ) -> dict:
        raise NotImplementedError

    def complete_text(self, task: str, system: str, user: str, cache_key: str) -> str:
        raise NotImplementedError
