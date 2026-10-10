# Dependencies to add (for lane A)

From: lane C, 2026-10-10. Owner of the change: lane A (`pyproject.toml`).
Delete this file once the change is merged.

## Why

`pyproject.toml` lists only `pydantic` (and `pytest` in `dev`). CP0_DECISIONS R3 fixes the full set at CP0.
Without them, lane C cannot start C1 (API skeleton) or C3 (UI client), and `make api` / `make ui` cannot run.
The CP0 exit test passes without them (`make test` is green), so this can be the first contract PR of CP1.

## Proposed change to `pyproject.toml`

```toml
[project]
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

[project.optional-dependencies]
dev = ["pytest>=8,<9", "hypothesis", "respx"]
```

- Runtime libraries go in `dependencies`; test-only tools (`hypothesis`, `respx`) go in `dev`, next to `pytest`.
- `make setup` installs `-e ".[dev]"`, so one command installs everything.
- R3 mentions `requirements.txt`; one list in `pyproject.toml` is enough. The Makefile still installs `requirements.txt` if one is added later.

## Done when

On a fresh clone: `make setup` succeeds, `make test` is green, and
`python -c "import fastapi, uvicorn, httpx, streamlit, yaml, numpy, respx, hypothesis"` works.

## Not urgent (lane C will propose at C7 as a small contract PR)

`types.py` models for the admin endpoints in LLD section 7:
`IngestRequest {sources, tickers, window}`, `JobStatus {status, progress, errors}`, `AlertTestResult {delivered}`.
