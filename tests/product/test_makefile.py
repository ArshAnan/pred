"""C0.2: the Makefile exposes the LLD section 9 runbook targets.

Uses `make -n` (dry run) so nothing is installed or started.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKING = ["setup", "api", "ui", "test", "ingest", "backfill", "flip"]
PLACEHOLDERS = ["warm", "eval"]

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")


def make(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["make", "--no-print-directory", *args],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )


def dry_run(target: str, *extra: str) -> str:
    proc = make("-n", target, *extra)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_test_target_runs_pytest():
    assert "-m pytest" in dry_run("test")


def test_api_target_starts_one_uvicorn_worker_on_localhost():
    out = dry_run("api")
    assert "uvicorn airscen.api:app" in out
    assert "--host 127.0.0.1" in out
    assert "--port 8000" in out
    assert "--workers 1" in out  # DD-2: never more than one worker


def test_ui_target_starts_streamlit_on_localhost():
    out = dry_run("ui")
    assert "streamlit run app.py" in out
    assert "--server.address 127.0.0.1" in out
    assert "--server.port 8501" in out


def test_setup_target_creates_venv():
    assert "-m venv .venv" in dry_run("setup")


def test_setup_installs_dev_extra_so_pytest_is_available(tmp_path):
    # pytest lives in pyproject's [dev] extra; a plain `pip install -e .` skips it.
    shutil.copy(ROOT / "Makefile", tmp_path / "Makefile")
    (tmp_path / "pyproject.toml").write_text("")
    proc = subprocess.run(["make", "-n", "setup"], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert 'pip install -e ".[dev]"' in proc.stdout


def test_ingest_target_runs_the_cli():
    assert "-m airscen.cli ingest" in dry_run("ingest")


def test_backfill_target_passes_the_window_to_the_cli():
    assert "-m airscen.cli backfill --window 2026-07-01..2026-07-12" in dry_run("backfill", "WINDOW=2026-07-01..2026-07-12")


def test_flip_target_runs_the_cli():
    assert "-m airscen.cli flip" in dry_run("flip")


def test_backfill_without_window_fails_with_usage():
    proc = make("backfill")
    assert proc.returncode != 0
    assert "WINDOW=" in proc.stdout + proc.stderr


@pytest.mark.parametrize("target", PLACEHOLDERS)
def test_placeholders_fail_loudly(target):
    proc = make(target, "WINDOW=2026-07-01..2026-07-12")
    assert proc.returncode != 0, f"{target} must not pretend to succeed"
    assert "not implemented yet" in proc.stdout + proc.stderr


def test_help_lists_every_runbook_target():
    out = make("help").stdout
    for target in WORKING + PLACEHOLDERS:
        assert target in out
