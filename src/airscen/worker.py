from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from airscen.store import Repository
    from airscen.types import SourceStatus, WorkerStatus


class WorkerLoop:
    def __init__(
        self,
        repo: Repository,
        adapters: Adapters,
        settings: Settings,
        label_fn: Callable,
        after_market_tick: Callable[[datetime], None] | None = None,
    ):
        raise NotImplementedError

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def tick_markets(self):
        raise NotImplementedError

    def tick_filings(self):
        raise NotImplementedError

    def tick_macro(self):
        raise NotImplementedError

    def status(self) -> tuple[dict[str, SourceStatus], WorkerStatus]:
        raise NotImplementedError
