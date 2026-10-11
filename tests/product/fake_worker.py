"""TEMPORARY stand-in for lane A's worker.WorkerLoop (A12/A13), used by lane C tests until lane A's
worker is on main and ships its own fake (CP0_DECISIONS R5). Replace it then.

It follows the agreed contract (CP0_DECISIONS D5): start(), stop(), tick_markets(), tick_filings(),
tick_macro(), and status() -> (dict[str, SourceStatus], WorkerStatus). The constructor is lane A's
business; the API receives a ready worker from a factory.
"""

from __future__ import annotations

from datetime import datetime, timezone

from airscen.types import SourceStatus, WorkerStatus

SOURCES = ("kalshi", "polymarket", "edgar", "fred", "alpaca", "gdelt", "slack")
TICK_AT = datetime(2026, 10, 9, 14, 29, tzinfo=timezone.utc)


class FakeWorker:
    def __init__(self, fail_start: Exception | None = None, fail_status: Exception | None = None):
        self.fail_start, self.fail_status = fail_start, fail_status
        self.started = self.stopped = 0
        self.ticks: list[str] = []
        self.sources = {name: SourceStatus(status="ok", last_ok_at=TICK_AT) for name in SOURCES}
        self.worker = WorkerStatus(last_tick_at=TICK_AT)

    def start(self):
        self.started += 1
        if self.fail_start:
            raise self.fail_start

    def stop(self):
        self.stopped += 1

    def tick_markets(self):
        self.ticks.append("markets")

    def tick_filings(self):
        self.ticks.append("filings")

    def tick_macro(self):
        self.ticks.append("macro")

    def status(self):
        if self.fail_status:
            raise self.fail_status
        return dict(self.sources), self.worker
