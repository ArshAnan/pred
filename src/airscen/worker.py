"""Background loops. CP0 stub; the ticks land in A12.

Dependencies are constructor arguments so this module never imports the
product lane. `label_fn` is scout, wrapped by the API lifespan. `after_market_tick`
is the alert evaluator. Both are caught by the loop once A12 is written.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from airscen.config import Settings
from airscen.store import Repository
from airscen.types import MarketMeta, SourceStatus, WorkerStatus


@dataclass(frozen=True)
class Adapters:
    """One object per source. Each object exposes that adapter's LLD functions.

    A module works, because `kalshi.quote` is the function `quote`. Tests pass
    fakes with the same attributes. The worker does not construct clients.
    """

    kalshi: Any
    polymarket: Any
    edgar: Any
    fred: Any
    alpaca: Any
    gdelt: Any


class WorkerLoop:
    def __init__(
        self,
        repo: Repository,
        adapters: Adapters,
        settings: Settings,
        label_fn: Callable[[list[MarketMeta]], None],
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
