# Lane A — Data (`feat/data`)

You own: `config.py`, `store.py`, `adapters/{http,kalshi,polymarket,edgar,fred,alpaca,gdelt}.py`, `extract.py`, `worker.py`, `overrides.yaml`, recorded vendor fixtures, and tests for these.
Read first: LLD §2, §3, §4 (schema and visibility rules), §5, the adapter and retry tables in §7–§8, §9.
Your consumers: B (store for the LLM cache and eval data), C (Repository for `service.py`, worker status for `/health`).

Format of each item: task → LLD reference → **Done when**.

---

## CP0 — Contract freeze (all three, you drive the keyboard)

- [x] **A0.1** Create the repo layout from CLAUDE.md §13 with `src/airscen/`, `pyproject.toml`, `.gitignore` (`.env`, `data/`, `logs/`, `__pycache__`), `.env.example` (LLD §9 keys), `config.yaml` (LLD §9 defaults). **Done when** `pip install -e .` works.
- [x] **A0.2** `types.py`: paste the LLD §4 models verbatim, add the exception hierarchy from §8, and add `HealthStatus`, `MarketView`, `RunSummary` (TEAM_PLAN §2). **Done when** it imports and the team agrees.

  Agreed contract clarification: all shared Pydantic models are frozen. Model creation validates the LLD §4 rules assigned to `Fact`, `MarketLabel`, and `RunRequest`. Snapshot cleaning, fuel-curve checks, and six-row probability checks remain with their designated components. The shared models and validators import successfully, and the focused contract tests pass.
- [x] **A0.3** Stub every public function in the LLD §3 table for all modules (correct signatures, `raise NotImplementedError`). Each owner then replaces their own stubs. **Done when** `pytest` collects with 0 errors.
- [x] **A0.4** Smoke curls from the data source audit for SEC (with `SEC_USER_AGENT`), FRED, Kalshi `/markets`, Polymarket Gamma. Save one real response per source to `tests/fixtures/vendors/`. **Done when** all four return data from your machine.

  Recorded on 2026-10-10. SEC is Delta's submissions (`cik` 0000027904, SIC 4512). FRED is one `DGS10` observation. Kalshi `/markets` and Polymarket Gamma are live payloads; neither is the Fed or WTI shortlist. Confirming those series is A14, not this item.
- [ ] **CP0 merged** (protocol in TEAM_PLAN §4)

  The three branches are on `main`, and `make test` is green. The code contract (types, stubs, `Settings`, `Adapters`, fixtures, Makefile, `pyproject.toml` dependencies, `tests/conftest.py`) matches CP0_DECISIONS. Still open: B0.3 and C0.3, because this checkout has no `.env`, so the exit test's smoke curls cannot be re-run from here.

## Block 1 → CP1 Foundations

- [x] **A1** `config.py`: `load_settings()` reads `.env` + `config.yaml` into a validated `Settings`; startup checks from §2 rule 2. **Done when** a missing `SEC_USER_AGENT` raises `ConfigError` naming it.
- [x] **A2** `store.py` schema: `migrate()` runs the §4 SQL exactly (WAL, `busy_timeout`, foreign keys, 7 tables). All write methods (`upsert_facts`, `put_snapshots`, `upsert_market`, `put_macro`, `save_run`, `update_run_brief`, `save_alert`, `cache_put`) use one short transaction each. **Done when** migrate on a temp file creates all 7 tables and a round-trip test passes per table.
- [x] **A3** `store.py` `as_of` reads with the §4 visibility and tie-break table: `latest_facts` (newest `filed_at`, source priority xbrl > override > parser > llm, only `validated=1`, `FACT_CONFLICT` signal on >0.5% disagreement), `latest_snapshot` (+ stale flag), `snapshot_series`, `macro_series` (newest vintage), `markets` (hand label beats llm), `cache_get`, `data_version`. **Done when** the §9 store test passes: with `as_of=2026-10-08` the Oct 9 8-K facts are invisible and the June-quarter facts show.
- [x] **A4** `adapters/http.py`: `SourceClient` with timeouts (3 s / 10 s / 20 s EDGAR / 30 s total), retry policy, `Retry-After`, per-host token bucket, 429 halves the rate for 60 s, circuit breaker (5 failures → 60 s open → one probe) — §8. **Done when** tests against a fake server cover retry, no-retry on 4xx, and breaker open/close.
- [x] **A5** `adapters/kalshi.py`: `list_markets(series)`, `quote(ticker)`, `candles(...)`, `historical_cutoff()`; Decimal parsing of dollar strings, `_fp` sizes, fill `MarketMeta.kind/threshold/group_key/outcome`. **Done when** recorded responses parse into `MarketMeta` and `Snapshot` with probabilities in [0, 1].
- [x] **A6** `adapters/polymarket.py`: `search`, `quotes` (batch ≤500), `history`; JSON-string fields, V2 `positionIds`, ignore 0.5 last-trade with an empty side. **Done when** recorded Gamma, CLOB and Data responses parse.
- [x] **A7** `adapters/edgar.py`: `resolve_cik` (from `company_tickers_exchange.json`), `recent_filings`, `fetch_document` (cache under `data/raw/edgar/`), `xbrl_facts`. Confirm UAL and AAL CIKs and write them into `config.yaml` (shared file: announce it). **Done when** DAL resolves to 27904 and the Oct 9 8-K downloads.
- [x] **A8** `adapters/fred.py`: `observations(series_id, start, end, as_of)` with vintages, `.` as missing. Confirm the series IDs (`DGS10`, `DCOILWTICO`). **Done when** a recorded response parses into `MacroObs`.

  `DGS10` parses from the recorded observation (5.22 on 2026-10-08). `DCOILWTICO` was not re-fetched here: this checkout has no `FRED_API_KEY`, and FRED returns 400 without one. The id is the one named in the LLD. UAL CIK 100517 and AAL CIK 6201 are in `config.yaml` (from `company_tickers_exchange.json`). KXWTI on 2026-10-13 has more than 3 close-above thresholds; KXFEDDECISION includes the October 2026 meeting. Those series are wired in A14.
- [ ] **CP1 merged** — your part of the exit test: store, http and adapter tests green on `main`.

## Block 2 → CP2 First real Delta run

- [x] **A9** `overrides.yaml` for DAL from the Sept-quarter 8-K (CLAUDE.md §6): operating income, net interest, gallons, fuel price, debt total/current, liquidity, adjusted net debt, revenue GAAP/adjusted, assets/liabilities/equity; plus `variable_debt_share` 0.22 with period June 30, 2026 and `filed_at` 2026-07-10. Load them as `extraction=override`, `validated=1`. **Done when** `latest_facts("DAL", now)` returns every required metric (OI0, NI0, G, D, v). Hand-check every number against the filing; do not copy from memory.
- [x] **A10** `extract.validate()`: the five rules in §8 "Extraction validation". **Done when** Delta's numbers pass rules 1, 2, 4 and 5, and a batch with an invented number fails rule 3.
- [x] **A11** `extract.extract_facts()` using `llm.complete_json("extract_metrics", ...)` (B's `llm.py`; use a fake client until B merges). Whole batch rejected on any failure → overrides. **Done when** a test with a fake LLM output produces validated facts, and a bad output falls back to overrides.
- [x] **A12** `worker.py`: `WorkerLoop` with `tick_markets` (quote recorder every 60 s), `tick_filings` (30 min), `tick_macro` (6 h), market discovery at startup and every 6 h (calls B's `scout`; use the stub until merged), per-source status `{status, last_ok_at, fail_streak}`, degraded after 15 min, `after_market_tick` hook, stop after current batch. One transaction per tick. **Done when** a test with fake adapters runs 3 ticks, skips a failing source, and calls the hook once per tick.
- [x] **A13** Expose a status object C can read for `/health` (DB ok, per-source status, `last_snapshot_age_s`, worker `last_tick_at`). **Done when** C confirms the shape. Shape on this branch: `status()` is `(dict[str, SourceStatus], WorkerStatus)`, plus `last_snapshot_age_s()` and `db_ok()`. C has not merged a reader yet.
- [ ] **A14** Live load for Delta: run discovery, filings, macro. Confirm the Kalshi series for the Fed decision and the WTI close-above ladder (≥3 thresholds on one resolve date) — LLD open item. Start the recorder and leave it running. **Done when** the DB holds DAL facts, `DGS10` and `DCOILWTICO` rows, and snapshots for the shortlist are growing.
- [ ] **CP2 merged** — your part of the exit test: `make ingest` fills the DB on `main`.

## Block 3 → CP3 Full features

- [ ] **A15** UAL and AAL: run extraction on their latest releases, hand-fill `overrides.yaml`, confirm press-release layouts. **Done when** both have all required metrics, or a clear note of which metric is missing and why.
- [ ] **A16** Replay data for July 10: DAL June-quarter facts (filed 2026-07-10) hand-filled from that release, FRED observations visible at that date, and backfill. **Done when** `latest_facts("DAL", 2026-07-10)` returns June-quarter values.
- [ ] **A17** Backfill job: Kalshi 60-min candles and Polymarket 3-hour history for the shortlist over `WINDOW=2026-07-01..2026-07-12`, `src` set to backfill, gaps reported. Confirm the July coverage really exists (HLD limit). **Done when** `make backfill WINDOW=...` writes snapshots and B can run the replay.
- [ ] **A18** `adapters/gdelt.py` (`headlines(query, before, hours, n)`, failures ignored by callers) and `adapters/alpaca.py` (`daily_bars`; verify the bars path — open item). **Done when** recorded responses parse.
- [ ] **A19** (optional) When a 10-Q arrives, cross-check against XBRL and set `FACT_CONFLICT`. Skip if behind.
- [ ] **CP3 merged** — your part of the exit test: replay run at July 10 finds facts and backfilled quotes.

## Block 4 → CP4 Eval and demo freeze

- [ ] **A20** Data for B's calibration check: resolved Fed, CPI and WTI markets with the last price before resolution and the outcome (from Kalshi `/historical/*` and Polymarket). **Done when** B has a table they can load through the store.
- [ ] **A21** Confirm `overrides.yaml` is hand-checked for all three airlines (B's number-correctness check depends on it). **Done when** each value has a source URL comment.
- [ ] **A22** Help rehearse: run the full demo from a clean clone on the demo machine (Polymarket access from that machine is an open item). **Done when** it runs.
- [ ] **CP4 merged**
