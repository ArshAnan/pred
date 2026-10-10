# High-Level Design: Market-Implied Scenario Analyst (Hackathon MVP)

Oct 9, 2026 · @Arsh Anand

## Design at a glance

The MVP is one Python repo with one SQLite file: scripts pull free data in, a small engine turns it into a market-implied scenario brief for Delta, United and American, and one loop sends alerts. Only three steps call an LLM; every number comes from deterministic code.

&#91;embedded content: architecture · 5 sources, 1 store, 5 modules, 3 outputs\]

Read top to bottom: five free sources, one ingest layer, one store, five engine modules, three outputs. Run two processes: a worker loop that records quotes, reruns scenarios and checks alerts, and the Streamlit app. Everything else is a command.

## Components

Thirteen small modules, one file each, in one Python package. Companies are configuration (`companies.yaml`: ticker, CIK, peers), so United and American reuse the Delta code path.

| Module | Job | Source or tech | LLM |
| --- | --- | --- | --- |
| `ingest/edgar.py` | resolve CIKs; load filing history and company facts; download the 8-K exhibit and latest 10-Q or 10-K text | SEC EDGAR, 10 requests per second cap, contact User-Agent | no |
| `ingest/markets.py` | find markets; record bid, ask, size and volume snapshots for a shortlist | Kalshi REST `/markets`, `/orderbook`; Polymarket Gamma and CLOB | no |
| `ingest/macro.py` | pull 10-year yield, fed funds rate and WTI with vintages | FRED `series/observations` | no |
| `ingest/prices_news.py` | daily bars for sanity checks; news for explanations on demand | Alpaca free key; GDELT DOC | no |
| `store.py` | SQLite tables and `as_of` query helpers | `sqlite3`, pandas | no |
| `extract.py` | turn the 8-K exhibit into lender metrics, check totals against the balance sheet, fall back to `overrides.yaml` | LLM with a JSON schema | yes |
| `scout.py` | keyword-search candidate markets, then label each with a risk factor, direction and mechanism; cache the labels | LLM with a JSON schema | yes |
| `trust.py` | weight each market 0 to 1 from spread, depth at the best price, 24-hour volume and days to close | plain Python | no |
| `scenario.py` | fuel by rates grid, probabilities from trusted markets, coverage and breach probability for the new note | plain Python | no |
| `brief.py` | write the brief from scenario JSON; every number carries a source id | LLM | yes |
| `worker.py` | loop: record snapshots, rerun scenarios, check alert rules | plain Python | no |
| `app.py` | brief, exposure map, scenario table, replay slider | Streamlit | no |
| `eval/` | run the checks in the evaluation section and print a report | plain Python | no |

One LLM provider, with its key in `.env`. The three LLM modules return validated JSON or prose built from supplied numbers, and none of them does arithmetic.

## One run, end to end

A run answers one question for one company at one `as_of` time: can it service a new note of a given size, tenor and coupon? The user supplies those three inputs; the coupon is the FRED 10-year yield plus a spread input.

1. **Load facts.** Read GAAP figures and lender metrics for the company and its peers as of `as_of`, using only rows filed on or before it.
2. **Load markets.** Read the latest snapshots for the labeled shortlist and drop markets below a trust floor set in config.
3. **Turn odds into factor states.** Fuel: read the WTI threshold ladder as points on a probability curve and bucket it into low, base and high. Rates: use the Fed decision market for the next meeting as hold or hike.
4. **Build the grid.** Three fuel states by two rate states gives six scenarios; each probability is the product of its two factor probabilities, and the brief states that independence assumption.
5. **Compute outcomes.** For each scenario, apply the formulas below to the latest reported quarter.
6. **Summarize.** Report probability-weighted coverage, the probability coverage falls below a user-set threshold, and the fuel price at which it does. Add a liquidity check against near-term maturities, and the same table for each peer.
7. **Write the brief.** The LLM receives the scenario JSON and the facts, each with a source id, and writes a one-page brief that lists assumptions and marks LLM-labeled market links as unvalidated.
8. **Watch.** The worker repeats steps 2 to 6 on every poll and alerts when the breach probability crosses its threshold or a driver market moves by more than a configured number of points.

| Quantity | Formula | Delta input from the audit |
| --- | --- | --- |
| Fuel cost change | change in $ per gallon × gallons burned | 1,146M gallons in the September quarter |
| WTI to jet fuel | change in $ per barrel ÷ 42, crude only, crack spread held constant | 42 gallons per barrel |
| Variable interest change | variable share × total debt × change in rate | 22% variable at June 30, 2026; $12,851M debt at September 30 |
| New note interest | size × coupon | user inputs |
| Coverage | operating income ÷ net interest expense | $1,454M ÷ $146M = 10.0x (derived) |
| Liquidity check | liquidity against debt due within 12 months | $6.9B liquidity; $3,324M current maturities |

Worked example for one quarter, derived from those inputs: fuel +$0.50 per gallon cuts operating income by $573M, so coverage falls from 10.0x to about 6.0x. Adding a $1B note at an assumed 5.5% coupon raises quarterly interest by about $14M, taking coverage to about 5.5x.

## Data model, config and layout

Six SQLite tables hold everything. Raw filings and API responses sit in `data/raw/` so any number can be traced back.

| Table | Key columns |
| --- | --- |
| `facts` | company, metric, value, unit, basis (GAAP or adjusted), period\_end, filed\_at, source\_url |
| `markets` | venue, market\_id, title, rules, close\_time, factor, direction, mechanism, labeled\_at |
| `snapshots` | venue, market\_id, ts, bid, ask, bid\_size, ask\_size, last, volume\_24h, open\_interest |
| `macro` | series\_id, obs\_date, value, realtime\_start |
| `runs` | run\_id, company, as\_of, inputs\_json, results\_json, brief\_md |
| `alerts` | ts, company, rule, payload\_json, sent |

Every read goes through `as_of` helpers that return only rows with `filed_at`, `ts` or `realtime_start` at or before that time. That one rule powers replay mode and prevents look-ahead.

Secrets live in `.env`; thresholds and lists live in `config.yaml`.

| Key | Used for | Needed |
| --- | --- | --- |
| `SEC_USER_AGENT` | EDGAR requests (name and email) | yes |
| `FRED_API_KEY` | rates and oil series | yes |
| `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` | daily bars and news | yes |
| `LLM_API_KEY` | extract, scout and brief | yes |
| `SLACK_WEBHOOK_URL` | alerts to a demo channel | yes |
| `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` | authenticated rate tier or a later WebSocket | optional; market data is public |

Polymarket and GDELT need no key. `config.yaml` holds the trust floor, alert thresholds, poll interval and the factor list. Scout labels five factors (fuel, rates, inflation, demand, geopolitics) for the exposure map, but the grid models only fuel and rates in the MVP.

```text
airline-scenarios/
  .env.example
  companies.yaml     # DAL, UAL, AAL: ticker, CIK, peers
  overrides.yaml     # hand-checked lender metrics if extract fails
  config.yaml        # trust floor, alert rules, poll interval, factors
  data/app.db        # SQLite
  data/raw/          # 8-K and 10-Q text, API responses
  src/ingest/        # edgar.py markets.py macro.py prices_news.py
  src/               # store.py extract.py scout.py trust.py scenario.py brief.py worker.py
  app.py             # Streamlit
  eval/              # checks.py calibration.py replay.py ablation.py
  Makefile           # ingest, worker, app, eval
```

## Evaluation in the MVP

Five checks ship, all computed from data already in SQLite; the Delta replay is the headline result. Everything runs with `make eval` and prints one report.

| Check | Question it answers | Method |
| --- | --- | --- |
| Number correctness | Are extracted figures right? | compare `extract.py` output with `overrides.yaml` for all three airlines; assert Delta's current plus noncurrent debt ($3,324M + $9,527M) equals its $12,851M total |
| Market calibration | Can we trust the odds? | for resolved Fed, CPI and WTI markets, take the last price before resolution; report Brier score and a reliability table by liquidity bucket |
| Delta replay | Would the system have given a useful answer then? | run the question as of July 10, 2026 and compare the scenario range with what the September quarter showed |
| With and without markets | Do the odds add anything? | rerun the replay with only the company's own fuel assumption and compare errors |
| Brief faithfulness | Does every number trace to a source? | extract numbers from the brief and verify each against scenario JSON or facts; report the pass rate |

The replay uses a question whose answer is known. On July 10, 2026, Delta's June-quarter release guided an all-in fuel price of about $3.15 per gallon, using the forward curve of July 2 ([release](https://ir.delta.com/news/news-details/2026/Delta-Air-Lines-Announces-June-Quarter-2026-Financial-Results/default.aspx)). The September quarter then came in at an adjusted $3.61, and Delta says it absorbed more than $500M of fuel cost above that guidance ([October 9 8-K](https://www.sec.gov/Archives/edgar/data/27904/000002790426000035/deltaairlinesannouncessept.htm)). Ground truth is deterministic: realized fuel price and coverage computed from filed numbers. The test is whether the market-implied high-fuel scenario was non-trivial in July and brackets the outcome.

Limits to state in the pitch:

- One replay is a case study, not a statistical result.
- The replay needs July market history. Kalshi serves candlesticks and Polymarket keeps 3-hour bars permanently, so confirm coverage in the smoke tests.
- The brief is judged on faithfulness to its inputs, not on foresight, because the LLM may know how events turned out.

Stretch, only if time remains: replays for United and American once their September filings land, and an event-study of market links against abnormal stock returns.

## What we deliberately left out

Every cut below keeps the demo working with fewer parts. The rule for adding anything back: it must remove a failure we have actually hit in a smoke test or a replay.

| Left out | Instead | Why that is safe | Add back when |
| --- | --- | --- | --- |
| Postgres or a time-series database | one SQLite file | a few dozen markets polled every few seconds is small, with one writer | several writers, or data outgrows a laptop |
| Vector database | keyword search plus LLM labeling of a short list | three airlines and one sector give a few hundred candidates | many sectors |
| Agent framework | three plain functions with JSON-schema outputs | each LLM step is one call with no tool loop | steps need retries or tool use |
| Queue or scheduler service | one `worker.py` loop | polling stays inside limits: about 20 Kalshi requests per second at the Basic tier, 300 Gamma `/markets` requests per 10 seconds | polling hundreds of markets |
| WebSocket streaming | REST polling | Kalshi's stream needs an API key, and alerts at minute scale do not need ticks | sub-second reaction matters |
| Monte Carlo and correlation modeling | a three by two scenario grid with stated independence | two factors, and explicit assumptions are easier to defend | a third modeled factor |
| Regression to validate market links | exposure measured from the company's own disclosures (gallons burned, variable debt share) | no noisy return estimates to explain away | markets have enough history for an event study |
| Stock prices in the core path | daily bars in the evaluation sanity check only | the question is about credit capacity, not price moves | adding stock-reaction analysis |
| Real alert delivery to companies | a Slack webhook to a demo channel | no contact with real firms | a design partner exists |
| Bond-level pricing | FRED yield plus a spread input | free data does not cover it, and the assumption is shown | licensed data is available |
| Dimensional XBRL parsing | LLM extraction of the press release plus `overrides.yaml` | three companies and a handful of metrics | many issuers |
| Accounts, Docker, hosting | a virtual environment and a Makefile | the demo runs on one laptop | deploying for others |

## Build plan and demo

Build in this order, each phase with a done-when test. Hours and team size are not known yet, so the plan is phases and cut lines rather than dates.

1. **Setup and smoke tests.** Create the keys in `.env`, run the curl checks from the audit, confirm the United and American CIKs, and start the snapshot recorder. Done when all sources return data and snapshots are being written.
2. **Delta facts.** EDGAR loader, `extract.py` with `overrides.yaml`, and the number-correctness check. Done when Delta's lender metrics pass the balance-sheet assertion.
3. **Markets.** Shortlist 15 to 30 markets on Fed decisions, CPI, WTI thresholds and Hormuz, then label and trust-score them. Done when the exposure map lists each market with factor, direction and trust.
4. **Scenario and brief.** Grid, coverage, breach probability and the written brief for Delta, then United and American through `companies.yaml`. Done when the three-airline table renders with source ids on every number.
5. **App, worker and alert.** Streamlit with an `as_of` slider, the worker loop, and a Slack message on a forced flip.
6. **Evaluation.** Calibration table, the July 10 replay, the with-and-without comparison and the faithfulness check.
7. **Rehearse.** Run the demo script twice and write the assumptions into the README.

If time runs short, cut in this order: the with-and-without comparison, the faithfulness check, the calibration table (keep the spread and depth trust score), peer scenarios beyond a plain table, then Slack (show the alert in the app instead). Never cut the `as_of` store, the Delta replay or the number-correctness check.

Three work lanes fit a team of three; with two people, merge the model lane into the other two:

| Lane | Owns |
| --- | --- |
| Data | ingest, store, recorder, `extract.py` |
| Model | scout, trust, scenario, `eval/` |
| Product | brief, app, worker alerts, demo script |

Five-minute demo:

1. Ask whether Delta can service a $1B, 7-year note at today's `as_of`; show the brief, scenario grid and peer table.
2. Open the exposure map and show that fuel markets outrank the Fed market for Delta.
3. Slide `as_of` back to July 10 and show what the markets implied then against what happened.
4. Show the Slack alert firing when the answer flips.
5. Finish on the evaluation report, including its stated limits.

Open items: the team's available hours, the LLM provider, and the United and American CIKs and September-quarter filing dates.
