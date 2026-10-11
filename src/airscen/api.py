"""FastAPI app: routes, request framing and the error envelope (LLD section 7, CP0_DECISIONS D3).

Run with exactly one worker (DD-2): `uvicorn airscen.api:app --workers 1`.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, FastAPI, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from airscen import config, service, store
from airscen.types import (
    AirScenError, Company, Fact, Factor, HealthStatus, LLMStatus, MarketView, RequestError, RunRequest, RunResult,
    RunSummary, Snapshot, StoreError, Venue, WorkerStatus,
)

PREFIX = "/api/v1"
DB_PATH = Path("data") / "app.db"
MAX_BODY_BYTES = 64 * 1024
IDEMPOTENCY_TTL_S = 600
HISTORY_DEFAULT_DAYS = 1

# D3: the only codes the API sends with their own status; anything else is 500 E_INTERNAL.
HTTP_STATUS = {
    "E_VALIDATION": 400, "E_UNAUTHORIZED": 401, "E_NOT_FOUND": 404, "E_CONFLICT": 409,
    "E_MISSING_FACT": 422, "E_SOURCE_UNAVAILABLE": 502, "E_STORE_UNAVAILABLE": 503,
}
_CODE_FOR_STATUS = {status: code for code, status in HTTP_STATUS.items()}

log = logging.getLogger("airscen.api")


def _default_repo(settings: Any) -> Any:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return store.Repository(DB_PATH)


def _no_job_runners(settings: Any, repo: Any) -> dict[str, service.JobRunner]:
    return {}  # C8 wires lane A's WorkerLoop ticks and backfill here


def create_app(
    settings_loader: Callable[[], Any] = config.load_settings,
    repo_factory: Callable[[Any], Any] = _default_repo,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    job_runners: Callable[[Any, Any], dict[str, service.JobRunner]] = _no_job_runners,
) -> FastAPI:
    """Build the app. Tests pass fakes and clocks; the real app uses lane A's loader and Repository."""
    now = clock or (lambda: datetime.now(timezone.utc))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings_loader()  # ConfigError propagates: refuse to start
        app.state.repo = app.state.service = None
        try:
            repo = repo_factory(app.state.settings)
            repo.migrate()
            app.state.repo = repo
            app.state.service = service.RunService(repo=repo, settings=app.state.settings, clock=now)
        except StoreError as e:
            _log("error", "db_unavailable", error_code=e.code, message=e.message)
        app.state.jobs = service.IngestJobs(job_runners(app.state.settings, app.state.repo), clock=now)
        yield

    app = FastAPI(title="AirScen API", version="1", lifespan=lifespan)
    app.state.settings = app.state.repo = app.state.service = None
    app.state.now = now
    app.state.idempotency = _IdempotencyKeys(IDEMPOTENCY_TTL_S, monotonic)
    app.include_router(_router())
    app.middleware("http")(_frame)
    app.add_exception_handler(AirScenError, _airscen_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    return app


# ---- routes ----

def _router() -> APIRouter:
    router = APIRouter(prefix=PREFIX)

    @router.get("/health", response_model=HealthStatus)
    def health(request: Request) -> HealthStatus:
        # In-memory only; never calls an outside service. Worker and source status arrive in C8.
        if request.app.state.repo is None:
            raise StoreError("The database is unavailable")
        settings = request.app.state.settings
        return HealthStatus(
            status="ok", db="ok", sources={},
            llm=LLMStatus(mode=settings.llm_mode), worker=WorkerStatus(),
        )

    @router.get("/companies", response_model=list[Company])
    def companies(request: Request) -> list[Company]:
        return [
            Company(ticker=c.ticker, cik=c.cik, name=c.name, peers=list(c.peers))
            for c in request.app.state.settings.companies
        ]

    @router.get("/companies/{ticker}/facts", response_model=list[Fact])
    def facts(request: Request, ticker: str, as_of: Optional[datetime] = None) -> list[Fact]:
        return _service(request).facts(ticker, _as_of(request, as_of))

    @router.post("/runs", status_code=201, response_model=RunResult)
    def create_run(
        request: Request, response: Response, req: RunRequest,
        idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    ) -> RunResult:
        svc = _service(request)
        keys: _IdempotencyKeys = request.app.state.idempotency
        if not idempotency_key:
            return svc.create_run(req)
        with keys.lock:  # one worker (DD-2): a lock is enough to stop a double click creating two runs
            if (run_id := keys.get(idempotency_key)) is not None:
                response.status_code = 200
                return svc.get_run(run_id)
            run = svc.create_run(req)
            keys.put(idempotency_key, run.run_id)
            return run

    @router.get("/runs/{run_id}", response_model=RunResult)
    def get_run(request: Request, run_id: str) -> RunResult:
        return _service(request).get_run(run_id)

    @router.get("/runs", response_model=list[RunSummary])
    def list_runs(request: Request, ticker: Optional[str] = None, limit: int = Query(50, ge=1, le=200)) -> list[RunSummary]:
        return _service(request).list_runs(ticker=ticker, limit=limit)

    @router.get("/markets", response_model=list[MarketView])
    def markets(
        request: Request, factor: Optional[Factor] = None,
        min_trust: Optional[float] = Query(None, ge=0, le=1), as_of: Optional[datetime] = None,
    ) -> list[MarketView]:
        return _service(request).market_views(_as_of(request, as_of), factor=factor, min_trust=min_trust)

    @router.get("/markets/{venue}/{market_id}/history", response_model=list[Snapshot])
    def history(
        request: Request, venue: Venue, market_id: str,
        start: Optional[datetime] = Query(None, alias="from"), end: Optional[datetime] = Query(None, alias="to"),
        as_of: Optional[datetime] = None,
    ) -> list[Snapshot]:
        as_of = _as_of(request, as_of)
        end = _aware(end, "to") if end else as_of
        start = _aware(start, "from") if start else end - timedelta(days=HISTORY_DEFAULT_DAYS)
        if start > end:
            raise RequestError("from must not be after to", details={"from": start.isoformat(), "to": end.isoformat()})
        return _service(request).history(venue, market_id, start, end, as_of)

    @router.post("/admin/ingest", status_code=202)
    def ingest(request: Request, req: IngestRequest) -> dict[str, str]:
        known = {c.ticker for c in request.app.state.settings.companies}
        if req.tickers and (unknown := sorted(set(req.tickers) - known)):
            raise RequestError(f"Unknown tickers: {', '.join(unknown)}", details={"fields": [{"loc": ["body", "tickers"]}]})
        return {"job_id": request.app.state.jobs.start(req.sources, req.tickers, req.window_dates())}

    @router.get("/admin/jobs/{job_id}", response_model=JobStatus)
    def job_status(request: Request, job_id: str) -> JobStatus:
        return JobStatus(**request.app.state.jobs.get(job_id))

    return router


class IngestRequest(BaseModel):
    """Body of POST /admin/ingest (LLD section 7: {sources, tickers, window}). A window makes it a backfill."""

    model_config = ConfigDict(extra="forbid")
    sources: list[Literal["markets", "filings", "macro"]] = Field(
        default_factory=lambda: list(service.SOURCE_GROUPS), min_length=1)
    tickers: Optional[list[str]] = None
    window: Optional[str] = Field(None, description="YYYY-MM-DD..YYYY-MM-DD, inclusive")

    @field_validator("sources")
    @classmethod
    def _unique(cls, v):
        if len(set(v)) != len(v):
            raise ValueError("sources must not repeat")
        return v

    @field_validator("window")
    @classmethod
    def _window(cls, v):
        if v is not None:
            _parse_window(v)
        return v

    def window_dates(self) -> Optional[tuple[date, date]]:
        return _parse_window(self.window) if self.window else None


def _parse_window(v: str) -> tuple[date, date]:
    try:
        start, end = (date.fromisoformat(p) for p in v.split(".."))
    except ValueError:
        raise ValueError("window must look like 2026-07-01..2026-07-12") from None
    if start > end:
        raise ValueError("window start must not be after its end")
    return start, end


class JobStatus(BaseModel):
    """Body of GET /admin/jobs/{job_id} (LLD section 7: {status, progress, errors} plus what was asked)."""

    job_id: str
    status: Literal["queued", "running", "succeeded", "partial", "failed"]
    sources: list[str]
    tickers: Optional[list[str]] = None
    window: Optional[str] = None
    progress: dict[str, int]
    errors: list[dict[str, str]]
    started_at: datetime
    finished_at: Optional[datetime] = None


def _service(request: Request) -> "service.RunService":
    svc = request.app.state.service
    if svc is None:
        raise StoreError("The database is unavailable")
    return svc


def _aware(t: datetime, name: str) -> datetime:
    if t.tzinfo is None or t.utcoffset() is None:
        raise RequestError(f"{name} must include a timezone", details={"fields": [{"loc": ["query", name]}]})
    return t.astimezone(timezone.utc)


def _as_of(request: Request, as_of: Optional[datetime]) -> datetime:
    """Every read takes an as_of (DD-4): default now, never more than a minute ahead."""
    now = request.app.state.now()
    if as_of is None:
        return now
    as_of = _aware(as_of, "as_of")
    if as_of > now + timedelta(minutes=1):
        raise RequestError("as_of cannot be more than one minute in the future",
                           details={"fields": [{"loc": ["query", "as_of"]}]})
    return as_of


class _IdempotencyKeys:
    """In-memory Idempotency-Key -> run_id for POST /runs (LLD section 7); lost on restart by design."""

    def __init__(self, ttl_s: float, monotonic: Callable[[], float]):
        self.ttl_s, self.monotonic = ttl_s, monotonic
        self.lock = threading.Lock()
        self._keys: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> Optional[str]:
        now = self.monotonic()
        self._keys = {k: v for k, v in self._keys.items() if now - v[1] <= self.ttl_s}
        hit = self._keys.get(key)
        return hit[0] if hit else None

    def put(self, key: str, run_id: str) -> None:
        self._keys[key] = (run_id, self.monotonic())


# ---- request framing: request id, token, body limit, one log line ----

async def _frame(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = rid
    start = time.perf_counter()
    try:
        resp = _guard(request)
        if resp is None:
            resp = await call_next(request)
    except Exception:
        log.exception("unhandled error", extra={"request_id": rid})
        resp = _envelope(500, "E_INTERNAL", "Internal error; quote the request ID when reporting it", {}, rid)
    resp.headers["X-Request-ID"] = rid
    _log(
        "info", "request", method=request.method, path=request.url.path, status=resp.status_code,
        latency_ms=round((time.perf_counter() - start) * 1000, 1), request_id=rid,
    )
    return resp


def _guard(request: Request) -> JSONResponse | None:
    rid = request.state.request_id
    settings = request.app.state.settings
    token = getattr(settings, "api_token", "") if settings is not None else ""
    if token and not hmac.compare_digest(request.headers.get("X-API-Token", ""), token):
        return _envelope(401, "E_UNAUTHORIZED", "Missing or wrong X-API-Token", {}, rid)
    length = request.headers.get("Content-Length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return _envelope(400, "E_VALIDATION", "Request body exceeds 64 KB", {"max_bytes": MAX_BODY_BYTES}, rid)
    return None


# ---- error envelope ----

def _envelope(status: int, code: str, message: str, details: dict, request_id: str) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details, "request_id": request_id}}
    return JSONResponse(body, status_code=status, headers={"X-Request-ID": request_id})


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "")


async def _airscen_error(request: Request, exc: AirScenError) -> JSONResponse:
    status = HTTP_STATUS.get(exc.code)
    if status is None:  # E_LLM_*, E_SOURCE_RATE_LIMITED, E_CONFIG, ...: never meant to reach the API
        return _envelope(500, "E_INTERNAL", exc.message, {**exc.details, "original_code": exc.code}, _rid(request))
    return _envelope(status, exc.code, exc.message, exc.details, _rid(request))


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    fields = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]
    return _envelope(400, "E_VALIDATION", "Request failed validation", {"fields": fields}, _rid(request))


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _CODE_FOR_STATUS.get(exc.status_code) or ("E_INTERNAL" if exc.status_code >= 500 else "E_VALIDATION")
    return _envelope(exc.status_code, code, str(exc.detail), {}, _rid(request))


def _log(level: str, event: str, **fields: Any) -> None:
    line = {"ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "level": level,
            "component": "api", "event": event, **fields}
    log.log(logging.ERROR if level == "error" else logging.INFO, json.dumps(line))


app = create_app()
