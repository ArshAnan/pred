# CP0 Decisions: closing the LLD gaps and avoiding merge conflicts

Status: **accepted** on `main` (2026-10-10). The merged code follows Parts 1 and 2. Part 3 records clarifications found while checking that merge against the LLD. Treat this file as frozen like the LLD. Each decision says who it unblocks.

---

## Part 1 — The six gaps

### D1. Missing API models (`HealthStatus`, `MarketView`, `RunSummary`)

The LLD's §7 uses them but §4 never defines them. Add to `types.py` at CP0. Shapes follow §8 "Observability" and the `GET /markets` and `GET /runs` endpoints.

```python
class SourceStatus(BaseModel):
    status: Literal["ok", "degraded", "down"]
    last_ok_at: Optional[datetime] = None
    fail_streak: int = 0

class LLMStatus(BaseModel):
    mode: Literal["live", "cache_only", "off"]
    last_provider: Optional[str] = None
    calls_last_hour: int = 0
    cache_hit_rate: Optional[float] = None

class WorkerStatus(BaseModel):
    last_tick_at: Optional[datetime] = None
    degraded: bool = False            # three consecutive failed ticks

class HealthStatus(BaseModel):
    status: Literal["ok", "degraded", "down"]
    db: Literal["ok", "down"]
    sources: dict[str, SourceStatus]  # keys: kalshi, polymarket, edgar, fred, alpaca, gdelt, slack
    last_snapshot_age_s: Optional[float] = None
    llm: LLMStatus
    worker: WorkerStatus

class MarketView(BaseModel):          # one row of GET /markets
    meta: MarketMeta
    label: Optional[MarketLabel] = None
    latest: Optional[Snapshot] = None
    trust: Optional[TrustResult] = None
    stale: bool = False

class RunSummary(BaseModel):          # one row of GET /runs
    run_id: str; company: str; as_of: datetime; created_at: datetime
    p_breach: Optional[float]; flags: list[str]
    llm_status: Literal["pending", "ready", "failed", "rejected", "skipped"]
```

Ownership of the data: A fills `sources`, `db`, `worker` and `last_snapshot_age_s`; B fills `llm`; C assembles `HealthStatus` in `api.py`.

### D2. Exceptions live in `types.py`

Reason: `scenario` must raise `DataError` but may import only `types` (DD-5), and the import-rule test would fail otherwise.

```python
class AirScenError(Exception):
    code = "E_INTERNAL"
    def __init__(self, message: str, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.details = details or {}

class ConfigError(AirScenError):  code = "E_CONFIG"
class RequestError(AirScenError): code = "E_VALIDATION"          # also E_UNAUTHORIZED, E_NOT_FOUND, E_CONFLICT
class DataError(AirScenError):    code = "E_MISSING_FACT"        # also E_VALIDATION_FAILED
class SourceError(AirScenError):  code = "E_SOURCE_UNAVAILABLE"  # also _RATE_LIMITED, _BAD_RESPONSE, _AUTH
class LLMError(AirScenError):     code = "E_LLM_UNAVAILABLE"     # also E_LLM_BAD_OUTPUT, E_LLM_BUDGET
class StoreError(AirScenError):   code = "E_STORE_UNAVAILABLE"
```

Rule: always pass the specific code when it is not the default, e.g. `RequestError("Unknown ticker XYZ", code="E_NOT_FOUND")`.

### D3. Error code → HTTP status (one table, in `api.py`, owned by C)

```python
HTTP_STATUS = {
    "E_VALIDATION": 400, "E_UNAUTHORIZED": 401, "E_NOT_FOUND": 404, "E_CONFLICT": 409,
    "E_MISSING_FACT": 422, "E_SOURCE_UNAVAILABLE": 502, "E_STORE_UNAVAILABLE": 503,
}
# any other code (E_LLM_*, E_SOURCE_RATE_LIMITED, E_CONFIG, ...) -> 500 E_INTERNAL, original code kept in details
```

Rule for A and B: never invent a new code without adding it here via a contract PR. `LLMError` should never reach the API: `service.finish_brief` catches it and sets `llm_status`.

### D4. Hand labels: `hand_labels.yaml` (owned by B)

Each entry holds `MarketLabel` fields plus **optional overrides** of the metadata A's adapter parses. The overrides let B correct a wrong `threshold` or `group_key` without editing A's adapter.

```yaml
# Market IDs below are PLACEHOLDERS. Replace with real IDs from Kalshi /markets (item A14).
- venue: kalshi
  market_id: "<WTI close-above 90, resolve date X>"
  relevant: true
  factor: fuel
  yes_moves_factor: up
  mechanism: "Higher WTI raises jet fuel cost, Delta's largest variable expense"
  # optional metadata overrides; only set if the adapter got it wrong
  kind: close_above
  threshold: 90
  threshold_unit: usd_per_bbl
  group_key: "<resolve date>"
```

Flow: B writes `scout.hand_labels(path) -> dict[tuple[Venue, str], tuple[MarketLabel, dict]]`. A's worker calls it during discovery and upserts. A hand label always beats an LLM label (§4).

### D5. Worker wiring (A's `worker.py`, C plugs in)

All dependencies come in through the constructor, so A never imports C's code and C never edits `worker.py`.

```python
class WorkerLoop:
    def __init__(
        self,
        repo: Repository,
        adapters: Adapters,                                    # dataclass of the adapter instances
        settings: Settings,
        label_fn: Callable[[list[MarketMeta]], None],          # B's scout, wrapped by C in the lifespan
        after_market_tick: Callable[[datetime], None] | None = None,   # C's alert evaluator
    ): ...
    def status(self) -> tuple[dict[str, SourceStatus], WorkerStatus]: ...
```

Rules: the worker catches and logs any exception from `after_market_tick` or `label_fn` and keeps running. C builds the `WorkerLoop` inside the API lifespan (DD-2).

### D6. Package and test layout

- `src/airscen/` with `pyproject.toml` (setuptools, src layout), installed with `pip install -e .`; imports are always `from airscen.x import y`.
- All `__init__.py` files created empty at CP0, never edited after.
- Tests split by lane so nobody touches another lane's test files:
  ```text
  tests/
    conftest.py          # CP0 only: tmp DB path, settings fixture; frozen after
    data/                # A: test_store.py, test_adapters.py, test_extract.py, test_worker.py, conftest.py
    model/               # B: test_trust.py, test_factors.py, test_scenario.py, test_llm.py, test_scout.py, conftest.py
    product/             # C: test_service.py, test_api.py, test_brief.py, test_alerts.py, test_client.py, conftest.py
    test_architecture.py # B
    fixtures/            # vendors/ (A), worked_example.json + ladder.json (B)
  ```

---

## Part 2 — Rules that prevent conflicts

### R1. Stubs are the contract
The CP0 stubs fix every public signature from LLD §3. Changing a signature, adding a public function someone else calls, or adding a model field is a **contract PR**: small, straight to `main`, announced in chat, and everyone runs `git fetch && git rebase origin/main` right away. Private helpers (`_name`) need no announcement.

### R2. `types.py` is append-only between checkpoints
Add fields with defaults and new models at the bottom. Never rename, reorder or delete. If two people append at once, the conflict is trivial: keep both.

### R3. Dependencies decided once, at CP0
One list, in `pyproject.toml`. Runtime libraries are `dependencies`; `pytest`, `hypothesis` and `respx` are the `dev` extra. There is no `requirements.txt`. `make setup` installs `-e ".[dev]"`.

```toml
dependencies = [
    "pydantic>=2,<3",
    "pydantic-settings",
    "fastapi",
    "uvicorn[standard]",
    "httpx",
    "pyyaml",
    "python-dotenv",
    "numpy",
    "streamlit",
]
dev = ["pytest>=8,<9", "hypothesis", "respx"]
```

Need something else later? Contract PR.

### R4. `config.yaml` sections are owned
| Section | Owner |
| --- | --- |
| `companies`, `poll` | A |
| `trust`, `fuel`, `scenario`, `llm` | B |
| `alerts` | C |

Add keys only under your own sections. `Settings` in `config.py` (A) must ignore unknown keys inside sections, so B and C can add a key without waiting for A.

### R5. Owners ship the fakes for their interface
Consumers never write their own fake of someone else's module (two fakes drift apart). At the start of Block 1:
- A ships `tests/fakes/fake_repo.py` (in-memory `Repository`, seeded from a dict) and fake adapters.
- B ships `tests/fakes/fake_llm.py` (scripted responses, records calls).
- C ships `tests/fakes/fake_slack.py`.

Fakes live in `tests/fakes/<owner file>`, edited only by the owner.

### R6. Shared docs have one editor
`CLAUDE.md`, `TEAM_PLAN.md`, the lane files and this file are edited only at checkpoints, by whoever is merging. Tick your own checklist boxes in your own lane file only.

### R7. Small commits, rebase often
Commit after each checklist item, and run `git fetch && git rebase origin/main` whenever someone announces a contract PR. Small, frequent rebases turn conflicts into one-line fixes instead of an end-of-block merge fight.

### R8. Tell your AI assistant the boundaries
Add to every prompt: "Edit only files my lane owns (see `docs/plan/TEAM_PLAN.md` §1). If a change needs another lane's file, `types.py`, `pyproject.toml` or `config.yaml` outside my section, stop and tell me instead." AI assistants happily "fix" neighbouring files, and that is the most likely source of surprise conflicts.

---

## CP0 agenda (in order)

1. Read this file together; change anything you disagree with.
2. A: skeleton, `pyproject.toml` (R3), empty `__init__.py` files, test folders (D6).
3. A: `types.py` = LLD §4 + D1 + D2.
4. Each owner writes the stubs for their own modules, with the D5 constructor for `WorkerLoop`.
5. B: `worked_example.json`, `ladder.json`.
6. Commit everything to `main`; each person creates their branch from it.

---

## Part 3 — Clarifications accepted with the merge

Checked against the LLD on 2026-10-10. These are the places the merged code deliberately differs from a verbatim reading, and why that reading cannot be implemented as written.

### D7. Breakeven WTI is optional

LLD §4 types `breakeven_wti_hold` and `breakeven_wti_hike` as `float`. §6 says that when fuel pass-through is 0, the breakeven is infinite (coverage stays above the threshold) or the breach is certain. JSON has no infinity, and a stored run must load back through `RunResult`. Both fields are `Optional[float]`. A non-finite value is stored as `None`. The engine still reports the outcome through `p_breach` (0 when there is no finite breakeven and coverage holds, 1 when a breach is certain). That behavior is B4; the type change is the contract.

### D8. `fuel_curve` receives `as_of`

LLD §3 writes `fuel_curve(ladder)`. The group rule in §6 needs `as_of` plus `fuel.horizon_days`, and DD-5 forbids reading the clock inside `factors`. The signature is `fuel_curve(ladder, as_of, horizon_days=100) -> tuple[FuelCurve | None, list[str]]`. `rate_states` and `fuel_curve` return `(value, flags)` for the same reason: §3 says they return flags and raise nothing.

### D9. `Settings` and `Adapters` exist at CP0

`load_settings() -> Settings` and the D5 `WorkerLoop` constructor named both types, and neither class existed, so the annotations did not resolve. `Settings` lives in `config.py` and matches `config.yaml` (unknown keys inside a section are ignored; an unknown top-level section is an error). `load_settings()` is still A1. `Adapters` lives in `worker.py`: one attribute per source adapter.

### What is still not CP0

- No `.env` on this machine, so B0.3 (Snowflake and Gemini keys) and C0.3 (Slack webhook) are open. The exit test's smoke curls cannot be re-run until those are filled in locally. `.env` stays untracked.
- `load_settings()`, the store, the adapters, the engine, the API and the UI are stubs. That is CP1 onward. The public names match LLD §3.
