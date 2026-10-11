"""Admin commands (LLD section 3): they call the API, never the store or the worker directly (DD-1).

    python -m airscen.cli ingest [--sources markets,filings,macro] [--tickers DAL,UAL] [--no-wait]
    python -m airscen.cli backfill --window 2026-07-01..2026-07-12 [--tickers DAL] [--no-wait]

Exit codes: 0 the job succeeded (or was started with --no-wait), 1 the job finished with errors,
2 the API refused the request or could not be reached.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable

from airscen.ui.client import ApiClient, ApiError

POLL_S = 1.0
DONE = {"succeeded", "partial", "failed"}


def ingest(client: ApiClient, sources: list[str] | None = None, tickers: list[str] | None = None,
           wait: bool = True, sleep: Callable[[float], None] = time.sleep) -> int:
    body: dict = {"sources": sources or ["markets", "filings", "macro"]}
    if tickers:
        body["tickers"] = tickers
    return _start(client, body, wait, sleep)


def backfill(client: ApiClient, window: str, tickers: list[str] | None = None,
             wait: bool = True, sleep: Callable[[float], None] = time.sleep) -> int:
    body: dict = {"sources": ["markets"], "window": window}
    if tickers:
        body["tickers"] = tickers
    return _start(client, body, wait, sleep)


def eval() -> int:  # noqa: A001 - the LLD names the command `eval`
    print("make eval: not implemented yet - wired to lane B's eval module in C14", file=sys.stderr)
    return 2


def _start(client: ApiClient, body: dict, wait: bool, sleep: Callable[[float], None]) -> int:
    try:
        job_id = client.post("/admin/ingest", body)["job_id"]
        print(f"Started {job_id}: {', '.join(body['sources'])}" + (f" for {body['window']}" if "window" in body else ""))
        if not wait:
            return 0
        while True:
            job = client.get(f"/admin/jobs/{job_id}")
            p = job["progress"]
            print(f"  {job['status']}: {p['done']}/{p['total']} source groups")
            if job["status"] in DONE:
                break
            sleep(POLL_S)
    except ApiError as e:
        extra = f" (job {e.details['job_id']})" if "job_id" in e.details else ""
        print(f"{e.code}: {e.message}{extra}", file=sys.stderr)
        return 2
    for err in job["errors"]:
        print(f"  {err['source']}: {err['code']}: {err['message']}")
    print(f"{job_id} {job['status']}")
    return 0 if job["status"] == "succeeded" else 1


def main(argv: list[str] | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    parser = argparse.ArgumentParser(prog="airscen", description="AirScen admin commands")
    sub = parser.add_subparsers(dest="command", required=True)
    p_ingest = sub.add_parser("ingest", help="fetch markets, filings and macro data now")
    p_ingest.add_argument("--sources", type=_csv, help="comma-separated: markets,filings,macro (default all)")
    p_backfill = sub.add_parser("backfill", help="backfill market quotes for a replay window")
    p_backfill.add_argument("--window", required=True, help="YYYY-MM-DD..YYYY-MM-DD")
    for p in (p_ingest, p_backfill):
        p.add_argument("--tickers", type=_csv, help="comma-separated tickers (default all configured)")
        p.add_argument("--no-wait", action="store_true", help="return once the job has started")
    sub.add_parser("eval", help="run the evaluation checks (C14)")
    args = parser.parse_args(argv)

    if args.command == "eval":
        return eval()
    client = ApiClient()
    if args.command == "ingest":
        return ingest(client, args.sources, args.tickers, wait=not args.no_wait, sleep=sleep)
    return backfill(client, args.window, args.tickers, wait=not args.no_wait, sleep=sleep)


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


if __name__ == "__main__":
    sys.exit(main())
