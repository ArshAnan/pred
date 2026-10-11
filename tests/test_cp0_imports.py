"""CP0 exit check: every lane's modules import, and the frozen contract resolves."""

import importlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import ValidationError

from airscen.config import Settings
from airscen.store import Repository
from airscen.types import MarketMeta
from airscen.ui.client import ApiError
from airscen.worker import Adapters, WorkerLoop

ROOT = Path(__file__).resolve().parents[1]

MODULES = [
    "airscen.types",
    "airscen.config",
    "airscen.store",
    "airscen.adapters.http",
    "airscen.adapters.kalshi",
    "airscen.adapters.polymarket",
    "airscen.adapters.edgar",
    "airscen.adapters.fred",
    "airscen.adapters.alpaca",
    "airscen.adapters.gdelt",
    "airscen.extract",
    "airscen.worker",
    "airscen.llm",
    "airscen.scout",
    "airscen.trust",
    "airscen.factors",
    "airscen.scenario",
    "airscen.eval",
    "airscen.brief",
    "airscen.service",
    "airscen.alerts",
    "airscen.api",
    "airscen.ui.client",
    "airscen.cli",
    "airscen.eval.__main__",
    "app",
]


@pytest.mark.parametrize("name", MODULES)
def test_lane_modules_import(name):
    importlib.import_module(name)


def test_worker_constructor_matches_d5():
    hints = get_type_hints(WorkerLoop.__init__)
    assert hints["repo"] is Repository
    assert hints["adapters"] is Adapters
    assert hints["settings"] is Settings
    assert hints["label_fn"] == Callable[[list[MarketMeta]], None]
    assert hints["after_market_tick"] == Callable[[datetime], None] | None


def test_config_yaml_matches_settings():
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    settings = Settings.model_validate(raw)
    assert [c.ticker for c in settings.companies] == ["DAL", "UAL", "AAL"]
    assert settings.companies[0].cik == 27904
    assert settings.companies[1].cik == 100517
    assert settings.companies[2].cik == 6201
    assert settings.poll.market_s == 60
    assert settings.trust.floor == 0.40
    assert settings.trust.weights.spread == 0.35
    assert settings.fuel.weights == (0.3, 0.4, 0.3)
    assert settings.alerts.watch == ["DAL"]
    assert settings.alerts.breach_threshold == 0.20
    assert settings.llm.max_calls_per_hour == 120
    assert settings.llm_mode == "live"


def test_settings_section_ignores_unknown_keys():
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    raw["trust"]["not_in_the_lld"] = 1
    settings = Settings.model_validate(raw)
    assert settings.trust.floor == 0.40
    assert not hasattr(settings.trust, "not_in_the_lld")


def test_settings_rejects_an_unknown_section():
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    raw["not_a_section"] = {}
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


def test_api_error_carries_the_banner_fields():
    err = ApiError("E_NOT_FOUND", "Unknown ticker DAL", "req-1", 404)
    assert err.code == "E_NOT_FOUND"
    assert err.message == "Unknown ticker DAL"
    assert err.request_id == "req-1"
    assert err.status == 404


def test_fuel_curve_takes_as_of_because_the_engine_has_no_clock():
    import inspect

    from airscen.factors import fuel_curve

    assert list(inspect.signature(fuel_curve).parameters) == ["ladder", "as_of", "horizon_days"]
