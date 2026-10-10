"""FastAPI app: routes, request framing and the error envelope (LLD section 7, CP0_DECISIONS D3).

Run with exactly one worker (DD-2): `uvicorn airscen.api:app --workers 1`.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from airscen import config, store
from airscen.types import AirScenError, Company, HealthStatus, LLMStatus, StoreError, WorkerStatus

PREFIX = "/api/v1"
DB_PATH = Path("data") / "app.db"
MAX_BODY_BYTES = 64 * 1024

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


def create_app(
    settings_loader: Callable[[], Any] = config.load_settings,
    repo_factory: Callable[[Any], Any] = _default_repo,
) -> FastAPI:
    """Build the app. Tests pass fakes; the real app uses lane A's loader and Repository."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings_loader()  # ConfigError propagates: refuse to start
        app.state.repo = None
        try:
            repo = repo_factory(app.state.settings)
            repo.migrate()
            app.state.repo = repo
        except StoreError as e:
            _log("error", "db_unavailable", error_code=e.code, message=e.message)
        yield

    app = FastAPI(title="AirScen API", version="1", lifespan=lifespan)
    app.state.settings = None
    app.state.repo = None
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

    return router


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
