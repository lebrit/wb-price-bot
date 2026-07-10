from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from wb_price_bot.config import ConfigurationError, Settings


def _base_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyz")
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "1001,1002")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _base_env(monkeypatch, tmp_path)
    settings = Settings.from_env()
    assert settings.allowed_users == frozenset({1001, 1002})
    assert settings.check_interval_seconds == 1800
    assert settings.wb_destination == -5827722
    assert settings.database_path.parent == tmp_path.resolve()


def test_interval_below_safe_minimum_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CHECK_INTERVAL_SECONDS", "60")
    with pytest.raises(ConfigurationError, match="900"):
        Settings.from_env()


def test_empty_allowlist_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "")
    with pytest.raises(ConfigurationError, match="пустым"):
        Settings.from_env()
