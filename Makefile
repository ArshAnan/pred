# Runbook targets from LLD section 9. Owner: lane C (Product).
# Recipes avoid shell-specific syntax so they work under sh (macOS, Linux) and cmd.exe (Windows).

ifeq ($(OS),Windows_NT)
  # Same shell whether make is started from PowerShell, cmd or Git Bash.
  SHELL := cmd.exe
  PYTHON ?= python
  VENV_PY := .venv\Scripts\python.exe
  VENV_PY_FILE := .venv/Scripts/python.exe
else
  PYTHON ?= python3
  VENV_PY := .venv/bin/python
  VENV_PY_FILE := $(VENV_PY)
endif

# Use the project venv once `make setup` has created it.
PY := $(if $(wildcard $(VENV_PY_FILE)),$(VENV_PY),$(PYTHON))

# src layout: recipes import airscen from src/ as well as from an editable install.
ifeq ($(OS),Windows_NT)
  export PYTHONPATH := src$(if $(PYTHONPATH),;$(PYTHONPATH))
else
  export PYTHONPATH := src$(if $(PYTHONPATH),:$(PYTHONPATH))
endif

API_HOST ?= 127.0.0.1
API_PORT ?= 8000
UI_HOST ?= 127.0.0.1
UI_PORT ?= 8501
PYTEST_ARGS ?=

.DEFAULT_GOAL := help
.PHONY: help setup api ui web test ingest backfill flip warm eval

help:
	@echo Targets:
	@echo   setup     create .venv, install requirements and the package with dev extras, copy .env.example to .env
	@echo   api       start the API server on $(API_HOST):$(API_PORT), one worker
	@echo   ui        start Streamlit on $(UI_HOST):$(UI_PORT)
	@echo   web       start the TypeScript app on 127.0.0.1:5173 (needs make api)
	@echo   test      run pytest, extra flags via PYTEST_ARGS=...
	@echo   ingest    fetch markets, filings and macro through the API, needs make api running
	@echo   backfill  backfill quotes for a replay window, usage: make backfill WINDOW=2026-07-01..2026-07-12
	@echo   flip      demo: force a BREACH_FLIP alert to Slack, needs make api running
	@echo   warm      fill the LLM cache for the demo questions, optional AS_OF=2026-10-11T14:00:00Z
	@echo   eval      print the five-check evaluation report

setup:
	$(PYTHON) -m venv .venv
	$(if $(wildcard requirements.txt),$(VENV_PY) -m pip install -r requirements.txt,@echo requirements.txt not present - using pyproject.toml)
	$(if $(wildcard pyproject.toml),$(VENV_PY) -m pip install -e ".[dev]",@echo pyproject.toml not found - nothing to install)
	$(if $(wildcard .env),@echo .env already exists - left unchanged,$(if $(wildcard .env.example),$(PYTHON) -c "import shutil; shutil.copyfile('.env.example', '.env')",@echo .env.example not found yet - skipping .env))

# DD-2: exactly one uvicorn worker; it also hosts the background worker loop.
# airscen.main wires every lane into the app (airscen.api:app alone runs without the worker).
api:
	$(PY) -m uvicorn airscen.main:app --host $(API_HOST) --port $(API_PORT) --workers 1

ui:
	$(PY) -m streamlit run app.py --server.address $(UI_HOST) --server.port $(UI_PORT)

web:
	cd frontend && npm run dev -- --host 127.0.0.1 --port 5173

test:
	$(PY) -m pytest $(PYTEST_ARGS)

# Both need `make api` running; they start a job through POST /admin/ingest and wait for it.
ingest:
	$(PY) -m airscen.cli ingest

backfill:
	$(if $(WINDOW),$(PY) -m airscen.cli backfill --window $(WINDOW),$(error usage: make backfill WINDOW=2026-07-01..2026-07-12))

# Demo: force a BREACH_FLIP through the real alert state machine (needs make api running).
flip:
	$(PY) -m airscen.cli flip

# Fill llm_cache for the demo questions (needs make api running and a live LLM), then set LLM_MODE=cache_only.
warm:
	$(PY) -m airscen.cli warm$(if $(AS_OF), --as-of $(AS_OF))

# Lane B's five-check evaluation report.
eval:
	$(PY) -m airscen.eval
