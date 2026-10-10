# Team Plan: Market-Implied Scenario Analyst (Hackknight 2026)

Source of truth: **Low-Level Design (LLD), Oct 10, 2026**. If this plan and the LLD disagree, the LLD wins; fix this plan at the next checkpoint.
LLD section numbers used below: §1 Scope and decisions, §2 Architecture and runtime, §3 Modules, §4 Data classes and schema, §5 Data flow, §6 Business logic, §7 Interface contracts, §8 Error handling, §9 Configuration, tests and runbook.

Lane files: `LANE_A_DATA.md`, `LANE_B_MODEL.md`, `LANE_C_PRODUCT.md`. Commit all four to `docs/plan/` on `main` at CP0.

## 1. Lanes, branches, file ownership

Only the owner edits a file between checkpoints. Need a change in someone else's file? Ask them, or note it for the next checkpoint.

| Hacker | Lane | Branch | Owns |
| --- | --- | --- | --- |
| A | Data | `feat/data` | `config.py`, `store.py`, `adapters/*`, `extract.py`, `worker.py`, `overrides.yaml`, tests for these, recorded vendor fixtures in `tests/fixtures/vendors/` |
| B | Model | `feat/model` | `llm.py`, `scout.py`, `trust.py`, `factors.py`, `scenario.py`, `eval/*`, `hand_labels.yaml`, `tests/fixtures/worked_example.json`, `tests/fixtures/ladder.json`, `tests/test_architecture.py`, tests for these |
| C | Product | `feat/product` | `service.py`, `api.py`, `brief.py`, `alerts.py`, `cli.py`, `ui/client.py`, `app.py`, `Makefile`, `README.md`, tests for these |

Shared files, changed only at a checkpoint or by a tiny "contract PR" to `main` announced in team chat (everyone rebases right after):
`types.py`, `pyproject.toml` / `requirements.txt`, `config.yaml`, `.env.example`, `CLAUDE.md`.

Why this split: A owns everything that touches the outside world or the database, B owns everything pure or model-shaped, C owns everything a user or judge sees. `worker.py` sits with A because its jobs are data flows (§5); C plugs alert evaluation in through a callback, so neither edits the other's file.

## 2. Gaps in the LLD to settle at CP0 (proposed answers)

Proposed answers: see CP0_DECISIONS.md.

These are things the LLD references but does not define. Agree on them before splitting.

- `HealthStatus`, `MarketView`, `RunSummary` are used in §7 but not defined in §4. Define them in `types.py` at CP0 (shapes: §8 "Observability" for `HealthStatus`; `MarketMeta` + `MarketLabel` + latest `Snapshot` + `TrustResult` for `MarketView`; `run_id, company, as_of, created_at, p_breach, llm_status` for `RunSummary`).
- The exception hierarchy (§8) has no module. Engine modules may import only `types` (DD-5) and `scenario` raises `DataError`, so put the exceptions in `types.py`.
- Hand labels ("hand labels win", §5) have no file. Use `hand_labels.yaml`, owned by B.
- `E_LLM_*` and `E_SOURCE_*` codes are not in the API error table. They should never reach the API (the brief falls back to the template); if one does, map it to 500 `E_INTERNAL`.
- Worker-to-alerts hook: `WorkerLoop(..., after_market_tick: Callable[[datetime], None] | None)`. C registers the alert evaluator in the API lifespan.
- Package layout: `src/airscen/` installed with `pip install -e .`.

## 3. Checkpoints

Every checkpoint uses the merge protocol in section 4. The exit test must pass **on `main`** before anyone starts the next block.

| Checkpoint | Goal | Exit test on `main` |
| --- | --- | --- |
| **CP0 Contract freeze** (all three together, A at the keyboard) | Skeleton, `types.py`, stubs, keys | `make test` runs (0 failures); `python -c "import airscen.types"` works; every lane's stubs import; each person's `.env` passes their smoke curls |
| **CP1 Foundations** | Each lane's core works in isolation with fakes | `make test` green; `make api` starts and `GET /api/v1/health` returns 200; golden scenario test passes |
| **CP2 First real Delta run** | Live data → engine → API → UI | `make api`, `make ingest`, then `POST /runs` for DAL returns 201 with real facts and live markets (or honest `FUEL_NO_MARKET_INPUT` / `RATES_NO_MARKET_INPUT` flags); the Streamlit Run page shows it with the template brief |
| **CP3 Full features** | LLM brief, peers, alerts, replay | LLM brief for DAL reaches `ready`; peer table shows UAL and AAL; `POST /admin/alerts/test` delivers to Slack; a forced `BREACH_FLIP` appears in the feed; a run with `as_of=2026-07-10` completes with `BACKFILL_INPUTS` |
| **CP4 Eval and demo freeze** | Evidence for judges | `make eval` prints the five-check report; `make warm` then `LLM_MODE=cache_only` demo runs end to end twice; tag `v1.0-demo` |

Rough load per block is balanced, but nobody knows the hours yet. If a lane finishes early, pull the next unchecked item from the most-behind lane after asking its owner.

## 4. Merge protocol (every checkpoint)

1. **Freeze.** Announce "CP<n> merge". Finish or stash the current item; do not start a new one.
2. **Each hacker, on their branch:**
   ```bash
   git fetch origin
   git rebase origin/main
   make test                         # must be green for your lane's tests
   git push --force-with-lease
   ```
   Open (or update) a PR to `main`.
3. **Merge in order A → B → C** (C depends on both). After each merge, the next person runs `git fetch && git rebase origin/main && make test` again, pushes, then merges. Use squash merges with titles like `CP2: data lane`.
4. **Conflict rules.**
   - `types.py`: keep the union of fields; never rename or delete a field silently. Renames need all three people.
   - `requirements.txt` / `pyproject.toml`: keep the union, sort, pin nothing new without telling the team.
   - `config.yaml`: keep the union of keys; values in the LLD win.
   - Anything else: the file owner resolves.
5. **On `main`, all three together:** run `make test`, then the checkpoint's exit test. Whoever owns a failing module fixes it directly on a `fix/cp<n>-*` branch and merges before new work starts.
6. **Everyone rebases** onto the new `main`, ticks the checkpoint box in their lane file, and continues.

Between checkpoints: commit after every checked item (`git commit -m "[A5] kalshi adapter parses quotes"`) and push often, so nothing is lost.

## 5. Prompting rules for AI assistants

Start each AI coding session with:

> Read `CLAUDE.md` and `docs/plan/LANE_<X>.md`. Then read LLD §<n>. Implement only checklist item <ID>. Edit only files my lane owns. Follow DD-1 to DD-8 and the import rules. Write the tests named in "Done when" first, then the code. Do not invent data; label anything illustrative.

- One checklist item per prompt. Check it off only when its "Done when" is true.
- If the AI wants to change a shared file, stop and raise it in team chat.
- Paste real API responses into `tests/fixtures/vendors/` instead of letting the AI guess payload shapes.

## 6. Cut order if time runs short (from the HLD and LLD)

Cut in this order: with-and-without comparison → faithfulness check → calibration table (keep trust scoring) → peer scenarios beyond a plain table → Slack (show alerts in the app).
**Never cut:** the `as_of` store, the Delta replay, the number-correctness check.
