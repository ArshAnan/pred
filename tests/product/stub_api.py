"""Demo API on the worked example: the REAL app (C1 + C6 endpoints + RunService) backed by the
TEMPORARY in-memory FakeStore seeded with the worked example, with the clock fixed at its as_of.
Replace FakeStore with lane A's store once it is on main.

ILLUSTRATIVE numbers: the WTI ladder, rate quotes, DGS10 and baseline WTI are assumptions
(see fake_store.py and tests/fixtures/worked_example.json).

Try the Run page by hand (two terminals):
    .venv/Scripts/python tests/product/stub_api.py      # API on 127.0.0.1:8000
    make ui                                              # Streamlit on 127.0.0.1:8501
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # so `fake_store` imports when run as a script

from airscen.api import create_app  # noqa: E402
from airscen.brief import template_brief  # noqa: E402
from airscen.config import Settings  # noqa: E402
from airscen.types import Brief, RunRequest, RunResult, ScenarioRow, ScenarioSummary  # noqa: E402
from fake_store import AS_OF, FakeStore, seed_worked_example  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "worked_example.json"


def load_worked_example() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def worked_example_result(req: RunRequest, run_id: str = "run_stub", created_at: datetime | None = None) -> RunResult:
    """A RunResult straight from the fixture's expected values (used by the C4 page tests)."""
    ex = load_worked_example()
    expected = ex["expected"]
    as_of = req.as_of or datetime.fromisoformat(ex["as_of"].replace("Z", "+00:00"))
    result = RunResult(
        run_id=run_id, company=req.ticker, as_of=as_of,
        created_at=created_at or datetime.now(timezone.utc),
        inputs=req.model_copy(update={"as_of": as_of}),
        rows=[ScenarioRow(**r) for r in expected["rows"]],
        summary=ScenarioSummary(**expected["summary"]),
        peers=[], flags=list(expected["flags"]), data_version="stub-worked-example",
        brief=Brief(template_md="", llm_md=None, llm_status="skipped"),
    )
    return result.model_copy(update={"brief": Brief(template_md=template_brief(result), llm_md=None, llm_status="skipped")})


def create_stub_app():
    store = FakeStore()
    seed_worked_example(store)
    return create_app(settings_loader=lambda: Settings(llm_mode="off"), repo_factory=lambda s: store,
                      clock=lambda: AS_OF)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_stub_app(), host="127.0.0.1", port=8000, workers=1)
