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

API_HOST ?= 127.0.0.1
API_PORT ?= 8000
UI_HOST ?= 127.0.0.1
UI_PORT ?= 8501
PYTEST_ARGS ?=

.DEFAULT_GOAL := help
.PHONY: help setup api ui test ingest backfill warm eval

help:
	@echo Targets:
	@echo   setup     create .venv, install requirements and the package with dev extras, copy .env.example to .env
	@echo   api       start the API server on $(API_HOST):$(API_PORT), one worker
	@echo   ui        start Streamlit on $(UI_HOST):$(UI_PORT)
	@echo   test      run pytest, extra flags via PYTEST_ARGS=...
	@echo   ingest    fetch markets, filings and macro through the API, needs make api running
	@echo   backfill  backfill quotes for a replay window, usage: make backfill WINDOW=2026-07-01..2026-07-12
	@echo   warm      placeholder until C14
	@echo   eval      placeholder until C14

setup:
	$(PYTHON) -m venv .venv
	$(if $(wildcard requirements.txt),$(VENV_PY) -m pip install -r requirements.txt,@echo requirements.txt not present - using pyproject.toml)
	$(if $(wildcard pyproject.toml),$(VENV_PY) -m pip install -e ".[dev]",@echo pyproject.toml not found - nothing to install)
	$(if $(wildcard .env),@echo .env already exists - left unchanged,$(if $(wildcard .env.example),$(PYTHON) -c "import shutil; shutil.copyfile('.env.example', '.env')",@echo .env.example not found yet - skipping .env))

# DD-2: exactly one uvicorn worker; it also hosts the background worker loop.
api:
	$(PY) -m uvicorn airscen.api:app --host $(API_HOST) --port $(API_PORT) --workers 1

ui:
	$(PY) -m streamlit run app.py --server.address $(UI_HOST) --server.port $(UI_PORT)

test:
	$(PY) -m pytest $(PYTEST_ARGS)

# Both need `make api` running; they start a job through POST /admin/ingest and wait for it.
ingest:
	$(PY) -m airscen.cli ingest

backfill:
	$(if $(WINDOW),$(PY) -m airscen.cli backfill --window $(WINDOW),$(error usage: make backfill WINDOW=2026-07-01..2026-07-12))

# Placeholders: fail loudly so nobody mistakes them for a working step.
warm:
	$(error make warm: not implemented yet - wired in C14)

eval:
	$(error make eval: not implemented yet - wired to lane B's eval module in C14)
