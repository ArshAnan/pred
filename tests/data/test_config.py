"""A1: load_settings reads config.yaml and an explicit environment."""

from pathlib import Path

import pytest
import yaml

from airscen.config import load_settings
from airscen.types import ConfigError

ROOT = Path(__file__).resolve().parents[2]


def _write_config(path: Path, **overrides) -> None:
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    raw.update(overrides)
    path.write_text(yaml.safe_dump(raw))


def test_missing_sec_user_agent_names_the_setting(tmp_path: Path):
    config = tmp_path / "config.yaml"
    _write_config(config)
    with pytest.raises(ConfigError, match="SEC_USER_AGENT"):
        load_settings(config_path=config, environ={"LLM_MODE": "off"})


def test_blank_sec_user_agent_is_missing(tmp_path: Path):
    config = tmp_path / "config.yaml"
    _write_config(config)
    with pytest.raises(ConfigError, match="SEC_USER_AGENT"):
        load_settings(
            config_path=config,
            environ={"SEC_USER_AGENT": "   ", "LLM_MODE": "off"},
        )


def test_load_settings_maps_env_and_yaml(tmp_path: Path):
    config = tmp_path / "config.yaml"
    _write_config(config)
    settings = load_settings(
        config_path=config,
        environ={
            "SEC_USER_AGENT": "Ada Lovelace ada@example.com",
            "FRED_API_KEY": "fred-key",
            "LLM_MODE": "cache_only",
            "API_PORT": "8001",
        },
    )
    assert settings.sec_user_agent == "Ada Lovelace ada@example.com"
    assert settings.fred_api_key == "fred-key"
    assert settings.llm_mode == "cache_only"
    assert settings.api_port == 8001
    assert [c.ticker for c in settings.companies] == ["DAL", "UAL", "AAL"]
    assert settings.poll.market_s == 60
    assert settings.trust.floor == 0.40


def test_live_mode_without_provider_credentials_names_the_provider(tmp_path: Path):
    config = tmp_path / "config.yaml"
    _write_config(config)
    with pytest.raises(ConfigError, match="snowflake"):
        load_settings(
            config_path=config,
            environ={
                "SEC_USER_AGENT": "Ada Lovelace ada@example.com",
                "LLM_MODE": "live",
                "LLM_CHAIN": "snowflake",
            },
        )


def test_ollama_host_satisfies_live_mode(tmp_path: Path):
    config = tmp_path / "config.yaml"
    _write_config(config)
    settings = load_settings(
        config_path=config,
        environ={
            "SEC_USER_AGENT": "Ada Lovelace ada@example.com",
            "LLM_MODE": "live",
            "LLM_CHAIN": "ollama",
            "OLLAMA_HOST": "http://127.0.0.1:11434",
        },
    )
    assert settings.llm_chain == "ollama"


def test_unknown_section_names_the_setting(tmp_path: Path):
    config = tmp_path / "config.yaml"
    _write_config(config, not_a_section={})
    with pytest.raises(ConfigError, match="not_a_section"):
        load_settings(
            config_path=config,
            environ={"SEC_USER_AGENT": "Ada Lovelace ada@example.com", "LLM_MODE": "off"},
        )


def test_unknown_key_inside_a_section_is_ignored(tmp_path: Path):
    config = tmp_path / "config.yaml"
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    raw["trust"]["not_in_the_lld"] = 1
    config.write_text(yaml.safe_dump(raw))
    settings = load_settings(
        config_path=config,
        environ={"SEC_USER_AGENT": "Ada Lovelace ada@example.com", "LLM_MODE": "off"},
    )
    assert settings.trust.floor == 0.40
    assert not hasattr(settings.trust, "not_in_the_lld")
