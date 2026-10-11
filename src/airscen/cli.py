"""Admin commands (LLD section 3): they call the API, never the store or the worker directly (DD-1).

    python -m airscen.cli ingest [--sources markets,filings,macro] [--tickers DAL,UAL] [--no-wait]
    python -m airscen.cli backfill --window 2026-07-01..2026-07-12 [--tickers DAL] [--no-wait]
    python -m airscen.cli flip [--low 3.0] [--high 6.0]        # demo: force a BREACH_FLIP alert
    python -m airscen.cli warm [--as-of 2026-10-11T14:00:00Z]  # fill llm_cache for the demo questions
    python -m airscen.cli eval                                  # lane B's five-check report

Exit codes: 0 the job succeeded (or was started with --no-wait), 1 the job finished with errors,
2 the API refused the request or could not be reached.
"""

from __future__ import annotations

import argparse
import runpy
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


def flip(client: ApiClient, low: float = 3.0, high: float = 6.0) -> int:
    """Demo: force a BREACH_FLIP. One alert tick at a low coverage threshold (breach unlikely), then one at
    a high threshold (breach likely), through POST /admin/alerts/tick and the real alert state machine.
    On the worked example, 3.0 gives p_breach about 0.074 and 6.0 gives 0.297, crossing 0.20."""
    try:
        client.post("/admin/alerts/tick", {"coverage_threshold": low})
        alerts = client.post("/admin/alerts/tick", {"coverage_threshold": high})["alerts"]
    except ApiError as e:
        print(f"{e.code}: {e.message}", file=sys.stderr)
        return 2
    flips = [a for a in alerts if a["rule"] == "BREACH_FLIP"]
    for a in alerts:
        delivered = "sent to Slack" if a["status"] == "sent" else "not delivered (saved in the feed as failed)"
        before, after = (_pct(a["payload"].get(k)) for k in ("before", "after"))
        print(f"{a['rule']} {a['company']}: {before} -> {after}, {delivered}")
    if not flips:
        print("No BREACH_FLIP: the answer did not cross the threshold from below, or the alert is cooling down (60 min).")
        return 1
    return 0 if all(a["status"] == "sent" for a in flips) else 1


REPLAY_AS_OF = "2026-07-10T20:00:00Z"   # what the Replay page sends for its default day
DEMO_REQUEST = {"ticker": "DAL", "note_size_usd_m": 1000.0, "tenor_years": 7, "coupon_spread_bp": 150.0,
                "coverage_threshold": 3.0, "fuel_passthrough": 1.0, "include_peers": True}  # the pages' defaults
BRIEF_WAIT_S = 120.0
POLL_BRIEF_S = 2.0


def warm(client: ApiClient, as_of: str | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    """Ask the demo questions once so their LLM briefs land in llm_cache (C14). A brief is cached only
    for the exact question and as_of, so pin the live demo's as_of with --as-of and use it in the Run page."""
    statuses = []
    try:
        for when in [REPLAY_AS_OF] + ([as_of] if as_of else []):
            run = client.post("/runs", DEMO_REQUEST | {"as_of": when})
            waited = 0.0
            while run["brief"]["llm_status"] == "pending" and waited < BRIEF_WAIT_S:
                sleep(POLL_BRIEF_S)
                waited += POLL_BRIEF_S
                run = client.get(f"/runs/{run['run_id']}")
            status = run["brief"]["llm_status"]
            statuses.append(status)
            print(f"{DEMO_REQUEST['ticker']} as of {when}: run {run['run_id']}, LLM brief {status}")
    except ApiError as e:
        print(f"{e.code}: {e.message}", file=sys.stderr)
        return 2
    if all(s == "ready" for s in statuses):
        print("Cache warm. For the demo set LLM_MODE=cache_only in .env and restart make api; "
              "a network failure then cannot break the briefs.")
        return 0
    print("Some briefs are not ready (failed: providers down or LLM_MODE=off; rejected: a number failed the check). "
          "Those runs will show the template brief.")
    return 1


def eval() -> int:  # noqa: A001 - the LLD names the command `eval`
    """Lane B's five-check report (python -m airscen.eval)."""
    runpy.run_module("airscen.eval", run_name="__main__")
    return 0


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
    p_flip = sub.add_parser("flip", help="demo: force a BREACH_FLIP alert (two alert ticks)")
    p_flip.add_argument("--low", type=float, default=3.0, help="coverage threshold for the first tick")
    p_flip.add_argument("--high", type=float, default=6.0, help="coverage threshold for the second tick")
    p_warm = sub.add_parser("warm", help="ask the demo questions once to fill llm_cache")
    p_warm.add_argument("--as-of", help="also warm the live demo at this UTC time, e.g. 2026-10-11T14:00:00Z")
    sub.add_parser("eval", help="print lane B's evaluation report")
    args = parser.parse_args(argv)

    if args.command == "eval":
        return eval()
    client = ApiClient()
    if args.command == "flip":
        return flip(client, args.low, args.high)
    if args.command == "warm":
        return warm(client, args.as_of, sleep=sleep)
    if args.command == "ingest":
        return ingest(client, args.sources, args.tickers, wait=not args.no_wait, sleep=sleep)
    return backfill(client, args.window, args.tickers, wait=not args.no_wait, sleep=sleep)


def _pct(v) -> str:
    return f"{v * 100:.1f}%" if isinstance(v, (int, float)) else "n/a"


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


if __name__ == "__main__":
    sys.exit(main())
