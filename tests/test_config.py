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
    monkeypatch.setenv("AUTH_PUBLIC_URL", "https://auth.example.com")
    monkeypatch.setenv("REGISTRATION_MODE", "approval")
    settings = Settings.from_env()
    assert settings.allowed_users == frozenset({1001, 1002})
    assert settings.check_interval_seconds == 1800
    assert settings.wb_destination == -5827722
    assert settings.database_path.parent == tmp_path.resolve()
    assert settings.auth_enabled is True
    assert settings.auth_public_url == "https://auth.example.com"
    assert settings.registration_mode == "approval"


def test_legacy_installer_uses_safe_auth_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.delenv("AUTH_PUBLIC_URL", raising=False)
    monkeypatch.setenv("AUTH_FALLBACK_PUBLIC_URL", "https://localhost")
    assert Settings.from_env().auth_public_url == "https://localhost"


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


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("AUTH_PUBLIC_URL", "http://auth.example.com", "HTTPS"),
        ("AUTH_PUBLIC_URL", "https://user@auth.example.com", "HTTPS"),
        ("AUTH_PUBLIC_URL", "https://auth.example.com/path", "HTTPS"),
        ("REGISTRATION_MODE", "unknown", "approval"),
    ],
)
def test_web_auth_configuration_is_validated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
    match: str,
) -> None:
    _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError, match=match):
        Settings.from_env()
