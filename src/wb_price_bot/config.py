from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(RuntimeError):
    """Raised when required application configuration is missing or invalid."""


def _read_secret(name: str) -> str:
    direct = os.getenv(name, "").strip()
    file_name = os.getenv(f"{name}_FILE", "").strip()
    if direct and file_name:
        raise ConfigurationError(f"Задайте только {name} или {name}_FILE, не оба значения")
    if file_name:
        try:
            return Path(file_name).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError(f"Не удалось прочитать {name}_FILE: {exc}") from exc
    return direct


def _positive_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} должен быть целым числом") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} должен быть не меньше {minimum}")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} должен быть true или false")


def _parse_allowed_users(raw: str) -> frozenset[int]:
    result: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if not item.isdecimal() or int(item) <= 0:
            raise ConfigurationError("TELEGRAM_ALLOWED_USERS содержит неверный Telegram ID")
        result.add(int(item))
    if not result:
        raise ConfigurationError("TELEGRAM_ALLOWED_USERS не должен быть пустым")
    return frozenset(result)


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_token: str
    session_encryption_key: str
    allowed_users: frozenset[int]
    data_dir: Path
    check_interval_seconds: int = 1800
    check_jitter_seconds: int = 120
    max_products_per_user: int = 200
    max_wb_batch_size: int = 25
    wb_destination: int = -5827722
    wb_currency: str = "rub"
    wb_language: str = "ru"
    price_history_days: int = 180
    log_level: str = "INFO"
    wb_api_url: str = "https://card.wb.ru/cards/v4/detail"
    wb_browser_headless: bool = False

    @property
    def database_path(self) -> Path:
        return self.data_dir / "wb-price-bot.sqlite3"

    @classmethod
    def from_env(cls) -> Settings:
        token = _read_secret("TELEGRAM_BOT_TOKEN")
        if not token or ":" not in token:
            raise ConfigurationError("Не задан корректный TELEGRAM_BOT_TOKEN(_FILE)")
        encryption_key = _read_secret("SESSION_ENCRYPTION_KEY")
        if not encryption_key:
            raise ConfigurationError("Не задан SESSION_ENCRYPTION_KEY(_FILE)")

        destination_raw = os.getenv("WB_DESTINATION", "-5827722").strip()
        try:
            destination = int(destination_raw)
        except ValueError as exc:
            raise ConfigurationError("WB_DESTINATION должен быть целым числом") from exc

        return cls(
            telegram_token=token,
            session_encryption_key=encryption_key,
            allowed_users=_parse_allowed_users(os.getenv("TELEGRAM_ALLOWED_USERS", "")),
            data_dir=Path(os.getenv("DATA_DIR", "./data")).expanduser().resolve(),
            check_interval_seconds=_positive_int("CHECK_INTERVAL_SECONDS", 1800, 900),
            check_jitter_seconds=_positive_int("CHECK_JITTER_SECONDS", 120, 0),
            max_products_per_user=_positive_int("MAX_PRODUCTS_PER_USER", 200, 1),
            max_wb_batch_size=_positive_int("MAX_WB_BATCH_SIZE", 25, 1),
            wb_destination=destination,
            wb_currency=os.getenv("WB_CURRENCY", "rub").strip().lower() or "rub",
            wb_language=os.getenv("WB_LANGUAGE", "ru").strip().lower() or "ru",
            price_history_days=_positive_int("PRICE_HISTORY_DAYS", 180, 1),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            wb_api_url=os.getenv("WB_API_URL", "https://card.wb.ru/cards/v4/detail").strip(),
            wb_browser_headless=_boolean("WB_BROWSER_HEADLESS", False),
        )
