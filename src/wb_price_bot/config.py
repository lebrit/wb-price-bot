from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
    timezone_name: str = "Asia/Irkutsk"
    max_bulk_import: int = 50
    max_rules_per_product: int = 10
    wb_geo_url: str = "https://user-geo-data.wildberries.ru/get-geo-info"
    mpstats_token: str = ""
    mpstats_api_url: str = "https://mpstats.io/api/analytics/v1/wb"
    mpstats_max_age_hours: int = 24
    auth_public_url: str = ""
    auth_bind_host: str = "0.0.0.0"
    auth_port: int = 8080
    auth_session_ttl_seconds: int = 600
    auth_max_concurrent_sessions: int = 2
    registration_mode: str = "approval"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "wb-price-bot.sqlite3"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_public_url)

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

        timezone_name = os.getenv("APP_TIMEZONE", "Asia/Irkutsk").strip()
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(
                "APP_TIMEZONE должна быть IANA-зоной, например Asia/Irkutsk"
            ) from exc

        auth_public_url = (
            os.getenv("AUTH_PUBLIC_URL", "").strip()
            or os.getenv("AUTH_FALLBACK_PUBLIC_URL", "").strip()
        ).rstrip("/")
        if auth_public_url:
            parsed_auth_url = urlparse(auth_public_url)
            if (
                parsed_auth_url.scheme != "https"
                or not parsed_auth_url.hostname
                or parsed_auth_url.username
                or parsed_auth_url.password
                or parsed_auth_url.query
                or parsed_auth_url.fragment
                or parsed_auth_url.path not in {"", "/"}
            ):
                raise ConfigurationError(
                    "AUTH_PUBLIC_URL должен быть HTTPS-адресом без логина, пути, query и fragment"
                )
        registration_mode = os.getenv("REGISTRATION_MODE", "approval").strip().lower()
        if registration_mode not in {"approval", "open", "allowlist"}:
            raise ConfigurationError("REGISTRATION_MODE должен быть approval, open или allowlist")
        auth_port = _positive_int("AUTH_PORT", 8080, 1)
        if auth_port > 65535:
            raise ConfigurationError("AUTH_PORT должен быть от 1 до 65535")
        auth_slots = _positive_int("AUTH_MAX_CONCURRENT_SESSIONS", 2, 1)
        if auth_slots > 10:
            raise ConfigurationError("AUTH_MAX_CONCURRENT_SESSIONS должен быть от 1 до 10")

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
            timezone_name=timezone_name,
            max_bulk_import=_positive_int("MAX_BULK_IMPORT", 50, 1),
            max_rules_per_product=_positive_int("MAX_RULES_PER_PRODUCT", 10, 1),
            wb_geo_url=os.getenv(
                "WB_GEO_URL", "https://user-geo-data.wildberries.ru/get-geo-info"
            ).strip(),
            mpstats_token=_read_secret("MPSTATS_TOKEN"),
            mpstats_api_url=os.getenv(
                "MPSTATS_API_URL", "https://mpstats.io/api/analytics/v1/wb"
            ).strip(),
            mpstats_max_age_hours=_positive_int("MPSTATS_MAX_AGE_HOURS", 24, 1),
            auth_public_url=auth_public_url,
            auth_bind_host=os.getenv("AUTH_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0",
            auth_port=auth_port,
            auth_session_ttl_seconds=_positive_int("AUTH_SESSION_TTL_SECONDS", 600, 300),
            auth_max_concurrent_sessions=auth_slots,
            registration_mode=registration_mode,
        )
