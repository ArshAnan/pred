# Low-Level Design: Market-Implied Scenario Analyst (Hackathon MVP)

Oct 10, 2026 · @Arsh Anand

## Scope and design decisions

This document specifies how to build the three-airline MVP from the high-level design: one Python API service that owns all data and logic, one Streamlit client, and one SQLite file. The companies are Delta (DAL), United (UAL) and American (AAL). Sources, scope and cuts are unchanged from the high-level design except where the last list in this section says otherwise.

| ID | Decision | Reason | Consequence |
| --- | --- | --- | --- |
| DD-1 | Streamlit calls only the API, and only the API process writes SQLite | SQLite allows one writer at a time, and a thin client keeps data logic out of UI code | Refines the high-level design, where Streamlit read the file directly; adds one HTTP boundary |
| DD-2 | One API process with a single uvicorn worker hosts the background worker loop | Avoids a third process and cross-process locking | Never start more than one worker; a restart restarts the loop |
| DD-3 | Timestamps are UTC ISO-8601, probabilities are floats in \[0, 1\], money is USD millions in fields ending `_usd_m` | One unit convention across adapters, engine and UI | Adapters convert at the boundary, for example Kalshi's dollar strings |
| DD-4 | Every read takes an `as_of` time | Replay mode and protection against look-ahead | No "latest" query exists; the default `as_of` is the current time |
| DD-5 | The engine modules (`trust`, `factors`, `scenario`) are pure functions over typed inputs | Unit tests need no database or network | These modules import only `types` |
| DD-6 | LLMs write labels and prose, never numbers used in math; every LLM output is schema-checked and cached; a template brief always exists | Correctness, and a demo that survives an outage | A run has two briefs, template and LLM |
| DD-7 | Vendor quirks stay inside adapters, which return domain types only | Kalshi and Polymarket differ in units, identifiers and protocol versions | The engine never sees vendor fields |
| DD-8 | LLM provider chain: Snowflake Cortex (MLH trial), then the Gemini free API tier, then local Ollama, plus a cache-only mode | Zero cost with a fallback | Evidence and limits are in the LLM contract in section 7 |

Changes from the high-level design:

- A FastAPI service and an HTTP contract are added (sections 2 and 7).
- The store gains an `llm_cache` table, making seven tables, and `snapshots` gains a `src` column.
- Fuel states come analytically from the WTI probability ladder; the three-by-two scenario grid stays as a presentation table.
- Markets carry a `kind` (close-above, touch, per-meeting) so contracts with different meanings are never mixed.

## Architecture and runtime

Two processes run on one machine, both bound to localhost: the API server on port 8000 and the Streamlit app on port 8501. Ollama is an optional third process.

&#91;embedded content: runtime architecture · 2 processes, 1 store, 3 groups of outside services\]

The API server owns the database, the adapters, the engine, the LLM client and the worker loop; Streamlit only sends HTTP requests.

| Process | Command | Address | Owns |
| --- | --- | --- | --- |
| API server | `uvicorn airscen.api:app --host 127.0.0.1 --port 8000 --workers 1` | 127.0.0.1:8000 | SQLite file, adapters, engine, LLM client, worker loop |
| Streamlit app | `streamlit run app.py --server.address 127.0.0.1 --server.port 8501` | 127.0.0.1:8501 | UI state only; calls `/api/v1` through `ui/client.py` |
| Ollama (optional) | `ollama serve` | 127.0.0.1:11434 | a local model, used only as the last fallback |
| CLI | `python -m airscen.cli ingest`, `backfill`, `eval` | none | `ingest` and `backfill` call the API's `/admin` endpoints; `eval` opens the database read-only |

Runtime rules:

1. Start the API first, then Streamlit. Streamlit calls `GET /health` on load and shows a banner if it fails.
2. Startup checks run before the API accepts traffic: settings validate, `SEC_USER_AGENT` is set, the database migrates, and at least one LLM provider is configured or `LLM_MODE` is `cache_only` or `off`. Any failure stops startup with a message naming the setting.
3. The worker loop starts in the app's lifespan handler and, on shutdown, stops after its current batch.
4. Bind to `127.0.0.1` only. There is no login in the MVP; if the API is ever exposed on a shared network, set `API_TOKEN` and the server then requires header `X-API-Token`.
5. Use exactly one uvicorn worker. Two workers would run two worker loops and two SQLite writers.

## Modules

One Python package, `airscen`, with one file per module. Signatures use domain types from section 4.

| Module | Responsibility | Public interface | Raises |
| --- | --- | --- | --- |
| `types.py` | Enums and Pydantic models shared by every module | see section 4 | none |
| `config.py` | Load `.env` and `config.yaml` into one validated `Settings` | `load_settings() -> Settings` | `ConfigError` |
| `store.py` | The only module that opens SQLite; enforces `as_of`; every write is a short transaction | `Repository(path)`, `migrate()`, `upsert_facts(rows)`, `latest_facts(company, as_of)`, `put_snapshots(rows)`, `latest_snapshot(venue, market_id, as_of)`, `snapshot_series(venue, market_id, start, end, as_of)`, `upsert_market(m)`, `markets(factor=None)`, `put_macro(rows)`, `macro_series(series_id, start, end, as_of)`, `save_run(run)`, `get_run(run_id)`, `update_run_brief(run_id, md, status)`, `save_alert(a)`, `alerts(company, since)`, `cache_get(key)`, `cache_put(entry)`, `data_version(as_of)` | `StoreError` |
| `adapters/http.py` | Shared client: timeouts, retry, per-host token bucket, circuit breaker | `SourceClient(name, base_url, rate_per_s, headers)`, `get_json(path, params)`, `post_json(path, body)` | `SourceError` |
| `adapters/kalshi.py` | Kalshi REST to domain types | `list_markets(series)`, `quote(ticker)`, `candles(ticker, start, end, period_min)`, `historical_cutoff()` | `SourceError` |
| `adapters/polymarket.py` | Gamma, CLOB and Data API to domain types | `search(query)`, `quotes(token_ids)`, `history(token_id, start, end, bucket_s)` | `SourceError` |
| `adapters/edgar.py` | Filing lists and documents from SEC | `resolve_cik(ticker)`, `recent_filings(cik, since)`, `fetch_document(ref)`, `xbrl_facts(cik, tags)` | `SourceError` |
| `adapters/fred.py` | FRED observations with vintages | `observations(series_id, start, end, as_of)` | `SourceError` |
| `adapters/alpaca.py` | Daily bars for sanity checks only | `daily_bars(symbols, start, end)` | `SourceError` |
| `adapters/gdelt.py` | Headlines published before a time | `headlines(query, before, hours, n)` | `SourceError`, ignored by callers |
| `llm.py` | Provider chain, JSON-schema output, cache, budget guard | `LLMClient.complete_json(task, system, user, schema, cache_key)`, `LLMClient.complete_text(task, system, user, cache_key)` | `LLMError` |
| `extract.py` | Press-release text to validated facts, with an overrides fallback | `extract_facts(company, text, ref)`, `validate(facts)` | `DataError` |
| `scout.py` | Candidate markets and their labels | `candidates(markets)`, `label(meta)` | `LLMError`, falls back to hand labels |
| `trust.py` | Quote cleaning and trust score | `clean(snapshot, now)`, `trust_score(market, snapshot, now, cfg)` | none |
| `factors.py` | Market odds to factor states | `rate_states(quotes)`, `fuel_curve(ladder)`, `fuel_states(curve)` | none; returns flags |
| `scenario.py` | Scenario math and summary metrics | `run_scenarios(inputs)` | `DataError` for missing required facts |
| `brief.py` | Template and LLM briefs, number check | `template_brief(result)`, `llm_brief(result, llm)`, `check_numbers(md, result)` | `LLMError` |
| `service.py` | Orchestration: assemble inputs, call the engine, persist | `RunService.create_run(req)`, `finish_brief(run_id)`, `get_run(run_id)` | `DataError`, `StoreError`, `RequestError` |
| `alerts.py` | Rule evaluation, dedup, Slack delivery | `AlertEngine.evaluate(company, run, previous)`, `deliver(alert)` | `SourceError`, caught and retried |
| `worker.py` | Background loops | `WorkerLoop.start()`, `stop()`, `tick_markets()`, `tick_filings()`, `tick_macro()` | none; logs and continues |
| `api.py` | FastAPI app, routers, error handlers | `app` | maps errors to HTTP |
| `ui/client.py` | Typed HTTP client for Streamlit | `ApiClient(base_url).get(path, params)`, `.post(path, body, key)` | `ApiError` |
| `app.py` | Streamlit pages: Run, Exposure map, Peers, Alerts, Replay | none | none |
| `eval/` | The five checks from the high-level design | `python -m airscen.eval` | none |
| `cli.py` | Admin commands | `ingest`, `backfill`, `eval` | none |

Dependency rules:

1. `types` imports nothing from the package.
2. Engine modules (`trust`, `factors`, `scenario`, and the template path of `brief`) import only `types`, the standard library and numpy.
3. Adapters import only `types` and `adapters.http`.
4. `service` imports the engine, `store`, `llm`, `extract` and `scout`; adapters arrive as constructor arguments so tests can fake them.
5. `api` imports only `service`, `store` for reads, and `types`.
6. `ui` imports nothing from the package except `types` for response models.
7. Only `store` opens the database.

A test parses the imports and fails the build if any rule is broken.

## Data classes and schema

Domain models live in `types.py` as frozen Pydantic v2 models. Adapters create them, the store persists them, the engine consumes them, and the API serialises them.

### Domain models

```python
# airscen/types.py -- every model is a frozen pydantic v2 BaseModel
from enum import Enum
from datetime import date, datetime
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field

class Venue(str, Enum):      KALSHI = "kalshi"; POLYMARKET = "polymarket"
class Factor(str, Enum):     FUEL = "fuel"; RATES = "rates"; INFLATION = "inflation"; DEMAND = "demand"; GEOPOLITICS = "geopolitics"
class MarketKind(str, Enum): CLOSE_ABOVE = "close_above"; TOUCH = "touch"; PER_MEETING = "per_meeting"; LEVEL_AFTER = "level_after"; OTHER = "other"
class Basis(str, Enum):      GAAP = "GAAP"; ADJUSTED = "ADJUSTED"
class Extraction(str, Enum): XBRL = "xbrl"; PARSER = "parser"; LLM = "llm"; OVERRIDE = "override"

class Metric(str, Enum):
    OPERATING_INCOME = "operating_income"          # usd_m, one quarter
    NET_INTEREST_EXPENSE = "net_interest_expense"  # usd_m, one quarter
    FUEL_GALLONS_M = "fuel_gallons_m"              # millions of gallons, one quarter
    FUEL_PRICE_PER_GAL = "fuel_price_per_gal"      # usd per gallon
    DEBT_TOTAL = "debt_total"                      # usd_m, includes finance leases
    DEBT_CURRENT = "debt_current"                  # usd_m
    VARIABLE_DEBT_SHARE = "variable_debt_share"    # fraction 0..1
    LIQUIDITY = "liquidity"                        # usd_m
    ADJUSTED_NET_DEBT = "adjusted_net_debt"        # usd_m
    REVENUE_GAAP = "revenue_gaap"; REVENUE_ADJUSTED = "revenue_adjusted"          # usd_m
    TOTAL_ASSETS = "total_assets"; TOTAL_LIABILITIES = "total_liabilities"; EQUITY = "equity"  # usd_m, identity checks

class Company(BaseModel): ticker: str; cik: int; name: str; peers: list[str]

class Fact(BaseModel):
    company: str; metric: Metric; value: float; unit: str; basis: Basis
    period_end: date; filed_at: datetime; source_url: str
    extraction: Extraction; validated: bool = False

class MarketMeta(BaseModel):
    venue: Venue; market_id: str; token_id: Optional[str] = None
    title: str; rules: str = ""; close_time: Optional[datetime] = None
    kind: MarketKind = MarketKind.OTHER
    outcome: Optional[Literal["hold", "hike", "cut"]] = None        # per-meeting markets only
    threshold: Optional[float] = None; threshold_unit: Optional[str] = None   # "usd_per_bbl", "bp"
    group_key: Optional[str] = None   # same resolve date or same meeting, e.g. "KXFEDDECISION-26OCT"

class MarketLabel(BaseModel):
    relevant: bool; factor: Optional[Factor] = None
    yes_moves_factor: Optional[Literal["up", "down"]] = None
    mechanism: str = Field("", max_length=200); citation: Optional[str] = None
    label_source: Literal["hand", "llm"]

class Snapshot(BaseModel):
    venue: Venue; market_id: str; ts: datetime
    bid: Optional[float] = None; ask: Optional[float] = None         # probability units, 0..1
    bid_size: Optional[float] = None; ask_size: Optional[float] = None
    last: Optional[float] = None; volume_24h: Optional[float] = None
    open_interest: Optional[float] = None; liquidity: Optional[float] = None
    src: Literal["live", "backfill_kalshi", "backfill_polymarket"] = "live"

class MacroObs(BaseModel): series_id: str; obs_date: date; value: float; realtime_start: date

# ---- engine and API models ----
class TrustResult(BaseModel):
    venue: Venue; market_id: str; score: float; mid: Optional[float]
    components: dict[str, float]; excluded_reason: Optional[str] = None

class RateStates(BaseModel):
    p_hike: Optional[float]; delta_hike_bp: int = 25; source_group: Optional[str]; venue_gap: Optional[float]

class FuelCurve(BaseModel):
    points: list[tuple[float, float]]   # (wti usd/bbl, P(WTI >= w)); tails included; non-increasing
    resolve_group: str; n_markets: int

class ScenarioInputs(BaseModel):
    company: str; as_of: datetime
    note_size_usd_m: float; tenor_years: int; coupon: float          # coupon as a decimal, e.g. 0.055
    coverage_threshold: float; fuel_passthrough: float = 1.0
    baseline: dict[Metric, Fact]; wti_ref: float                     # mean WTI over the baseline quarter
    rates: Optional[RateStates]; fuel: Optional[FuelCurve]

class ScenarioRow(BaseModel):
    fuel_label: Literal["P10", "P50", "P90"]; rate_label: Literal["hold", "hike"]
    wti: float; probability: Optional[float]
    operating_income_usd_m: float; net_interest_usd_m: float
    coverage: float; breach: bool

class ScenarioSummary(BaseModel):
    p_breach: Optional[float]
    breakeven_wti_hold: float; breakeven_wti_hike: float
    coverage_p50: float; coverage_p90: float
    liquidity_cover: Optional[float]; liquidity_ok: Optional[bool]

class PeerResult(BaseModel): ticker: str; period_end: date; summary: ScenarioSummary; flags: list[str]

class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str; as_of: Optional[datetime] = None
    note_size_usd_m: float; tenor_years: int; coupon_spread_bp: float
    coverage_threshold: float = 3.0; fuel_passthrough: float = 1.0; include_peers: bool = True

class Brief(BaseModel):
    template_md: str; llm_md: Optional[str]
    llm_status: Literal["pending", "ready", "failed", "rejected", "skipped"]

class RunResult(BaseModel):
    schema_version: int = 1; run_id: str; company: str; as_of: datetime; created_at: datetime
    inputs: RunRequest; rows: list[ScenarioRow]; summary: ScenarioSummary
    peers: list[PeerResult]; flags: list[str]; data_version: str; brief: Brief

class Alert(BaseModel):
    alert_id: str; ts: datetime; company: str; run_id: Optional[str]
    rule: Literal["BREACH_FLIP", "DRIVER_MOVE", "DATA_STALE"]
    dedup_key: str; severity: Literal["high", "medium", "low"]; payload: dict
    status: Literal["pending", "sent", "failed", "suppressed"]
```

### Invariants

| Model | Invariant | Enforced by |
| --- | --- | --- |
| `Snapshot` | 0 ≤ bid ≤ ask ≤ 1, sizes ≥ 0, `ts` in UTC | `trust.clean` drops violators and counts them |
| `Fact` | `filed_at` on or after `period_end`; `variable_debt_share` in \[0, 1\]; money in USD millions | model validators |
| `MarketLabel` | `factor` and `yes_moves_factor` are required when `relevant` is true | model validator |
| `RunRequest` | `note_size_usd_m` in (0, 50,000\]; `tenor_years` 1 to 40; `coupon_spread_bp` 0 to 1,000; `coverage_threshold` in (0, 50\]; `fuel_passthrough` 0 to 1; unknown fields rejected; `as_of` not later than now plus one minute | Pydantic; violations return `E_VALIDATION` |
| `FuelCurve` | at least 3 market points plus 2 tail points; survival never increases with price | `factors.fuel_curve` |
| `ScenarioRow` | probabilities of all six rows sum to 1 within 1e-9, or all are null | property test |

### Database

Seven tables hold everything. Only snapshots to markets and alerts to runs are joined by foreign key; a run reproduces its inputs through `data_version`.

&#91;embedded content: schema · 7 tables, 2 foreign keys\]

```sql
PRAGMA journal_mode = WAL;  PRAGMA busy_timeout = 5000;  PRAGMA foreign_keys = ON;

CREATE TABLE facts (
  id INTEGER PRIMARY KEY,
  company TEXT NOT NULL, metric TEXT NOT NULL,
  basis TEXT NOT NULL CHECK (basis IN ('GAAP','ADJUSTED')),
  value REAL NOT NULL, unit TEXT NOT NULL,
  period_end TEXT NOT NULL, filed_at TEXT NOT NULL, source_url TEXT NOT NULL,
  extraction TEXT NOT NULL CHECK (extraction IN ('xbrl','parser','llm','override')),
  validated INTEGER NOT NULL DEFAULT 0, loaded_at TEXT NOT NULL,
  UNIQUE (company, metric, basis, period_end, extraction)
);
CREATE INDEX ix_facts ON facts (company, metric, filed_at);

CREATE TABLE markets (
  venue TEXT NOT NULL, market_id TEXT NOT NULL, token_id TEXT,
  title TEXT NOT NULL, rules TEXT NOT NULL DEFAULT '', close_time TEXT,
  kind TEXT NOT NULL, outcome TEXT, threshold REAL, threshold_unit TEXT, group_key TEXT,
  relevant INTEGER, factor TEXT, yes_moves_factor TEXT, mechanism TEXT, citation TEXT,
  label_source TEXT, labeled_at TEXT, content_hash TEXT NOT NULL,
  PRIMARY KEY (venue, market_id)
);

CREATE TABLE snapshots (
  venue TEXT NOT NULL, market_id TEXT NOT NULL, ts TEXT NOT NULL,
  bid REAL, ask REAL, bid_size REAL, ask_size REAL, last REAL,
  volume_24h REAL, open_interest REAL, liquidity REAL,
  src TEXT NOT NULL DEFAULT 'live',
  PRIMARY KEY (venue, market_id, ts),
  FOREIGN KEY (venue, market_id) REFERENCES markets (venue, market_id)
);

CREATE TABLE macro (
  series_id TEXT NOT NULL, obs_date TEXT NOT NULL, value REAL NOT NULL, realtime_start TEXT NOT NULL,
  PRIMARY KEY (series_id, obs_date, realtime_start)
);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, company TEXT NOT NULL, as_of TEXT NOT NULL, created_at TEXT NOT NULL,
  inputs_json TEXT NOT NULL, result_json TEXT NOT NULL, data_version TEXT NOT NULL,
  brief_template_md TEXT NOT NULL, brief_llm_md TEXT,
  brief_llm_status TEXT NOT NULL CHECK (brief_llm_status IN ('pending','ready','failed','rejected','skipped'))
);

CREATE TABLE alerts (
  alert_id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs (run_id),
  ts TEXT NOT NULL, company TEXT NOT NULL, rule TEXT NOT NULL, dedup_key TEXT NOT NULL,
  severity TEXT NOT NULL, payload_json TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, sent_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending','sent','failed','suppressed'))
);
CREATE INDEX ix_alerts ON alerts (company, ts);

CREATE TABLE llm_cache (
  cache_key TEXT PRIMARY KEY, task TEXT NOT NULL, provider TEXT, model TEXT,
  response_json TEXT NOT NULL, tokens_in INTEGER, tokens_out INTEGER, created_at TEXT NOT NULL
);
```

Every read goes through `as_of` helpers with these visibility rules:

| Table | Visible when | Tie-break |
| --- | --- | --- |
| `facts` | `filed_at` is at or before `as_of` | per company, metric and basis, the newest `filed_at`; source priority xbrl, then override, then parser, then llm; only `validated = 1` rows count; if two sources differ by more than 0.5% for the same period the xbrl value wins and the run gets `FACT_CONFLICT` |
| `snapshots` | `ts` is at or before `as_of` | newest per market; stale when `as_of` minus `ts` exceeds `stale_after_s` |
| `macro` | `realtime_start` and `obs_date` are at or before `as_of` | per `obs_date`, the newest `realtime_start` |
| `markets` | always; labels have no history in the MVP | a `hand` label beats an `llm` label |
| `runs`, `alerts` | not filtered; they are history | none |

## Data flow

A run request returns scenario numbers and a template brief in one synchronous call. The LLM brief is produced in a background task and fetched by polling.

&#91;embedded content: run request lifecycle · 5 participants, 1 background task, 1 polling loop\]

The template brief is always stored first, and the LLM brief replaces it only after it passes `check_numbers`. If every provider fails, the run keeps the template and sets `llm_status` to `failed`.

### Background flows

| Flow | Trigger | Steps | Writes | On failure |
| --- | --- | --- | --- | --- |
| Quote recorder | every 60 s (`poll.market_s`) | for each labeled, relevant market: Kalshi `quote(ticker)`, Polymarket `quotes(token_ids)` in batches of at most 500, `trust.clean`, insert | `snapshots` | skip the failed source for this tick, add to its fail streak, continue |
| Market discovery and labeling | startup, every 6 hours, or `POST /admin/ingest` with `markets` | list Kalshi markets by series (Fed decision, CPI, WTI), search Polymarket, keyword prefilter, `scout.label` for new or changed `content_hash`; hand labels win | `markets` | an LLM failure leaves the market unlabeled and excluded |
| Filings watcher | every 30 minutes (`poll.filings_s`) | `recent_filings(cik, since)`; for each new 8-K or 10-Q download the document, `extract_facts`, `validate`, upsert; when a 10-Q arrives, cross-check against XBRL | `facts` | failed validation falls back to `overrides.yaml`; the raw file stays in `data/raw/` |
| Macro refresh | startup, then every 6 hours | FRED `DGS10` and `DCOILWTICO` with vintages | `macro` | keep earlier rows and mark the source degraded |
| Alert evaluation | after each recorder tick, for companies in `alerts.watch` | `RunService.create_run` with `as_of` now and the saved watch inputs, compare with the previous run, `AlertEngine.evaluate`, deliver | `runs`, `alerts` | rule errors are logged; the loop continues |
| Backfill for replay | `POST /admin/ingest` with `backfill` and a window | Kalshi candlesticks (60-minute) and Polymarket history (3-hour buckets) for the shortlist; write snapshots with a backfill `src` | `snapshots` | missing history sets `BACKFILL_GAP` on replayed runs |

Flow rules:

1. Data moves one way: source, adapter, typed domain object, Repository. Raw vendor JSON never leaves an adapter; raw bodies are saved under `data/raw/` only for filings and parse errors.
2. The engine receives its inputs only from `RunService`, which gathers them from the Repository with a single `as_of`.
3. The UI receives only API JSON and never sees a vendor field.
4. Writes are atomic: one transaction per recorder tick and one per filing.
5. The worker loop calls `RunService` and `AlertEngine` in process; it never calls the API over HTTP.

## Business logic

This section is the specification the engine is tested against. All thresholds, weights and references named here are defaults in `config.yaml`; none is fitted to data.

| Symbol | Meaning | Unit | Source |
| --- | --- | --- | --- |
| OI0 | operating income, latest quarter | USD millions | fact `operating_income`, GAAP |
| NI0 | net interest expense, latest quarter | USD millions | fact `net_interest_expense` |
| G | fuel burned, latest quarter | millions of gallons | fact `fuel_gallons_m` |
| D | total debt and finance leases | USD millions | fact `debt_total` |
| v | variable-rate share of debt | fraction | fact `variable_debt_share` |
| L, Mcur | liquidity; debt due within 12 months | USD millions | facts `liquidity`, `debt_current` |
| N, c | new note size; coupon | USD millions; decimal | request; FRED `DGS10` plus the spread |
| θ | coverage threshold | multiple | request |
| κ | fuel pass-through, 1 means no offset | 0 to 1 | request |
| W̄ | mean WTI over the baseline quarter | USD per barrel | FRED `DCOILWTICO` |

### Inputs and required data

- `as_of` defaults to now. Facts are the latest rows visible at `as_of` under the rules in section 4.
- OI0, NI0, G, D and v are required. If any is missing the API returns 422 `E_MISSING_FACT` naming them. L and Mcur are optional; without them the liquidity check is null.
- Coupon: c = (DGS10 + coupon\_spread\_bp / 100) / 100, where DGS10 is the latest observation at or before `as_of`, in percent.
- W̄ is the mean of `DCOILWTICO` observations dated inside the baseline quarter and visible at `as_of`, and needs at least 30 of them. Otherwise use `fuel.wti_ref_override` from config and set `REF_WTI_OVERRIDE`; with neither, return `E_MISSING_FACT`.
- Each company uses its own latest period. A peer whose `period_end` differs from the focal company's gets `PERIOD_MISMATCH`. If v's period is earlier than D's, set `VARIABLE_SHARE_STALE`.

### Quote cleaning and trust

```text
clean(snapshot):
  drop if bid or ask is missing, bid > ask, or any of bid, ask, last is outside [0, 1]
  mark stale if now - ts > stale_after_s                  (default 300 s)
mid    = (bid + ask) / 2            # the probability the engine uses
spread = ask - bid

f_spread = clip(1 - spread / spread_max, 0, 1)                                    spread_max = 0.10
f_depth  = clip(log10(1 + min(bid_size, ask_size)) / log10(1 + depth_ref), 0, 1)  depth_ref  = 1000 contracts
f_volume = clip(log10(1 + volume_24h) / log10(1 + volume_ref[venue]), 0, 1)       kalshi 10,000 contracts; polymarket 100,000 USD
f_time   = clip(days_to_close / min_days, 0, 1)                                   min_days   = 2
trust    = 0.35 f_spread + 0.25 f_depth + 0.25 f_volume + 0.15 f_time
```

A market feeds the engine when `trust` is at least `trust_floor` (0.40) and its snapshot is not stale. Backfilled snapshots carry no order book, so their trust is the fixed `trust_backfill_cap` (0.50) and the run gets `BACKFILL_INPUTS`.

### Rate states

Input: trusted markets with factor `rates`, kind `per_meeting` and the same `group_key` (the next Fed meeting), one per outcome.

1. Take the mid for hold, hike and cut, counting a missing cut as 0. Hold and hike must both exist; otherwise rates are fixed at hold and the run gets `RATES_NO_MARKET_INPUT`.
2. total = m\_hold + m\_hike + m\_cut, then π\_hike = m\_hike / total and π\_hold = (m\_hold + m\_cut) / total. Normalising matters because raw prices can sum above 1: on October 9 Kalshi showed hold at 84% and hike at 18%.
3. A cut is folded into "no hike" because the grid has two rate states. A hike is +25 bp and hold is 0; any hike size is treated as 25 bp in the MVP.
4. If both venues have trusted markets for the same meeting, use the venue with the higher trust score (Kalshi on a tie) and record `venue_gap`, the absolute difference in π\_hike. A gap above 0.10 sets `VENUE_DISAGREE`.

### Fuel curve and fuel states

Input: trusted markets with factor `fuel`, `yes_moves_factor` up, kind `close_above` and one shared `group_key` (a resolve date). Each gives a threshold K in USD per barrel and p = mid = P(WTI at or above K).

1. Choose the group: among groups with at least 3 trusted thresholds, the one with the latest resolve date not after `as_of` plus 100 days; ties go to the group with more markets. If none qualifies, fuel is null and the run gets `FUEL_NO_MARKET_INPUT`.
2. Sort by K and force survival to be non-increasing: p\_i = min(p\_i, p\_(i-1)).
3. Let s be the median spacing between thresholds. Add tail points (K\_first - s, 1.0) and (K\_last + s, 0.0). S(w) is the linear interpolation through all points, equal to 1 below the left tail and 0 above the right tail. The tails are an assumption and the brief says so.
4. Presentation states: w\_q = S^-1(1 - q) for q = 0.10, 0.50 and 0.90, labeled P10, P50 and P90 with weights 0.3, 0.4 and 0.3 (Swanson's three-point rule).
5. Touch markets are never inputs: a touch pays on any intraday hit while close-above pays only on the settlement. They are a cross-check: at the same threshold, p\_touch must be at least p\_close\_above minus 0.02, otherwise set `LADDER_INCONSISTENT`.
6. Two assumption flags are always set: `CRUDE_ONLY` (the jet-fuel crack spread is held constant) and `RESOLVE_DATE_PROXY` (WTI at the resolve date stands in for the quarter average).

### Scenario math

```text
for each fuel state f (WTI w_f) and rate state r (change d_r as a decimal):
  dP_f   = (w_f - W_bar) / 42                   # USD per gallon; 42 gallons per barrel; crude only
  OI_f   = OI0 - kappa * G * dP_f               # USD millions
  NI_r   = NI0 + v * D * d_r / 4 + N * c / 4    # variable-rate interest change plus the new note, per quarter
  cover  = OI_f / NI_r
  breach = cover < theta                        # always true when OI_f <= 0
  prob   = weight_f * pi_r                      # null if the fuel or rate input is missing
```

The new note adds its first-year run-rate interest. `tenor_years` is carried for the brief and for the spread the user picks; it does not change the quarterly math.

### Summary metrics

```text
W_star_r = W_bar + 42 * (OI0 - theta * NI_r) / (kappa * G)    # breakeven WTI for rate state r
p_breach = sum over r of pi_r * S(W_star_r)                   # S is the survival curve above
```

Coverage falls as WTI rises, so a breach means WTI above W\_star, and its probability is the survival value at W\_star.

- If κ is 0, W\_star is infinite when OI0 exceeds θ times NI\_r (probability 0), otherwise a breach is certain (probability 1).
- If the fuel curve is null, `p_breach` is null and only the breakevens are shown. If rates are null, use π\_hold = 1.
- `coverage_p50` and `coverage_p90` are the hold-state coverages at the P50 and P90 fuel states.
- `liquidity_cover` = L / Mcur and `liquidity_ok` means it is at least 1.0 (`liquidity_cover_min`); both are null if L or Mcur is missing.

### Peers

- Peers use the same request, the same fuel curve and the same rate states, but their own G, OI0, NI0, D, v, L, Mcur and their own W̄.
- Margins are compared on `revenue_adjusted` when the company and every compared peer have it; otherwise on GAAP with `MARGIN_BASIS_MISMATCH`. Delta's GAAP revenue includes third-party refinery sales ($2,601M of $20,186M in the September quarter) that its peers do not have.
- Only companies in `alerts.watch` are alerted on.

### Alert rules

| Rule | Fires when | Re-arms when | Cooldown | Severity |
| --- | --- | --- | --- | --- |
| `BREACH_FLIP` | `p_breach` rises to `breach_threshold` (0.20) or more from below | `p_breach` is below the threshold minus `hysteresis` (0.02) | 60 min | high |
| `DRIVER_MOVE` | a trusted market feeding the run moves at least `driver_move_pp` (10 points) within `window_min` (60 min) | the cooldown ends and the move is below the threshold | 60 min per market | medium |
| `DATA_STALE` | a required source has been degraded for more than 15 minutes | the source is healthy again | once per incident | low |

The payload holds the rule, company, `as_of`, before and after values, the driver markets, the `run_id`, and up to three GDELT headlines published before the move (best effort, ignored on failure). The dedup key is `company:rule:market_id`. Delivery is a Slack webhook POST with three attempts in total, waiting 1 s and then 4 s between them.

&#91;embedded content: alert lifecycle · 4 states, 1 retry loop\]

### Worked example

Delta as of October 9, 2026. Facts come from the [September-quarter 8-K](https://www.sec.gov/Archives/edgar/data/27904/000002790426000035/deltaairlinesannouncessept.htm); the ladder, quotes and W̄ are illustrative.

| Input | Value | Basis |
| --- | --- | --- |
| OI0, NI0 | 1,454 and 146 | 8-K, GAAP operating income and net interest expense |
| G | 1,146 | millions of gallons burned in the quarter |
| D | 12,851 | debt and finance leases at September 30 |
| v | 22% | variable share at June 30, 2026, so `VARIABLE_SHARE_STALE` is set |
| L, Mcur | 6,900 and 3,324 | liquidity and current maturities at September 30 |
| N, c | 1,000 and 5.5% | user inputs; coupon assumed |
| θ, κ | 6.0 and 1.0 | user inputs |
| W̄ | 85 | illustrative, USD per barrel |
| Rates | hold 0.82, hike 0.17, cut 0.01 | illustrative quotes |
| WTI ladder | at or above 80: 0.90; 90: 0.62; 100: 0.35; 110: 0.18; 120: 0.08 | illustrative; tails at 70 (1.00) and 130 (0.00) |

Calculation:

- NI\_hold = 146 + 1,000 × 0.055 / 4 = 159.75. NI\_hike = 159.75 + 0.22 × 12,851 × 0.0025 / 4 = 161.52. π\_hike = 0.17 and π\_hold = 0.83.
- W\_star\_hold = 85 + 42 × (1,454 - 6.0 × 159.75) / 1,146 = 103.16. W\_star\_hike = 102.77.
- S(103.16) = 0.296 and S(102.77) = 0.303, so p\_breach = 0.83 × 0.296 + 0.17 × 0.303 = 0.297.
- Fuel states: P10 = 80.00, P50 = 94.44, P90 = 118.00. Liquidity cover = 6,900 / 3,324 = 2.08.

| Fuel | WTI | Rate | Probability | OI (USD m) | NI (USD m) | Coverage | Breach |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P10 | 80.00 | hold | 0.249 | 1,590 | 159.75 | 9.96x | no |
| P50 | 94.44 | hold | 0.332 | 1,196 | 159.75 | 7.49x | no |
| P90 | 118.00 | hold | 0.249 | 554 | 159.75 | 3.47x | yes |
| P10 | 80.00 | hike | 0.051 | 1,590 | 161.52 | 9.85x | no |
| P50 | 94.44 | hike | 0.068 | 1,196 | 161.52 | 7.41x | no |
| P90 | 118.00 | hike | 0.051 | 554 | 161.52 | 3.43x | yes |

These numbers are the golden fixtures for `test_scenario_golden`. The app replaces the illustrative ladder and quotes with live data.

## Interface contracts

Three boundaries carry rules: Streamlit to the API, the adapters to outside services, and the LLM client to its providers. Each is fixed here so the two halves can be built separately.

### API conventions

- **Base URL:** `http://127.0.0.1:8000/api/v1`. JSON in UTF-8; requests with a body send `Content-Type: application/json`.
- **Formats:** timestamps are ISO-8601 UTC with a `Z` suffix (`2026-10-09T14:30:00Z`), dates are `YYYY-MM-DD`, probabilities are floats in \[0, 1\], money fields end `_usd_m`.
- **Request IDs:** every response carries `X-Request-ID`, echoed from the request or a new UUID4. Error bodies repeat it.
- **Versioning:** the path carries `v1` and result bodies carry `schema_version: 1`. A breaking change gets `/api/v2`.
- **Idempotency:** `POST /runs` accepts an `Idempotency-Key` header (UUID4). The same key within 10 minutes returns the original run with status 200 instead of 201. The server keeps keys in memory.
- **Strictness:** request bodies reject unknown fields and are limited to 64 KB. List endpoints take `limit` (default 50, maximum 200) and, where it applies, `since`.
- **Access:** no login. If `API_TOKEN` is set, every request needs header `X-API-Token` with that value. CORS is off because the browser never calls the API.

### Endpoints

| Method and path | Purpose | Request | Success | Errors |
| --- | --- | --- | --- | --- |
| `GET /health` | liveness and per-source status | none | 200 `HealthStatus` | 503 if the database is unreadable |
| `GET /companies` | configured companies and peers | none | 200 list of `Company` | none |
| `GET /companies/{ticker}/facts` | latest facts per metric | query `as_of` | 200 list of `Fact` | 404 |
| `GET /markets` | labeled markets with latest quote and trust | query `factor`, `min_trust`, `as_of` | 200 list of `MarketView` | 400 |
| `GET /markets/{venue}/{market_id}/history` | quote series | query `from`, `to`, `as_of` | 200 list of `Snapshot` | 400, 404 |
| `POST /runs` | create a run | `RunRequest` | 201 `RunResult` | 400, 404, 422, 503 |
| `GET /runs/{run_id}` | fetch a run, including the brief upgrade | none | 200 `RunResult` | 404 |
| `GET /runs` | recent runs | query `ticker`, `limit` | 200 list of `RunSummary` | 400 |
| `GET /alerts` | alert feed | query `company`, `since`, `limit` | 200 list of `Alert` | 400 |
| `POST /admin/ingest` | start an ingest or backfill job | `{sources, tickers, window}` | 202 `{job_id}` | 400, 409 |
| `GET /admin/jobs/{job_id}` | job status | none | 200 `{status, progress, errors}` | 404 |
| `POST /admin/alerts/test` | send a test Slack message | none | 200 `{delivered}` | 502 |

### Message shapes

Request to `POST /runs`:

```json
{
  "ticker": "DAL",
  "as_of": "2026-10-09T14:30:00Z",
  "note_size_usd_m": 1000,
  "tenor_years": 7,
  "coupon_spread_bp": 150,
  "coverage_threshold": 6.0,
  "fuel_passthrough": 1.0,
  "include_peers": true
}
```

Response (illustrative values, shortened):

```jsonc
{
  "schema_version": 1,
  "run_id": "run_7f3a9c",
  "company": "DAL",
  "as_of": "2026-10-09T14:30:00Z",
  "created_at": "2026-10-09T14:30:02Z",
  "inputs": { /* the request with defaults filled in */ },
  "rows": [
    {"fuel_label": "P10", "rate_label": "hold", "wti": 80.0, "probability": 0.249,
     "operating_income_usd_m": 1590.4, "net_interest_usd_m": 159.75, "coverage": 9.96, "breach": false}
    /* five more rows */
  ],
  "summary": {"p_breach": 0.297, "breakeven_wti_hold": 103.16, "breakeven_wti_hike": 102.77,
              "coverage_p50": 7.49, "coverage_p90": 3.47, "liquidity_cover": 2.08, "liquidity_ok": true},
  "peers": [{"ticker": "UAL", "period_end": "2026-06-30", "summary": { /* same shape */ }, "flags": ["PERIOD_MISMATCH"]}],
  "flags": ["VARIABLE_SHARE_STALE", "CRUDE_ONLY"],
  "data_version": "d41a7c0e",
  "brief": {"template_md": "...", "llm_md": null, "llm_status": "pending"}
}
```

Error envelope, used by every non-2xx response:

```json
{"error": {"code": "E_MISSING_FACT", "message": "Required facts are missing for DAL: variable_debt_share",
           "details": {"missing": ["variable_debt_share"]}, "request_id": "5b1f2c9e-0c1a-4a34-9a57-7d2b1c0f9e11"}}
```

| HTTP | Code | When | Client behavior |
| --- | --- | --- | --- |
| 400 | `E_VALIDATION` | body or query fails validation, including `as_of` more than a minute in the future | show `details.fields`; do not retry |
| 401 | `E_UNAUTHORIZED` | `API_TOKEN` is set and the header is missing or wrong | show the message |
| 404 | `E_NOT_FOUND` | unknown ticker, run, market or job | show the message |
| 409 | `E_CONFLICT` | an ingest job for the same sources is already running | show `details.job_id` |
| 422 | `E_MISSING_FACT` | a required fact or macro input is missing for this request | show `details.missing` and suggest running ingest |
| 500 | `E_INTERNAL` | unexpected error | show the request ID; do not retry |
| 502 | `E_SOURCE_UNAVAILABLE` | an admin action needed an outside service that failed | retry by hand |
| 503 | `E_STORE_UNAVAILABLE` | SQLite is locked or unreadable | GET retries twice automatically; POST does not |

### Streamlit client rules

| Rule | Value |
| --- | --- |
| Base URL | `API_BASE_URL`, default `http://127.0.0.1:8000/api/v1` |
| Timeouts | connect 2 s; read 10 s for GET; read 30 s for `POST /runs` |
| Retries | GET: up to 2 retries after 0.5 s then 1 s on connection errors, 502 and 503; POST: none automatic |
| Idempotency | a fresh `Idempotency-Key` for each click on Run, reused when the user retries after a failure |
| Polling | `GET /runs/{run_id}` every 2 s while `brief.llm_status` is `pending`, stopping after 60 s, then show the template brief with a note |
| Caching | `st.cache_data(ttl=600)` for `/companies`, `ttl=30` for `/markets`; never cache `POST` or `/runs` |
| Session state | only `run_id`, `as_of`, the form values and the last `/health` status |
| Time | the UI sends UTC; replay mode shows a banner with the `as_of` in force |
| Errors | `ApiError(code, message, request_id, status)` is shown as a banner; an unreachable API shows the base URL and a retry button |
| Imports | `ui/client.py` and `app.py` import only `types` and an HTTP library |
| Pages | Run (form and result), Exposure map (markets and trust), Peers, Alerts, Replay |

### Server rules

1. Requests and responses are Pydantic models; exception handlers convert every error into the envelope above.
2. `POST /runs` finishes the numeric work within its request (it is pure computation and takes well under a second) and schedules `finish_brief` with FastAPI background tasks. Its server-side time budget is 25 s.
3. `/health` reads in-memory status and never calls an outside service.
4. `/admin` endpoints run jobs in background tasks, one job per source group at a time.
5. Each request logs method, path, status, latency and request ID as one JSON line.

### Adapter contracts

All adapters use `adapters/http.py`: connect timeout 3 s, read timeout 10 s (20 s for EDGAR documents), and the retry policy in section 8.

| Source | Base URL | Auth | Client rate | Parsing notes |
| --- | --- | --- | --- | --- |
| Kalshi | `https://external-api.kalshi.com/trade-api/v2` | none for market data | 10 req/s (the documented Basic budget is about 20) | dollar fields such as `yes_bid_dollars` are strings, so parse with Decimal; sizes end in `_fp`; call `/historical/cutoff` before archive queries; a 429 has no `Retry-After` |
| Polymarket Gamma | `https://gamma-api.polymarket.com` | none | 10 req/s (documented: `/markets` 300 per 10 s) | `outcomePrices` and `clobTokenIds` are JSON-encoded strings; V2 markets use `positionIds`, so branch on `version` |
| Polymarket CLOB | `https://clob.polymarket.com` | none | 10 req/s (documented: `/book` 1,500 per 10 s) | batch `POST /books` up to 500 tokens; ignore a `last-trade-price` of 0.5 with an empty side |
| Polymarket Data | `https://data-api.polymarket.com` | none | 5 req/s | `/v2/prices-history` accepts explicit ranges up to 15 days; use `bucket_seconds` 10800 for history older than 90 days |
| SEC | `https://data.sec.gov` and `https://www.sec.gov/Archives` | `User-Agent` set from `SEC_USER_AGENT` | 5 req/s (SEC cap is 10) | cache documents by accession under `data/raw/edgar/`; XBRL facts carry `end`, `filed`, `form`, `fy` and `fp` |
| FRED | `https://api.stlouisfed.org/fred` | `api_key` query parameter | 2 req/s (limit not verified) | values are strings and `.` marks a missing value; use `realtime_start` and `realtime_end` for vintages |
| Alpaca | `https://data.alpaca.markets/v2` | `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY` headers | 3 req/s (200 per minute documented) | `feed=iex`; the newest 15 minutes are unavailable; the bars path is not yet verified |
| GDELT | `https://api.gdeltproject.org/api/v2/doc/doc` | none | 1 req/s (limit not verified) | window of at most 3 months; `mode=artlist` with `format=json` |
| Slack | the URL in `SLACK_WEBHOOK_URL` | secret in the URL | 1 message/s | 2xx means delivered; anything else is a failed attempt |

The documented limits come from the source audit. Where it says "not verified", the rate is a conservative default.

### LLM contract

```python
class LLMClient:
    def complete_json(self, task: str, system: str, user: str, schema: dict, cache_key: str) -> dict: ...
    def complete_text(self, task: str, system: str, user: str, cache_key: str) -> str: ...
# task is one of "extract_metrics", "label_market", "write_brief"; temperature is 0
```

| Task | Input | Output | Limits |
| --- | --- | --- | --- |
| `extract_metrics` | the 8-K sections: key statistics, balance sheet, debt table | JSON list of `{metric, value, unit, basis, period_end, evidence}` | 2,000 output tokens; every `value` must appear inside its `evidence` text |
| `label_market` | title, rules, close time | a `MarketLabel` | 400 output tokens |
| `write_brief` | the `RunResult` numbers as JSON | markdown of at most 400 words | 1,200 output tokens; `check_numbers` rejects any number absent from the input |

Provider chain, in default order (`LLM_CHAIN=snowflake,gemini,ollama`):

| Provider | Access | Cost and limits | Caveats |
| --- | --- | --- | --- |
| Snowflake Cortex through the MLH trial | OpenAI-compatible endpoint `https://<account>.snowflakecomputing.com/api/v2/cortex/v1/chat/completions` with a programmatic access token as the bearer token; start with `claude-haiku-4-5` (generally available), then `claude-haiku-5-5` (public preview) or `llama3.3-70b` | billed in AI Credits at $2.00 each with global routing; per million tokens `claude-haiku-4-5` is 0.50 input and 2.50 output credits, `claude-haiku-5-5` 0.05 and 0.25, `llama3.3-70b` 0.36 each; our estimated workload of about 0.8M input and 0.17M output tokens is about 0.8 credits (about $1.65) on `claude-haiku-4-5`; its rate limit is 5M tokens and 10,000 requests per minute ([REST API docs](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-rest-api), [consumption table](https://www.snowflake.com/legal-files/CreditConsumptionTable.pdf)) | the [MLH page](https://www.mlh.com/partners/snowflake) offers a 120-day trial but does not state the credit amount; an older Snowflake page said trial accounts get about one credit per day of Cortex LLM use and the current pages do not repeat it, so check your own balance first; some models need cross-region inference enabled; HTTP 402 means the budget is exceeded; structured output uses `response_format` with a JSON schema |
| Gemini API free tier | an API key from Google AI Studio; Google also documents an OpenAI-compatible endpoint; use a current Flash model | free; a [Google Cloud page](https://cloud.google.com/use-cases/free-ai-tools) cites up to 250 requests per day and 10 per minute on an unpaid key for a Flash model | the free student Google AI Pro plan does not apply: Google's [AI plans page](https://ai.google.dev/gemini-api/docs/google-ai-plans) says plan benefits cover only the AI Studio web interface and API keys are billed separately; limits are per model and change, so check AI Studio |
| Ollama (local) | `http://127.0.0.1:11434` with an instruct model of about 8B parameters | free; speed depends on the laptop | JSON-schema support varies by model, so validate and retry; last resort |
| Google Cloud $300 trial | not in the chain | none | Google's [free-trial documentation](https://docs.cloud.google.com/free/docs/free-cloud-features.md.txt) says the credit cannot pay for the Gemini API in AI Studio or for partner models offered as managed APIs, and it expires after 90 days; it also adds Google Cloud authentication setup |

Behavior rules:

1. Compute `cache_key = sha256(task, prompt_version, input)` and look it up in `llm_cache` first; a hit returns with no call.
2. On a miss in `live` mode, try providers in `LLM_CHAIN` order. `LLM_MODE=cache_only` raises `E_LLM_UNAVAILABLE` on a miss and `off` never calls a model.
3. Move to the next provider on connection errors, HTTP 429, 402 and 5xx. On invalid JSON or a schema failure, retry once on the same provider with a repair instruction, then move on.
4. Budget guard: at most `max_calls_per_hour` (120) calls and `max_input_tokens` (20,000) per call; exceeding either raises `LLMError` with `E_LLM_BUDGET`.
5. Log provider, model, tokens in and out, latency and cache hit for every call, and store usage with the cache row.
6. Run one warm-up pass, then switch the demo to `cache_only`, so a network failure cannot break it.

## Error handling

Errors are typed, retried only where that is safe, and degrade to a labeled partial answer instead of a crash.

### Exception hierarchy

```python
class AirScenError(Exception):   code = "E_INTERNAL"
class ConfigError(AirScenError): code = "E_CONFIG"                 # startup only
class RequestError(AirScenError):                                  # E_VALIDATION (400), E_UNAUTHORIZED (401), E_NOT_FOUND (404), E_CONFLICT (409)
class DataError(AirScenError):                                     # E_MISSING_FACT (422), E_VALIDATION_FAILED (extraction)
class SourceError(AirScenError):                                   # E_SOURCE_UNAVAILABLE, E_SOURCE_RATE_LIMITED, E_SOURCE_BAD_RESPONSE, E_SOURCE_AUTH
class LLMError(AirScenError):                                      # E_LLM_UNAVAILABLE, E_LLM_BAD_OUTPUT, E_LLM_BUDGET
class StoreError(AirScenError):                                    # E_STORE_UNAVAILABLE (503)
```

Every exception carries `code`, a human `message` and a `details` dict. The API converts them into the error envelope in section 7; the worker logs them and continues.

### Retry, timeout and rate policy

| Policy | Value |
| --- | --- |
| Retry on | connection errors, read timeouts, HTTP 429 and 5xx; never on other 4xx |
| Attempts | 3 in total for reads, waiting 0.5 s then 1 s with 20% jitter, capped at 8 s; a `Retry-After` header wins when present |
| Writes to outside services | no retry, except Slack: 3 attempts in total, waiting 1 s then 4 s |
| Timeouts | connect 3 s, read 10 s (20 s for EDGAR documents), 30 s for the whole call |
| Circuit breaker | 5 consecutive failures open the circuit for 60 s, during which calls fail fast with `E_SOURCE_UNAVAILABLE`; one probe call then decides |
| Rate limiting | a token bucket per host at the client rates in section 7; a 429 halves that host's rate for 60 s |

### Failure behavior

| Failure | Detected by | Handling | What the user sees |
| --- | --- | --- | --- |
| A quote fetch fails | `SourceError` after retries | skip the source this tick and add to its fail streak; runs use the last snapshot if it is not stale, otherwise exclude the market | `STALE_MARKETS`; `/health` degraded after 15 minutes |
| A vendor payload is malformed | Pydantic error inside the adapter | raise `E_SOURCE_BAD_RESPONSE`, save the raw body to `data/raw/errors/`, drop the item and keep the batch | the item is missing; a counter increments |
| A quote is crossed or out of range | `trust.clean` | drop and count | nothing |
| EDGAR is unreachable | `SourceError` | no new filings; runs use stored facts | `/health` shows EDGAR degraded |
| Extraction fails validation | `extract.validate` | reject the whole batch and use `overrides.yaml`, marking those facts `override` | the brief notes the figure came from an override; with no override, runs that need it return 422 |
| The fuel ladder has fewer than 3 trusted thresholds | `factors.fuel_curve` returns nothing | not an error; `p_breach` is null | `FUEL_NO_MARKET_INPUT`; breakevens still shown |
| Hold or hike markets are missing | `factors.rate_states` returns nothing | rates are fixed at hold | `RATES_NO_MARKET_INPUT` |
| FRED is unreachable or a series is stale | `SourceError` or too few observations | use stored observations visible at `as_of`; W̄ falls back to config | `REF_WTI_OVERRIDE`, or 422 when no fallback exists |
| All LLM providers fail | `LLMError` from the chain | keep the template brief; scout leaves the market unlabeled (hand labels still work); extract uses overrides | `llm_status` is `failed` |
| The LLM returns invalid JSON | schema validation | one repair retry, then the next provider | nothing |
| An LLM brief contains a number not in the input | `check_numbers` | discard it and set `llm_status` to `rejected` | the template brief |
| Slack delivery fails | non-2xx or timeout | 3 attempts, then status `failed`; the alert stays in the feed | the feed marks it "not delivered" |
| SQLite is busy | `sqlite3.OperationalError` | wait up to 5 s (`busy_timeout`), retry the write once, then raise `StoreError` | 503 on requests; the worker logs and skips the tick |
| Streamlit cannot reach the API | connection error | GET retries, then a banner | the base URL and a retry button |
| Configuration is wrong at startup | `ConfigError` | refuse to start | a message naming the setting |
| A worker tick raises | exception inside a tick | catch per tick, log, continue; three consecutive failed ticks mark the worker degraded | `/health` worker degraded |

### Extraction validation

A batch of extracted facts passes only if every rule holds; one failure rejects the batch.

1. Current plus noncurrent debt equals total debt within 0.5%. Delta's September quarter passes: 3,324 + 9,527 = 12,851.
2. When present, assets equal liabilities plus equity within 0.5%. Delta's passes: 33,575 + 29,960 + 22,513 = 86,048.
3. Every extracted number appears verbatim in the source text it cites as evidence.
4. Ranges hold: gallons above 0, net interest above 0, variable share between 0 and 1.
5. Where `overrides.yaml` has the metric, the extracted value matches it within 0.5%.

### Run flags

Flags are machine-readable strings on every run so the UI and the brief can state each limitation.

| Flag | Set when | Effect |
| --- | --- | --- |
| `STALE_MARKETS` | a market feeding the run has a stale snapshot | that market is excluded |
| `FUEL_NO_MARKET_INPUT` | no qualifying WTI ladder | `p_breach` is null |
| `RATES_NO_MARKET_INPUT` | hold or hike market missing | rates fixed at hold |
| `LADDER_INCONSISTENT` | a touch market undercuts its close-above twin | shown, numbers unchanged |
| `VENUE_DISAGREE` | `venue_gap` above 0.10 | shown |
| `PERIOD_MISMATCH` | a peer's period differs from the focal company's | peer shown with its period |
| `VARIABLE_SHARE_STALE` | v is from an earlier period than D | shown |
| `FACT_CONFLICT` | two sources differ by more than 0.5% | the xbrl value is used |
| `REF_WTI_OVERRIDE` | W̄ comes from config | shown |
| `BACKFILL_INPUTS`, `BACKFILL_GAP` | replay uses backfilled quotes, or history is missing | trust capped at 0.50; markets without history excluded |
| `MARGIN_BASIS_MISMATCH` | margins compared across GAAP and adjusted | shown |
| `CRUDE_ONLY`, `RESOLVE_DATE_PROXY` | always | listed as assumptions in the brief |

### Observability

- **Logs:** JSON lines to stdout and `logs/app.log` with `ts`, `level`, `component`, `event`, `request_id`, `run_id`, `source`, `latency_ms` and `error_code`.
- **`/health` body:** overall `status` (ok, degraded or down), `db`, per source `{status, last_ok_at, fail_streak}`, `last_snapshot_age_s`, `llm` `{mode, last_provider, calls_last_hour, cache_hit_rate}` and `worker` `{last_tick_at}`. Degraded means a required source has failed for more than 15 minutes; down means the database is unreadable.
- **Counters** kept in memory: `snapshots_dropped`, `llm_cache_hits`, `alerts_sent`.

## Configuration, tests and runbook

Secrets live in `.env`; everything tunable lives in `config.yaml`. Both are read once at startup by `load_settings()`.

### Environment keys

| Key | Used for | Needed |
| --- | --- | --- |
| `SEC_USER_AGENT` | EDGAR requests, formatted as a name and email | yes |
| `FRED_API_KEY` | `DGS10` and WTI series | yes |
| `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` | daily bars for the evaluation sanity check | eval only |
| `SLACK_WEBHOOK_URL` | alert delivery; without it alerts are saved with status `failed` and shown in the feed | for alerts |
| `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_PAT` | Snowflake Cortex | if in `LLM_CHAIN` |
| `GEMINI_API_KEY` | Gemini free tier | if in `LLM_CHAIN` |
| `OLLAMA_HOST` | local model, default `http://127.0.0.1:11434` | if in `LLM_CHAIN` |
| `LLM_MODE` | `live`, `cache_only` or `off`; default `live` | no |
| `LLM_CHAIN` | provider order; default `snowflake,gemini,ollama` | no |
| `API_HOST`, `API_PORT` | server bind address; defaults `127.0.0.1` and `8000` | no |
| `API_BASE_URL` | Streamlit's target; default `http://127.0.0.1:8000/api/v1` | no |
| `API_TOKEN` | optional shared secret for `X-API-Token` | no |
| `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` | the authenticated rate tier or a later WebSocket; market data works without them | no |

### Defaults in `config.yaml`

```yaml
companies:
  - {ticker: DAL, cik: 27904, name: Delta Air Lines, peers: [UAL, AAL]}
  - {ticker: UAL, cik: null, name: United Airlines Holdings, peers: [DAL, AAL]}   # resolved from company_tickers_exchange.json at startup
  - {ticker: AAL, cik: null, name: American Airlines Group, peers: [DAL, UAL]}
poll: {market_s: 60, filings_s: 1800, macro_s: 21600, stale_after_s: 300}
trust:
  weights: {spread: 0.35, depth: 0.25, volume: 0.25, time: 0.15}
  floor: 0.40
  backfill_cap: 0.50
  spread_max: 0.10
  depth_ref: 1000
  volume_ref: {kalshi: 10000, polymarket: 100000}
  min_days: 2
fuel: {horizon_days: 100, wti_ref_override: null, weights: [0.3, 0.4, 0.3]}
scenario: {liquidity_cover_min: 1.0, rate_hike_bp: 25}
alerts:
  watch: [DAL]
  watch_inputs: {note_size_usd_m: 1000, tenor_years: 7, coupon_spread_bp: 150, coverage_threshold: 3.0, fuel_passthrough: 1.0}
  breach_threshold: 0.20
  hysteresis: 0.02
  driver_move_pp: 10
  window_min: 60
  cooldown_min: 60
llm:
  max_calls_per_hour: 120
  max_input_tokens: 20000
  models: {snowflake: claude-haiku-4-5, gemini: "<current Flash model id>", ollama: "<installed instruct model>"}
```

The `coverage_threshold` default of 3.0 is a placeholder: no covenant has been verified, so the threshold is a user input.

### Test map

| Module | Tests | Fixtures |
| --- | --- | --- |
| `scenario` | golden test from the worked example (breakevens 103.16 and 102.77, `p_breach` 0.297, six rows); coverage never rises with WTI or note size (property test); probabilities sum to 1; κ = 0 edge; OI ≤ 0 is a breach | `worked_example.json` |
| `factors` | monotone fix; tail points; P10, P50, P90 equal 80, 94.44 and 118; rate normalisation gives 0.17 and 0.83; touch cross-check; fewer than 3 thresholds returns nothing | `ladder.json` |
| `trust` | each component; crossed quote dropped; stale quote excluded; backfill cap | inline |
| `store` | `as_of` visibility per table: on October 8 the October 9 8-K facts are invisible and the June-quarter facts show instead | temp SQLite |
| `extract` | the five validation rules on Delta's September-quarter sections; an invented number is rejected; overrides fallback | saved 8-K sections |
| `llm` | cache hit makes no call; provider fallback order; repair retry; budget guard; `cache_only` miss | fake providers |
| `brief` | `check_numbers` rejects an invented number; the template contains every summary number | inline |
| adapters | parsing of recorded Kalshi, Polymarket, SEC and FRED responses; retry and circuit breaker against a fake server | recorded JSON |
| `api` | every endpoint's status codes, the envelope shape, idempotency key, a future `as_of` rejected | FastAPI test client |
| `alerts` | state transitions from the diagram; dedup; hysteresis; Slack failure after 3 attempts | fake Slack |
| `ui/client` | timeouts, GET retries, error banner data | fake API server |
| architecture | the import rules from section 3 | none |
| replay | a run as of July 10 completes on backfilled fixtures and carries `BACKFILL_INPUTS` | recorded history |

### Runbook

| Command | Does |
| --- | --- |
| `make setup` | create the virtual environment, install requirements, copy `.env.example` to `.env` |
| `make api` | start the API server on 127.0.0.1:8000 |
| `make ui` | start Streamlit on 127.0.0.1:8501 |
| `make ingest` | call `POST /admin/ingest` for markets, filings and macro |
| `make backfill WINDOW=2026-07-01..2026-07-12` | backfill quotes for a replay window |
| `make warm` | run the replay and demo queries once so `llm_cache` is full, then set `LLM_MODE=cache_only` |
| `make eval` | run the five checks and print the report |
| `make test` | run pytest |

Run the day-zero smoke tests from the data source audit first; they confirm keys, CIKs and Polymarket access from the demo machine.

### Open items

- Confirm the Kalshi series for the WTI ladder and the Fed decision through `/series` and `/markets`. The ladder needs at least 3 thresholds on one resolve date.
- Check the Snowflake trial's credit balance and any daily cap, and which Cortex models your region offers.
- Confirm the United and American CIKs and their press-release layouts, then fill `overrides.yaml` for them by hand.
- The FRED and GDELT rate limits and Alpaca's bars path are unverified.
- Check Polymarket API access from the demo machine.
- Trust weights, thresholds and the 0.3, 0.4, 0.3 weights are defaults; tune them on the July 10 replay.
