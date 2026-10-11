# Lane B — Model (`feat/model`)

You own: `trust.py`, `factors.py`, `scenario.py`, `llm.py`, `scout.py`, `eval/*`, `hand_labels.yaml`, `tests/fixtures/worked_example.json`, `tests/fixtures/ladder.json`, `tests/test_architecture.py`, and tests for these.
Read first: LLD §1 (DD-5, DD-6, DD-8), §3 import rules, §4 models and invariants, §6 (all of it), the LLM contract in §7, §9 test map.
Rule for engine modules (`trust`, `factors`, `scenario`): pure functions, import only `types`, stdlib and numpy. No network, no database, no clock.

Format of each item: task → LLD reference → **Done when**.

---

## CP0 — Contract freeze (all three)

- [x] **B0.1** Review `types.py` with A and C, especially `ScenarioInputs`, `ScenarioRow`, `ScenarioSummary`, `FuelCurve`, `RateStates`.

  Accepted on the merged tree. Models match LLD §4 plus D1 and D2. Two clarifications are recorded in CP0_DECISIONS D7 and D8: breakeven WTI is optional so a pass-through of 0 survives JSON, and `fuel_curve` takes `as_of` because the engine cannot read the clock.
- [x] **B0.2** Write `tests/fixtures/worked_example.json` and `ladder.json` from the §6 worked example (inputs and every expected output: six rows, breakevens 103.16 / 102.77, `p_breach` 0.297, P10/P50/P90 = 80.00 / 94.44 / 118.00, liquidity cover 2.08). Mark them illustrative. C needs these now. **Done when** committed at CP0.

  Both files are on `main` and `tests/model/test_fixtures.py` locks them. Operating income is the 2-decimal formula value (1590.43, 1196.42, 553.57). The LLD display table rounds those three cells.
- [ ] **B0.3** Get LLM keys: Snowflake MLH trial (mlh.link/snowflake) and a Gemini AI Studio key. **Done when** both are in your `.env`.

  Not done. This checkout has no `.env`. `.env.example` lists `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_PAT` and `GEMINI_API_KEY`.
- [ ] **CP0 merged**

  Code and fixtures are on `main`. This box stays open until B0.3 and C0.3 are done and someone re-runs the smoke curls.

## Block 1 → CP1 Foundations

- [x] **B1** `trust.clean()` and `trust.trust_score()` per §6 "Quote cleaning and trust": drop crossed or out-of-range quotes, stale after 300 s, four components, weights from config, floor 0.40, backfill cap 0.50. **Done when** tests cover each component, a crossed quote, a stale quote, and the backfill cap.
- [x] **B2** `factors.rate_states()`: per-meeting markets in one `group_key`, normalise, fold cut into hold, +25 bp hike, venue choice by trust (Kalshi on tie), `venue_gap`, flags `RATES_NO_MARKET_INPUT` and `VENUE_DISAGREE`. **Done when** illustrative quotes give π_hike 0.17 and π_hold 0.83, and a missing hike market returns nothing plus the flag.
- [x] **B3** `factors.fuel_curve()` and `fuel_states()`: group selection (≥3 trusted thresholds, latest resolve ≤ as_of + 100 days), monotone fix, tail points at one median spacing, linear interpolation, inverse for P10/P50/P90 with weights 0.3/0.4/0.3, touch cross-check (`LADDER_INCONSISTENT`), always set `CRUDE_ONLY` and `RESOLVE_DATE_PROXY`. **Done when** `ladder.json` gives 80.00 / 94.44 / 118.00 and <3 thresholds returns nothing plus `FUEL_NO_MARKET_INPUT`.
- [x] **B4** `scenario.run_scenarios()`: six rows, `breach = cover < theta` (always true when OI ≤ 0), probabilities null if an input is missing, breakevens, `p_breach = Σ π_r · S(W*_r)`, κ = 0 edge case, liquidity cover. Raise `DataError` (`E_MISSING_FACT`) for missing required facts. **Done when** `test_scenario_golden` matches the worked example to 2 decimals.
- [x] **B5** Property tests (hypothesis): coverage never rises with WTI or with note size; six-row probabilities sum to 1 within 1e-9 or are all null. **Done when** green.
- [x] **B6** `tests/test_architecture.py`: parse imports and enforce the 7 rules in §3. **Done when** it passes on the current tree and fails if you add `import airscen.store` to `scenario.py`.
- [ ] **CP1 merged** — your part of the exit test: golden test green on `main`.

## Block 2 → CP2 First real Delta run

- [x] **B7** `llm.py`: `complete_json` / `complete_text`; `cache_key = sha256(task, prompt_version, input)` using the store's `cache_get` / `cache_put` (fake in-memory cache until A merges); chain from `LLM_CHAIN`; modes `live` / `cache_only` / `off`; move on for connection errors, 429, 402, 5xx; one repair retry on bad JSON; budget guard (120 calls/hour, 20,000 input tokens); log provider, model, tokens, latency, cache hit; temperature 0. **Done when** fake-provider tests cover cache hit (no call), fallback order, repair retry, budget error, and `cache_only` miss.

  `tests/model/test_llm.py` and `tests/fakes/fake_llm.py` cover those cases. The client does not import the store. Cache rows match the `llm_cache` columns. `off` raises `E_LLM_UNAVAILABLE` without reading the cache. A JSON array is returned when the schema type is array, because `extract_metrics` is a list.
- [ ] **B8** One real Snowflake Cortex call with `claude-haiku-4-5` via the OpenAI-compatible endpoint, then one real Gemini call. Check the trial credit balance and any daily cap (LLD open item). **Done when** both work, and the findings are in the README notes for C.

  Not done. `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_PAT` and `GEMINI_API_KEY` are empty, so no call was made and the credit balance was not checked. README is Lane C's file; notes for C are here instead. The client is wired for `POST https://<account>.snowflakecomputing.com/api/v2/cortex/v1/chat/completions` with bearer `SNOWFLAKE_PAT`, model `claude-haiku-4-5`, and `response_format.type = json_schema`. Gemini uses `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions` with bearer `GEMINI_API_KEY` and model `gemini-3.8-flash` (the current Flash id in Google's docs on 2026-10-10). `config.yaml` still has the placeholder Gemini id; the client ignores a model value that starts with `<`. Ollama is `OLLAMA_HOST` plus `/v1/chat/completions`, default model `llama3.1`.
- [ ] **B9** `hand_labels.yaml` + `scout.candidates()`: keyword prefilter over A's market list; hand-label 15–30 shortlist markets (next Fed meeting hold/hike/cut, the WTI close-above ladder, CPI, Hormuz) with factor, direction, mechanism. Check A's parsed `kind`, `threshold`, `group_key`, `outcome` for each. **Done when** the shortlist is labeled in the DB and `factors` produces a fuel curve and rate states from live quotes (or you know exactly why not).

  `candidates` and `hand_labels` are in, with 19 Kalshi markets observed on 2026-10-10 (next meeting `KXFEDDECISION-26OCT`, six `KXWTI-26OCT1314` close-above strikes, one touch cross-check, four October CPI strikes, three Hormuz markets). A's `adapters/kalshi.py` is still `NotImplementedError`, so those kind/threshold/group_key/outcome values are this file's reading of the public titles, not a check of A's parser. Polymarket search returned HTTP 403. The shortlist is not in the DB: `Repository.upsert_market` and the worker are still Lane A stubs. A one-off read of the public quotes at 2026-10-11T01:10Z, applying these overrides by hand, did feed the engine: fuel curve on `KXWTI-26OCT1314` with P10/P50/P90 87.06/91.81/96.49 and flags `CRUDE_ONLY`, `RESOLVE_DATE_PROXY`; rate states `p_hike` 0.154 on `KXFEDDECISION-26OCT` with no rate flag. `KXWTI-26OCT1314-T100.99` and the touch market were below the trust floor, so they were not inputs. Those live numbers were not stored.
- [ ] **CP2 merged** — your part of the exit test: live markets feed the engine for the DAL run.

  Not merged. The engine can score the live quotes above, but the DAL run still needs Lane A's store and adapters and Lane C's `POST /runs`.

## Block 3 → CP3 Full features

- [ ] **B10** `scout.label()` with the LLM: JSON schema = `MarketLabel`, cached by `content_hash`, hand labels always win, LLM failure leaves the market unlabeled. **Done when** a fake-LLM test passes and live labels exist for non-shortlist candidates (shown as "unvalidated" in the brief).
- [ ] **B11** Peer margin comparison as a pure helper (e.g. in `scenario.py`): adjusted revenue when all compared companies have it, else GAAP with `MARGIN_BASIS_MISMATCH`. **Done when** a test with Delta's refinery numbers ($2,601M of $20,186M) picks adjusted.
- [ ] **B12** First replay run as of 2026-07-10 using A's backfill. Sanity-check the outputs by hand. **Done when** it completes with `BACKFILL_INPUTS` and you have written down what the fuel range looked like.
- [ ] **CP3 merged**

## Block 4 → CP4 Eval and demo freeze (`python -m airscen.eval`, opens the DB read-only)

Build in this order; the first two are never cut.

- [ ] **B13** `eval/checks.py` — number correctness: `extract` output vs `overrides.yaml` for all three airlines; Delta's debt identity (3,324 + 9,527 = 12,851) and balance sheet (33,575 + 29,960 + 22,513 = 86,048). **Done when** it prints pass/fail per metric.
- [ ] **B14** `eval/replay.py` — Delta replay as of 2026-07-10 (guide about $3.15/gal) vs the September quarter (adjusted $3.61/gal, coverage about 10x). The test: was the market-implied high-fuel state non-trivial in July and does the range bracket the outcome? **Done when** it prints the July scenario range next to the realised values, with "one case study, not a statistical result".
- [ ] **B15** `eval/ablation.py` (cut first) — rerun the replay with only the company's own fuel assumption. Converting $3.15/gal into a WTI level is an assumption; state how you did it. **Done when** it prints both errors.
- [ ] **B16** `eval/faithfulness` (cut second) — run C's `brief.check_numbers` over stored LLM briefs, report the pass rate. **Done when** it prints a rate and sample size.
- [ ] **B17** `eval/calibration.py` (cut third) — Brier score vs the 0.25 of always saying 50%, plus a reliability table by liquidity bucket, on A's resolved markets. **Done when** it prints both with the sample size.
- [ ] **B18** `python -m airscen.eval` runs all available checks and prints one report ending with the stated limits. Tune trust weights / thresholds on the replay only if time remains, and record any change. **Done when** `make eval` works on `main`.
- [ ] **CP4 merged**
