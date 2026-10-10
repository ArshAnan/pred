import pytest

from airscen.config import load_settings
from airscen.types import ConfigError


def test_load_settings_reads_config_and_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("poll: {market_s: 45}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "SEC_USER_AGENT=Example User example@example.com\n"
        "FRED_API_KEY=local-key\nLLM_MODE=off\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FRED_API_KEY", "process-key")

    settings = load_settings()

    assert settings.poll.market_s == 45
    assert settings.fred_api_key == "process-key"
    assert settings.sec_user_agent == "Example User example@example.com"
    assert settings.companies[0].ticker == "DAL"


def test_missing_sec_user_agent_names_setting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("FRED_API_KEY=local-key\nLLM_MODE=off\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="SEC_USER_AGENT"):
        load_settings()


def test_missing_fred_key_names_setting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "SEC_USER_AGENT=Example User example@example.com\nLLM_MODE=off\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="FRED_API_KEY"):
        load_settings()
