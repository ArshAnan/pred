# Market-Implied Scenario Analyst

Hackknight 2026 (Diameter Capital, Best Financial Hack, Option 1 "Prediction Markets as a Financial Signal").

It answers one corporate-finance question with prediction-market odds turned into explicit, auditable scenarios:

> Can Delta comfortably service a new $1B, 7-year senior note, and how does it compare with United and American?

It reads the company's financials from SEC filings, finds Kalshi and Polymarket markets on oil and the Fed, turns their
odds into fuel-price and rate scenarios, and reports how likely interest coverage is to fall below your threshold, the
breakeven oil price and a liquidity check, with a brief where every number traces to its input. It alerts when the answer
flips. The answer is **conditional on today's market-implied odds and the stated assumptions; it is not a forecast,
a credit rating, investment advice or a trading signal.**

Design: `CLAUDE.md` (overview), the low-level design (LLD) in the repo root, and `docs/plan/` (team plan, CP0 decisions,
lane checklists).

## Setup (from a clean clone)

You need Python 3.11+ and GNU make (Windows: `choco install make`; the Makefile works from PowerShell, cmd or Git Bash).

```bash
make setup
```

This creates `.venv`, installs the package with its test tools, and copies `.env.example` to `.env` (an existing `.env`
is left alone). Then fill in `.env`; it is git-ignored, never commit it.

| Key | Needed | What for |
| --- | --- | --- |
| `SEC_USER_AGENT` | yes | EDGAR requires a name and email, e.g. `Your Name you@example.com` |
| `FRED_API_KEY` | yes | 10-year Treasury (coupon) and WTI (baseline oil price); free at fred.stlouisfed.org |
| `SLACK_WEBHOOK_URL` | for alerts | an incoming webhook for your demo channel; without it alerts are saved as `failed` and still shown in the app |
| `LLM_MODE` | no | `live`, `cache_only` or `off`. Use `off` until you have an LLM key: briefs stay template-only |
| `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_PAT` / `GEMINI_API_KEY` | if `LLM_MODE` is not `off` | the LLM provider chain in `LLM_CHAIN` |
| `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` | eval only | daily bars for the evaluation sanity check |

`make test` should then pass. Thresholds and weights live in `config.yaml`, not in code.

## Running it

Two terminals:

```bash
make api      # API on http://127.0.0.1:8000/api/v1 (one worker; it also runs the data recorder and alerts)
make ui       # Streamlit on http://127.0.0.1:8501
make web      # TypeScript app on http://127.0.0.1:5173 (first run: cd frontend && npm install)
```

Then, with the API running:

| Command | Does |
| --- | --- |
| `make ingest` | fetch markets, filings and macro data now, through `POST /admin/ingest`; prints progress |
| `make backfill WINDOW=2026-07-01..2026-07-12` | backfill market quotes for a replay window |
| `make flip` | demo: force a `BREACH_FLIP` alert to Slack (see below) |
| `make warm [AS_OF=2026-10-11T14:00:00Z]` | ask the demo questions once so the LLM briefs are cached |
| `make eval` | print the five-check evaluation report (lane B) |
| `make test` | run all tests |

`make api` runs `airscen.main:app`, which wires every lane together. `airscen.api:app` alone also starts, without the
background worker.

### Pages

- **Run**: the question form (company, as-of time, note size, tenor, spread, coverage threshold, fuel pass-through, peers),
  the answer, the six-scenario grid, flags and the brief. The LLM brief appears when ready (the page checks every 2 s for
  up to 60 s); until then, or if it fails the number check, the template brief is shown.
- **Proof**: recomputes the stored answer from the filings, Treasury yield, baseline oil price and trusted quotes
  visible at that as-of time. Each check is pass or fail. A written brief counts only when every number in it already
  appears in the run or a sourced fact. The formulas are the decision; a language model writes labels and the brief.
- **Exposure map**: the markets that bear on the decision, by factor, with direction and trust. LLM-written labels are
  marked "LLM (unvalidated)".
- **Peers**: the same question for United and American with the same fuel curve and Fed odds but their own financials, plus operating margin (adjusted revenue when every company has it).
- **Sensitivity**: for the last run, moves note size, coupon spread, coverage threshold, and fuel pass-through one at a time and shows which one crosses the 20% breach-probability alert line. The fuel curve and Fed odds stay at that run.
- **Alerts**: the alert feed (undelivered alerts are marked) and a "Send test alert" button.
- **Replay**: slide `as_of` back (default 2026-07-10) and rerun with only the data visible then.

## Five-minute demo

1. **Ask the question** (Run page): DAL, $1,000M, 7 years, 150 bp, your coverage threshold. Show the answer, the grid,
   the flags and the brief. Point out that every number in the brief traces to the run or a sourced fact.
2. **Proof**: open Proof. It applies the quarterly formulas again to the sourced inputs and shows the pass or fail
   of each check, including the written brief. Say that the language model did not produce the breach probability.
3. **Exposure map**: fuel markets outrank the Fed market for Delta.
4. **Peers**: the three-airline table with `PERIOD_MISMATCH` where quarters differ.
5. **Replay**: slide to 2026-07-10 and run. Compare the scenario range with what the September quarter showed; the run
   carries `BACKFILL_INPUTS` when it uses backfilled quotes.
6. **Alert**: run `make flip`. It evaluates the watched company twice, once at a 3.0x coverage threshold and once at 6.0x;
   on the worked example the breach probability goes from about 7.4% to 29.7%, crossing 20%, so a real `BREACH_FLIP`
   goes through the alert state machine to Slack. A second `make flip` within 60 minutes sends nothing (cooldown).
7. **Evaluation**: `make eval`, and say its limits out loud (below).

Before the demo, to make it immune to network failures: with `LLM_MODE=live`, run `make warm AS_OF=<the time you will
use>`, set `LLM_MODE=cache_only` in `.env`, restart `make api`, and in the Run page untick "Use the current time" and enter
that same time. A brief is cached only for the exact question and as-of time; the Replay page's default (2026-07-10
20:00 UTC) is always warmed.

## Assumptions (also listed in every brief)

- **Crude only** (`CRUDE_ONLY`): fuel cost moves with WTI; the jet fuel crack spread is not modeled.
- **Resolve date proxy** (`RESOLVE_DATE_PROXY`): the WTI market's resolve date stands in for the whole quarter.
- Fuel and rate states are **independent**: a scenario's probability is the product of the two.
- The WTI survival curve's **tail points** (one threshold spacing beyond the lowest and highest market) are an assumption.
- The new note adds its first-year interest each quarter; its tenor does not change the quarterly math.
- The 3.0x default coverage threshold is a **placeholder**: no covenant has been verified, so set your own.
- Trust weights, thresholds and the 0.3/0.4/0.3 scenario weights are defaults, not fitted.

## Limits we state upfront

- One replay is a case study, not a statistical result.
- No claim of predictive edge, and no market-to-stock correlation or causal claim.
- The brief is judged on faithfulness to its inputs, not on foresight (the LLM may know how events turned out).
- Not investment advice; nothing here trades or hedges.

## Illustrative versus verified numbers

- **Verified** (checked against Delta's September-quarter 2026 8-K, filed 2026-10-09): operating income $1,454M, net
  interest $146M, fuel burned 1,146M gallons, debt and finance leases $12,851M (current $3,324M), liquidity $6.9B.
- **Illustrative** (the worked example in LLD section 6 and `tests/fixtures/worked_example.json`): the WTI ladder, the Fed
  quotes, the baseline WTI of 85, the 5.5% coupon (a 10-year Treasury of 4.00% in tests), and so the golden results
  (breach probability 0.297, breakevens 103.16 / 102.77). Test-only market ids contain `ILLUSTRATIVE`; peer test data is
  marked synthetic. Live runs replace all of these with real quotes and FRED data.
- The 22% variable-rate share is Delta's June 30, 2026 figure, so runs flag `VARIABLE_SHARE_STALE`.

## Status and open items (2026-10-11)

Lanes A, B and C are on `main` together: the real store and worker (A), the engine, LLM client, labels and
evaluation (B), and the API, briefs, alerts and app (C). `make test` runs every lane's tests; lane C's also run on
lane A's real in-memory store (`tests/product/test_real_store.py`).

Still open:

| Item | Effect |
| --- | --- |
| `.env` keys (`SEC_USER_AGENT`, `FRED_API_KEY`, and LLM keys if you want a written brief) | `make api` will not load settings, and ingest cannot fetch FRED, until those are filled in. The app and the tests run without them. |
| July 10 Treasury yield | filled from FRED: the print visible on July 10 is the July 9 yield, 4.54% (`realtime_start` 2026-07-10). The replay note is $1,000M at 6.04% (that yield plus the 150 bp demo spread). There is no July 10 observation. |
| United and American variable-rate share | the June 30, 2026 10-Qs do not state a share. United discloses $11.2B of floating-rate principal subject to increased-cost provisions; American lists facilities one by one. Neither share is derived, so both stay out of the peer table. |
| Calibration liquidity | `calibration.yaml` has the last price and the outcome, not volume, so the Brier score is reported and the liquidity buckets are not |
| Two demo rehearsals | still to do together |
