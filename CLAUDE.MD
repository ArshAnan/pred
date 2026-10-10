# Project context: Market-Implied Scenario Analyst

Hackathon MVP for Hackknight 2026 (Diameter Capital, Best Financial Hack). Last updated 2026-10-10.
Status: design complete (data source audit, high-level design, low-level design). Implementation is next.

This file is the shared starting point for teammates and for AI coding assistants. Read it first.
"Verified" means checked against official docs or filings on 2026-10-09. "Illustrative" and "not verified" mean exactly that.
To have Claude Code load it automatically, rename or copy it to `CLAUDE.md`.

## 1. What we are building

A tool that answers a corporate-finance decision question about a company by turning prediction-market odds into explicit, auditable scenarios.

Demo question: "Can Delta comfortably service a new $1B, 7-year senior note, and how does it compare with United and American?"

How it answers:

1. Pull the company's financials from SEC filings.
2. Find Kalshi and Polymarket markets that bear on the decision (oil prices, Fed decisions).
3. Convert market odds into fuel-price and interest-rate scenarios.
4. Compute interest coverage (operating income divided by net interest expense) under each scenario.
5. Report the probability coverage falls below a user-set threshold, the breakeven oil price, and a liquidity check, with a sourced brief.
6. Alert when the conclusion would flip.

The output is a conditional answer ("given today's market-implied odds and these stated assumptions"), never a forecast or a trading signal.

## 2. Hackathon context

- Event: Hackknight 2026, hosted by Diameter Capital Partners. Track: Core Track 01, "Best Financial Hack", Option 1 "Prediction Markets as a Financial Signal".
- Prize: an interview with Diameter Capital (Summer 2027 intern and full-time roles).
- Judging criteria: clear pain point (a defined question or user), technical soundness (sensible methods and sound data handling), insight (surfaces information that could not easily be seen before).
- Option 1 ideas on the flyer: detect and explain sudden market moves; connect markets to the companies they affect; test whether markets lead or lag stock prices; build a monitoring tool or AI assistant. Data hints: Kalshi public API, Polymarket, SEC EDGAR; pairing market data with filings or news helps.
- Feedback from the track contact after our pitch:
  - He liked the concept and is very curious how we engineer the evaluation step.
  - He does NOT want a specific market-to-company correlation, KPI link, or next-day stock performance.
  - He wants high-level business questions answered. Example he gave: a company planning a bond issue, can it pay it off, how is it positioned against competitors and securities. Examples only, not must-haves.

## 3. Problem and pain points

Corporate finance, treasury and credit teams decide under uncertainty about external events such as oil prices and interest rates. Scenario analysis is manual and slow. Prediction markets price those events in real time, but nobody connects them to a specific company's balance sheet. Three gaps stop people from using them:

- Discovery: thousands of markets exist; which ones matter for this company?
- Translation: how does an event probability become a financial impact for this company?
- Trust: which markets are liquid and reliable enough to use?

## 4. Scope

In scope (v1):

- One decision type done well: debt issuance and serviceability, with peer comparison.
- Three US airlines: Delta (DAL), United (UAL), American (AAL).
- Sources: Kalshi, Polymarket, SEC EDGAR, FRED, plus Alpaca (evaluation sanity checks) and GDELT (alert headlines).
- Fuel (WTI) and rates (Fed decision) as the two modeled factors; inflation, demand and geopolitics are labeled for the exposure map but not modeled.
- Scenario engine, trust scoring, sourced brief, decision-anchored alerts to a Slack channel, replay mode, and an evaluation harness.

Out of scope:

- Trading signals, next-day stock prediction, market-to-stock correlation claims, causal claims.
- Replacing credit ratings or giving investment advice.
- Private companies, non-US filings, proprietary data (Bloomberg, full TRACE), bond-level pricing.
- Sending alerts to real companies, executing trades or hedges.
- Other decision types (M&A, capex), Monte Carlo or correlation modeling, regression-based link validation (stretch only).
- Accounts, Docker, hosting.

## 5. Demo companies

| Company | Ticker | CIK | Data state (2026-10-09) |
| --- | --- | --- | --- |
| Delta Air Lines | DAL | 27904 (verified), SIC 4512 | September-quarter results released 2026-10-09 (8-K) |
| United Airlines Holdings | UAL | confirm via `company_tickers_exchange.json` | not audited; Delta's 10-Q for the quarter not checked either |
| American Airlines Group | AAL | confirm via `company_tickers_exchange.json` | not audited |

Why airlines: a live fuel shock, rate exposure through variable-rate debt, a near-term maturity bump, and Delta owns a refinery (so peer comparison forces proper GAAP versus adjusted handling).

## 6. Delta facts (September-quarter 2026 8-K, filed 2026-10-09; verified)

Source: https://www.sec.gov/Archives/edgar/data/27904/000002790426000035/deltaairlinesannouncessept.htm

- Revenue: $20,186M GAAP; $17,585M adjusted (excludes $2,601M of third-party refinery sales).
- Operating income $1,454M; margin 7.2% GAAP, 9.4% adjusted. Net interest expense $146M. Nine-month operating income $3,819M against $442M net interest.
- Debt and finance leases $12,851M: current $3,324M (was $1,605M at year-end), noncurrent $9,527M. Cash $3,787M.
- Adjusted net debt $13,350M. Liquidity $6.9B including a $3.1B undrawn revolver. Gross leverage guided to about 2.2x.
- Fuel: 1,146M gallons burned; adjusted price $3.61 per gallon (up 60% year over year); December-quarter guide about $4.25 at the October 2 forward curve. Delta says it absorbed more than $500M of fuel cost above its early-July guidance.
- Full-year EPS guidance $5.10 to $5.60, cut from $6.50 to $7.50 given in July.
- From the June-quarter release (2026-07-10): weighted average interest rate 4.9%, 78% fixed and 22% variable at June 30, 2026; July guide used about $3.15 per gallon (forward curve of July 2).
- Balance sheet check: 33,575 + 29,960 + 22,513 = 86,048 (liabilities plus equity equals assets).

## 7. Architecture at a glance

Two processes on one machine, both bound to localhost, plus one optional:

| Process | Command | Address |
| --- | --- | --- |
| API server (FastAPI, one uvicorn worker, hosts the worker loop) | `uvicorn airscen.api:app --host 127.0.0.1 --port 8000 --workers 1` | 127.0.0.1:8000, base path `/api/v1` |
| Streamlit app (API client only) | `streamlit run app.py --server.address 127.0.0.1 --server.port 8501` | 127.0.0.1:8501 |
| Ollama (optional, last-resort LLM) | `ollama serve` | 127.0.0.1:11434 |

SQLite is the only datastore (WAL mode); only the API process writes it.

Design decisions:

- DD-1: Streamlit calls only the API; only the API process writes SQLite.
- DD-2: one API process, one uvicorn worker, hosting the background worker loop. Never run more than one worker.
- DD-3: UTC ISO-8601 timestamps; probabilities are floats in [0, 1]; money is USD millions in fields ending `_usd_m`.
- DD-4: every read takes an `as_of` time (enables replay, prevents look-ahead). There is no "latest" query.
- DD-5: engine modules (`trust`, `factors`, `scenario`) are pure functions that import only `types`.
- DD-6: LLMs write labels and prose, never numbers used in math. Every LLM output is schema-checked and cached. A template brief always exists.
- DD-7: vendor quirks stay inside adapters, which return domain types only.
- DD-8: LLM provider chain is Snowflake Cortex (MLH trial), then Gemini free API tier, then local Ollama, plus a cache-only mode.

Package `airscen`: `types`, `config`, `store`, `adapters/{http,kalshi,polymarket,edgar,fred,alpaca,gdelt}`, `llm`, `extract`, `scout`, `trust`, `factors`, `scenario`, `brief`, `service`, `alerts`, `worker`, `api`, `ui/client`, `app.py` (Streamlit), `eval/`, `cli`.

Dependency rules: `types` imports nothing; engine modules import only `types`, stdlib and numpy; adapters import only `types` and `adapters.http`; `api` imports only `service`, `store` (reads) and `types`; `ui` imports only `types`; only `store` opens the database. A test enforces this.

One run, end to end: `POST /runs` loads facts, quotes and macro as of `as_of`, computes trust, factor states and scenarios, stores the run with a template brief, and returns `RunResult` (201). An LLM brief is generated in a background task and fetched by polling `GET /runs/{run_id}`; it replaces the template only if `check_numbers` passes.

Background flows: quote recorder every 60 s; market discovery and labeling at startup and every 6 hours; filings watcher every 30 minutes; macro refresh every 6 hours; alert evaluation after each recorder tick; backfill for replay on demand.

API endpoints (all under `/api/v1`): `GET /health`, `GET /companies`, `GET /companies/{ticker}/facts`, `GET /markets`, `GET /markets/{venue}/{market_id}/history`, `POST /runs`, `GET /runs/{run_id}`, `GET /runs`, `GET /alerts`, `POST /admin/ingest`, `GET /admin/jobs/{job_id}`, `POST /admin/alerts/test`.
Errors use one envelope: `{"error": {"code", "message", "details", "request_id"}}` with codes `E_VALIDATION` (400), `E_UNAUTHORIZED` (401), `E_NOT_FOUND` (404), `E_CONFLICT` (409), `E_MISSING_FACT` (422), `E_INTERNAL` (500), `E_SOURCE_UNAVAILABLE` (502), `E_STORE_UNAVAILABLE` (503).
`POST /runs` takes an `Idempotency-Key` header. Streamlit timeouts: connect 2 s, read 10 s (GET) and 30 s (`POST /runs`); GET retries twice; polls every 2 s for up to 60 s.

## 8. Data sources (audited 2026-10-09)

| Source | Base URL | Auth | Key facts and quirks |
| --- | --- | --- | --- |
| Kalshi | `https://external-api.kalshi.com/trade-api/v2` | none for market data | Candlesticks 1, 60 or 1,440 minutes. Settled markets older than `/historical/cutoff` appear only under `/historical/*`. Order book returns YES and NO bids only. Dollar fields are strings; sizes end in `_fp`. Documented Basic budget about 20 requests per second (authenticated); unauthenticated limit not documented. WebSocket needs an API key. |
| Polymarket | Gamma `https://gamma-api.polymarket.com`; CLOB `https://clob.polymarket.com`; Data `https://data-api.polymarket.com` | none | History retention: 1-minute at least 7 days, 5-minute at least 60, 30-minute at least 90, 3-hour and 12-hour permanent; explicit ranges up to 15 days. `outcomePrices` and `clobTokenIds` are JSON strings; V2 markets use `positionIds`. A never-traded token returns 0.5 from last-trade-price (ignore it). Throttled, not rejected. |
| SEC EDGAR | `https://data.sec.gov`, `https://www.sec.gov/Archives` | `User-Agent` with your contact | 10 requests per second cap. XBRL APIs include only standard-taxonomy, entity-wide facts, so geography, segment and per-debt-instrument detail must come from filing text. Submissions update in under a second, XBRL in under a minute. |
| FRED | `https://api.stlouisfed.org/fred` | free API key | Series `DGS10`, `DFF`/`FEDFUNDS`, `DCOILWTICO` (confirm IDs). Vintages via `realtime_start`/`realtime_end`; `output_type=4` gives initial releases. Values are strings. Rate limit not verified. |
| Alpaca | `https://data.alpaca.markets/v2` | free key and secret | IEX-only free plan, history since 2016, latest 15 minutes excluded, 200 requests per minute. Used for evaluation sanity checks only. Bars path not verified. |
| GDELT DOC 2.0 | `https://api.gdeltproject.org/api/v2/doc/doc` | none | Rolling window of the last 3 months; `mode=artlist`. Used for up to 3 headlines in alert payloads. Rate limit not verified. |
| Slack | incoming webhook URL | secret in URL | Alert delivery; 2xx means delivered. |

Market quotes seen on third-party aggregator pages were not verified. Always confirm against the exchange APIs.

## 9. Business logic (engine specification)

Symbols: OI0 operating income, NI0 net interest expense (latest quarter, USD millions); G fuel burned (millions of gallons); D total debt; v variable-rate share of debt; L liquidity; Mcur debt due within 12 months; N note size (USD millions); c coupon (decimal); theta coverage threshold; kappa fuel pass-through (1 means no offset); W_bar mean WTI over the baseline quarter (USD per barrel).

Coupon: `c = (DGS10 + coupon_spread_bp / 100) / 100`, with DGS10 in percent.

Quote trust (defaults in `config.yaml`, not fitted):

```text
mid = (bid + ask) / 2;  spread = ask - bid
f_spread = clip(1 - spread / 0.10, 0, 1)
f_depth  = clip(log10(1 + min(bid_size, ask_size)) / log10(1 + 1000), 0, 1)
f_volume = clip(log10(1 + volume_24h) / log10(1 + ref), 0, 1)      ref = 10,000 (Kalshi), 100,000 (Polymarket)
f_time   = clip(days_to_close / 2, 0, 1)
trust    = 0.35 f_spread + 0.25 f_depth + 0.25 f_volume + 0.15 f_time      feeds the engine if trust >= 0.40 and not stale (300 s)
```

Rate states (Fed decision market for the next meeting): normalise hold, hike, cut mids so they sum to 1. `pi_hike = m_hike / total`, `pi_hold = (m_hold + m_cut) / total`. Hike is +25 bp, hold is 0. Missing markets fix rates at hold and set `RATES_NO_MARKET_INPUT`.

Fuel curve (WTI "close above K" ladder on one resolve date, at least 3 trusted thresholds): force survival non-increasing, add tail points one median spacing beyond each end, interpolate linearly to get S(w) = P(WTI >= w). Presentation states P10, P50, P90 get weights 0.3, 0.4, 0.3 (Swanson's rule). Touch-type markets ("hits X at any time") are never inputs, only a consistency cross-check.

Scenario math (per quarter):

```text
dP_f   = (w_f - W_bar) / 42                    # USD per gallon; 42 gallons per barrel; crude only
OI_f   = OI0 - kappa * G * dP_f
NI_r   = NI0 + v * D * d_r / 4 + N * c / 4
cover  = OI_f / NI_r;   breach = cover < theta
W_star_r = W_bar + 42 * (OI0 - theta * NI_r) / (kappa * G)      # breakeven WTI
p_breach = sum over r of pi_r * S(W_star_r)
liquidity_cover = L / Mcur   (ok if >= 1.0)
```

`tenor_years` is carried for the brief only; it does not change the quarterly math.

Worked example (Delta, illustrative ladder and quotes; golden test fixture): OI0 1,454; NI0 146; G 1,146; D 12,851; v 22%; N 1,000; c 5.5%; theta 6.0; kappa 1.0; W_bar 85; rates hold 0.82, hike 0.17, cut 0.01; WTI survival at 80, 90, 100, 110, 120 = 0.90, 0.62, 0.35, 0.18, 0.08.
Results: breakeven WTI 103.16 (hold) and 102.77 (hike); `p_breach` 0.297; P10, P50, P90 WTI = 80.00, 94.44, 118.00; hold-state coverage 9.96x, 7.49x, 3.47x; liquidity cover 2.08x.

Peers use the same request, fuel curve and rate states but their own financials and W_bar. Compare margins on adjusted revenue when available (Delta's refinery sales distort GAAP revenue).

Alert rules (state machine: ARMED, TRIGGERED, SENDING, COOLDOWN; re-arm only after the condition clears):

- `BREACH_FLIP`: `p_breach` rises to 0.20 or more; re-arm below 0.18; cooldown 60 min; high.
- `DRIVER_MOVE`: a trusted driver market moves 10 points or more within 60 min; cooldown 60 min per market; medium.
- `DATA_STALE`: a required source degraded for more than 15 minutes; once per incident; low.
- Slack delivery: 3 attempts in total, waiting 1 s then 4 s.

Run flags (machine-readable limitations): `STALE_MARKETS`, `FUEL_NO_MARKET_INPUT`, `RATES_NO_MARKET_INPUT`, `LADDER_INCONSISTENT`, `VENUE_DISAGREE`, `PERIOD_MISMATCH`, `VARIABLE_SHARE_STALE`, `FACT_CONFLICT`, `REF_WTI_OVERRIDE`, `BACKFILL_INPUTS`, `BACKFILL_GAP`, `MARGIN_BASIS_MISMATCH`, `CRUDE_ONLY`, `RESOLVE_DATE_PROXY`.

## 10. LLM plan (zero cost)

Only three LLM steps: `extract` (press-release text to facts), `scout` (label markets by factor and direction), `brief` (prose from scenario numbers). No LLM output is used in math.

Provider chain (`LLM_CHAIN=snowflake,gemini,ollama`):

1. Snowflake Cortex via the MLH 120-day trial (mlh.link/snowflake). OpenAI-compatible endpoint `https://<account>.snowflakecomputing.com/api/v2/cortex/v1/chat/completions`, bearer programmatic access token. Start with `claude-haiku-4-5`. AI Credit is $2.00; `claude-haiku-4-5` costs 0.50 input and 2.50 output credits per million tokens. Our whole workload (about 0.8M input and 0.17M output tokens) is about 0.8 credits (about $1.65). Check the trial's credit balance and any daily cap first (an older Snowflake page mentioned about 1 credit per day for trial accounts; current pages do not repeat it).
2. Gemini API free tier with an AI Studio key. The free student Google AI Pro plan does NOT cover API keys (Google says plan benefits apply only to the AI Studio web UI).
3. Ollama (local), last resort.

The Google Cloud $300 trial is not in the chain (per Google's docs it cannot pay for the Gemini API in AI Studio or for partner models offered as managed APIs).

Cache everything in `llm_cache`. After one warm-up pass (`make warm`), run the demo with `LLM_MODE=cache_only` so a network failure cannot break it.

## 11. Evaluation plan (what we show judges)

Five checks, run by `make eval`:

| Check | Question | Method |
| --- | --- | --- |
| Number correctness | Are extracted figures right? | Compare `extract` output with hand-checked `overrides.yaml`; accounting identities (debt and balance sheet above) |
| Market calibration | Can we trust the odds? | Brier score against the 0.25 of always saying 50%, plus a reliability table by liquidity bucket, on resolved Fed, CPI and WTI markets |
| Delta replay | Would the system have helped then? | Run as of 2026-07-10 (June-quarter release, guide about $3.15 per gallon); compare the scenario range with the September quarter (adjusted $3.61, coverage about 10x) |
| With and without markets | Do the odds add anything? | Rerun the replay with only the company's own fuel assumption |
| Brief faithfulness | Does every number trace to a source? | `check_numbers` pass rate |

Automated tests (pytest): golden scenario test from the worked example; property tests (coverage never rises with WTI or note size, probabilities sum to 1); `as_of` visibility (on October 8 the October 9 8-K facts must be invisible); extraction validation (current plus noncurrent debt equals total within 0.5%, assets equal liabilities plus equity, every number appears in its evidence text); LLM cache and fallback order with fake providers; API contract tests; alert state machine; import-rule test.

Limits to state upfront: one replay is a case study, not a statistical result; no claim of predictive edge; the brief is judged on faithfulness to its inputs, not foresight.

## 12. Rules for contributors and AI assistants

- Do not fabricate data. Label illustrative numbers as illustrative and unverified assumptions as unverified.
- Follow DD-1 to DD-8 and the dependency rules in section 7.
- All times UTC ISO-8601; probabilities in [0, 1]; money in USD millions with the `_usd_m` suffix.
- Every read goes through `as_of` helpers. Never query "latest" directly.
- Only `store.py` opens SQLite. Streamlit never touches the database or imports the engine.
- Keep engine functions pure. No network, no database, no clock inside `trust`, `factors`, `scenario`.
- Parse vendor quirks (Kalshi dollar strings, Polymarket protocol versions) inside adapters only.
- Respect rate limits. Client rates: Kalshi 10 req/s, Polymarket 10 req/s, EDGAR 5 req/s with a real `User-Agent`, Alpaca 3 req/s, FRED 2 req/s, GDELT 1 req/s.
- Secrets live in `.env` only and are never committed. Thresholds and weights live in `config.yaml`.
- Prefer the smallest change. Add a component only to fix a failure seen in a smoke test or replay.

## 13. Repo layout, config and commands

```text
airline-scenarios/
  .env.example
  companies.yaml     # DAL, UAL, AAL: ticker, CIK, peers
  overrides.yaml     # hand-checked lender metrics, used if extraction fails
  config.yaml        # poll intervals, trust weights, alert rules, LLM limits
  data/app.db        # SQLite
  data/raw/          # 8-K and 10-Q text, error bodies
  src/airscen/       # package described above
  app.py             # Streamlit
  eval/              # checks, calibration, replay, ablation
  tests/             # fixtures and pytest suites
  Makefile
```

`.env` keys: `SEC_USER_AGENT` (required), `FRED_API_KEY` (required), `ALPACA_KEY_ID` and `ALPACA_SECRET_KEY` (eval only), `SLACK_WEBHOOK_URL`, `SNOWFLAKE_ACCOUNT` and `SNOWFLAKE_PAT`, `GEMINI_API_KEY`, `OLLAMA_HOST` (default `http://127.0.0.1:11434`), `LLM_MODE` (`live`, `cache_only`, `off`), `LLM_CHAIN`, `API_HOST`, `API_PORT`, `API_BASE_URL` (default `http://127.0.0.1:8000/api/v1`), optional `API_TOKEN`, optional `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`.

Commands: `make setup`, `make api`, `make ui`, `make ingest`, `make backfill WINDOW=2026-07-01..2026-07-12`, `make warm`, `make eval`, `make test`.

SQLite tables (7): `facts`, `markets`, `snapshots`, `macro`, `runs`, `alerts`, `llm_cache`.

## 14. Build plan

Phases, each with a done-when test:

1. Setup and smoke tests: keys in `.env`, curl checks against every source, confirm United and American CIKs, start the snapshot recorder (Polymarket's fine-grained history cannot be recovered later).
2. Delta facts: EDGAR loader, `extract` with `overrides.yaml`, number-correctness check.
3. Markets: shortlist 15 to 30 markets (Fed decisions, CPI, WTI thresholds, Hormuz), label and trust-score them.
4. Scenario and brief for Delta, then United and American via `companies.yaml`.
5. App, worker and alert: Streamlit with an `as_of` slider, worker loop, Slack message on a forced flip.
6. Evaluation: calibration table, July 10 replay, with-and-without comparison, faithfulness check.
7. Rehearse the demo twice and write assumptions into the README.

Cut order if time runs short: with-and-without comparison, faithfulness check, calibration table (keep spread and depth trust), peer scenarios beyond a plain table, Slack (show alerts in the app). Never cut: the `as_of` store, the Delta replay, the number-correctness check.

Work lanes: Data (ingest, store, recorder, extract); Model (scout, trust, scenario, eval); Product (brief, app, worker alerts, demo script).

Five-minute demo: ask the Delta question at today's `as_of` and show brief, grid and peer table; show the exposure map (fuel markets outrank the Fed market for Delta); slide `as_of` back to July 10 and compare with what happened; show a Slack alert firing when the answer flips; finish on the evaluation report and its limits.

## 15. Open items

- Confirm the Kalshi series for the WTI ladder and the Fed decision through `/series` and `/markets` (need at least 3 thresholds on one resolve date).
- Check the Snowflake trial's credit balance, any daily cap, and which Cortex models the region offers.
- Confirm United and American CIKs and press-release layouts; hand-fill `overrides.yaml` for them.
- FRED and GDELT rate limits and Alpaca's bars path are unverified.
- Check Polymarket API access from the demo machine.
- Trust weights, thresholds and Swanson weights are defaults; tune on the July 10 replay.
- Team hours and size decide how many cut lines we use.

## 16. Full documents

These are private to the owner until shared; ask for access.

- Data source audit: https://claude.ai/code/artifact/41bd4de1-23b2-431c-82f7-4ac33b6d7251
- High-level design: https://claude.ai/code/artifact/e404c221-93e5-4eeb-bea4-010c59e3a273
- Low-level design: https://claude.ai/code/artifact/7669f6f9-12f4-41d2-9bef-795ac5fc17fc