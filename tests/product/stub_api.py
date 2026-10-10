"""TEMPORARY stub API for the C4 Run page. Remove when C6 adds the real POST /runs.

It is the real C1 app (framing, error envelope, /health, /companies) plus POST /runs and
GET /runs/{run_id} that return lane B's worked example (tests/fixtures/worked_example.json).
ILLUSTRATIVE numbers: the WTI ladder, rate quotes and baseline WTI are assumptions.

Try the page by hand (two terminals):
    .venv/Scripts/python tests/product/stub_api.py      # API stub on 127.0.0.1:8000
    make ui                                              # Streamlit on 127.0.0.1:8501
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import Request

from airscen.api import PREFIX, create_app
from airscen.brief import template_brief
from airscen.types import Brief, RequestError, RunRequest, RunResult, ScenarioRow, ScenarioSummary

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "worked_example.json"
STUB_DATA_VERSION = "stub-worked-example"


def load_worked_example() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def worked_example_result(req: RunRequest, run_id: str = "run_stub", created_at: datetime | None = None) -> RunResult:
    """RunResult for DAL from the worked example; the request's own inputs are echoed back."""
    ex = load_worked_example()
    expected = ex["expected"]
    as_of = req.as_of or datetime.fromisoformat(ex["as_of"].replace("Z", "+00:00"))
    result = RunResult(
        run_id=run_id, company=req.ticker, as_of=as_of,
        created_at=created_at or datetime.now(timezone.utc),
        inputs=req.model_copy(update={"as_of": as_of}),
        rows=[ScenarioRow(**r) for r in expected["rows"]],
        summary=ScenarioSummary(**expected["summary"]),
        peers=[], flags=list(expected["flags"]), data_version=STUB_DATA_VERSION,
        brief=Brief(template_md="", llm_md=None, llm_status="skipped"),
    )
    return result.model_copy(update={"brief": Brief(template_md=template_brief(result), llm_md=None, llm_status="skipped")})


def create_stub_app():
    settings = SimpleNamespace(
        api_token="", llm_mode="off",
        companies=[SimpleNamespace(ticker="DAL", cik=27904, name="Delta Air Lines", peers=["UAL", "AAL"])],
    )

    class NoStore:
        def migrate(self):
            pass

    app = create_app(settings_loader=lambda: settings, repo_factory=lambda s: NoStore())
    runs: dict[str, RunResult] = {}

    @app.post(PREFIX + "/runs", status_code=201, response_model=RunResult)
    def create_run(req: RunRequest, request: Request) -> RunResult:
        if req.ticker != "DAL":
            raise RequestError(f"The stub only has DAL, not {req.ticker}", code="E_NOT_FOUND")
        run = worked_example_result(req, run_id=f"run_{uuid.uuid4().hex[:6]}")
        runs[run.run_id] = run
        return run

    @app.get(PREFIX + "/runs/{run_id}", response_model=RunResult)
    def get_run(run_id: str) -> RunResult:
        if run_id not in runs:
            raise RequestError(f"Unknown run {run_id}", code="E_NOT_FOUND")
        return runs[run_id]

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_stub_app(), host="127.0.0.1", port=8000, workers=1)
