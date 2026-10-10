"""Shared fixtures. Frozen after CP0 (CP0_DECISIONS D6)."""

from pathlib import Path

import pytest

from airscen.config import Settings


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "app.db"


@pytest.fixture
def settings() -> Settings:
    return Settings()
