# Lane C — Product (`feat/product`)

You own: `service.py`, `api.py`, `brief.py`, `alerts.py`, `cli.py`, `ui/client.py`, `app.py`, `Makefile`, `README.md`, and tests for these.
Read first: LLD §1 (DD-1, DD-2, DD-6), §2 runtime rules, §5 run lifecycle, §6 "Inputs and required data", "Peers" and "Alert rules", all of §7, §8 failure table and observability.
Rules: Streamlit (`app.py`, `ui/client.py`) imports only `types` and an HTTP library, never the store or engine. `api` imports only `service`, `store` (reads) and `types`. One uvicorn worker.

Format of each item: task → LLD reference → **Done when**.

---

## CP0 — Contract freeze (all three)

- [x] **C0.1** Review `types.py` with A and B, especially `RunRequest`, `RunResult`, `Brief`, `Alert`, and the new `HealthStatus`, `MarketView`, `RunSummary`.

  Accepted on the merged tree. `RunRequest` enforces the §4 bounds and rejects an `as_of` more than a minute ahead or without a timezone. `ApiError` carries `code`, `message`, `request_id` and `status`. The D7 breakeven clarification is what `tests/product/test_run_roundtrip.py` locks.
- [x] **C0.2** `Makefile` with targets `setup`, `api`, `ui`, `test` working now; `ingest`, `backfill`, `warm`, `eval` as placeholders (§9 runbook). **Done when** `make test` runs pytest.

  `tests/product/test_makefile.py` covers the four working targets, the one-worker API command, and the four loud placeholders.
- [ ] **C0.3** Create a Slack incoming webhook for a demo channel. **Done when** it is in your `.env`.

  Not done. This checkout has no `.env`. `.env.example` lists `SLACK_WEBHOOK_URL`.
- [ ] **CP0 merged**

  The Makefile and the reviewed types are on `main`. This box stays open until C0.3 and B0.3 are done.

## Block 1 → CP1 Foundations (build against B's `worked_example.json`, not live data)

- [ ] **C1** `api.py` skeleton: FastAPI app under `/api/v1`, lifespan runs `load_settings()` and `store.migrate()`, `X-Request-ID` middleware, one JSON log line per request, optional `X-API-Token`, 64 KB body limit, exception handlers that turn every `AirScenError` into the §7 error envelope with the right HTTP code. `GET /health` (in-memory status only) and `GET /companies`. **Done when** API tests cover 200 on health, the envelope shape for 400/404, and 401 when `API_TOKEN` is set.
- [ ] **C2** `brief.template_brief(result)` and `brief.check_numbers(md, result)`: template lists every summary number, the flags and assumptions (`CRUDE_ONLY`, `RESOLVE_DATE_PROXY`, independence of fuel and rates); `check_numbers` rejects any number not in the input. **Done when** both §9 brief tests pass on the worked example.
- [ ] **C3** `ui/client.py`: `ApiClient` with connect 2 s, read 10 s GET / 30 s `POST /runs`, GET retries (0.5 s, 1 s) on connection errors, 502, 503; no automatic POST retry; `ApiError(code, message, request_id, status)`. **Done when** tests against a fake server pass.
- [ ] **C4** `app.py` Run page: form (ticker, `as_of`, note size, tenor, spread, threshold, pass-through, peers), result (summary, six-row grid, flags, template brief), error banner, health banner. Point it at a temporary stub endpoint that returns the worked example. **Done when** the page renders the golden numbers.
- [ ] **CP1 merged** — your part of the exit test: `make api` starts and `/health` returns 200 on `main`.

## Block 2 → CP2 First real Delta run

- [ ] **C5** `service.RunService.create_run(req)`: one `as_of` for all reads; required facts or 422 `E_MISSING_FACT` naming them; coupon `c = (DGS10 + spread/100)/100`; W̄ = mean `DCOILWTICO` in the baseline quarter with ≥30 observations, else `wti_ref_override` + `REF_WTI_OVERRIDE`, else 422; markets → `trust` → `factors` → `scenario`; flags `VARIABLE_SHARE_STALE`, `STALE_MARKETS`, `FACT_CONFLICT`; save run with template brief and `data_version`. Adapters and repo arrive as constructor args. **Done when** a fake repo seeded from the worked example returns the golden numbers end to end, and a missing `variable_debt_share` gives 422.
- [ ] **C6** Endpoints: `POST /runs` (201; same `Idempotency-Key` within 10 min → 200 with the original run; `as_of` > now + 1 min → 400), `GET /runs/{id}`, `GET /runs`, `GET /companies/{ticker}/facts`, `GET /markets` (`MarketView` with trust), `GET /markets/{venue}/{id}/history`. **Done when** the §9 API contract tests pass.
- [ ] **C7** `POST /admin/ingest` (202 with `job_id`, 409 if the same source group is running) and `GET /admin/jobs/{id}`; `cli.py` `ingest` / `backfill` call them; wire `make ingest`. **Done when** `make ingest` starts A's jobs through the API.
- [ ] **C8** Start A's `WorkerLoop` in the lifespan and stop it on shutdown; feed its status into `/health`. **Done when** `/health` shows per-source status from the running worker.
- [ ] **CP2 merged** — your part of the exit test: `POST /runs` for DAL returns 201 from live data and the Run page shows it.

## Block 3 → CP3 Full features

- [ ] **C9** `brief.llm_brief(result, llm)` (`write_brief`, ≤400 words, input is the run JSON with source ids) and `service.finish_brief(run_id)` as a FastAPI background task; replace the template only if `check_numbers` passes, else `rejected`; all providers fail → `failed`. UI polls `GET /runs/{id}` every 2 s for up to 60 s. **Done when** a fake-LLM test covers ready, rejected and failed, and a real DAL brief reaches `ready`.
- [ ] **C10** Peers: with `include_peers`, run UAL and AAL with the same request, fuel curve and rate states but their own facts and W̄; `PERIOD_MISMATCH`; B's margin helper. Peers page. **Done when** the three-airline table renders with flags.
- [ ] **C11** `alerts.py`: state machine ARMED → TRIGGERED → SENDING → COOLDOWN; `BREACH_FLIP` (≥0.20, re-arm below 0.18, 60 min cooldown), `DRIVER_MOVE` (≥10 points in 60 min, per market), `DATA_STALE` (once per incident); dedup key `company:rule:market_id`; payload with before/after, drivers, `run_id`, up to 3 GDELT headlines (best effort); Slack 3 attempts (1 s, 4 s). Register `evaluate` as the worker's `after_market_tick` for companies in `alerts.watch`. **Done when** state-machine, hysteresis, dedup and Slack-failure tests pass with fake Slack.
- [ ] **C12** `GET /alerts`, `POST /admin/alerts/test`, Alerts page (marks undelivered alerts). Add a way to force a flip for the demo (e.g. run with a temporarily lowered threshold via config) and document it. **Done when** a forced `BREACH_FLIP` reaches the Slack channel.
- [ ] **C13** Exposure map page (markets by factor with direction and trust; LLM labels marked unvalidated) and Replay page (`as_of` slider, banner showing the `as_of` in force). **Done when** sliding to 2026-07-10 shows a run with `BACKFILL_INPUTS`.
- [ ] **CP3 merged**

## Block 4 → CP4 Eval and demo freeze

- [ ] **C14** `make warm` runs the demo and replay queries once to fill `llm_cache`; document switching to `LLM_MODE=cache_only`. Wire `make eval` to B's module. **Done when** the demo runs with the network LLM disabled.
- [ ] **C15** README: setup, commands, the five-minute demo script (CLAUDE.md §14), assumptions, and stated limits (one replay is a case study; no predictive claim; brief judged on faithfulness; illustrative vs verified numbers). **Done when** a teammate can follow it from a clean clone.
- [ ] **C16** Rehearse the demo twice with A and B, timing each step. **Done when** two clean runs.
- [ ] **CP4 merged** — tag `v1.0-demo`.
