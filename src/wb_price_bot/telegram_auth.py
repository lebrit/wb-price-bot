from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl


class TelegramInitDataError(ValueError):
    """Raised when Telegram Mini App initData cannot be trusted."""


@dataclass(frozen=True, slots=True)
class TelegramMiniAppUser:
    id: int
    username: str | None
    first_name: str
    last_name: str


def validate_init_data(
    raw: str,
    bot_token: str,
    *,
    max_age_seconds: int = 300,
    now: datetime | None = None,
) -> TelegramMiniAppUser:
    if not raw or len(raw.encode("utf-8")) > 16_384:
        raise TelegramInitDataError("Telegram initData отсутствует или слишком велик")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise TelegramInitDataError("Telegram initData имеет неверный формат") from exc
    values = dict(pairs)
    if len(values) != len(pairs):
        raise TelegramInitDataError("Telegram initData содержит повторяющиеся поля")
    received_hash = values.pop("hash", "")
    if len(received_hash) != 64:
        raise TelegramInitDataError("В Telegram initData отсутствует hash")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise TelegramInitDataError("Подпись Telegram initData не совпадает")

    try:
        auth_date = datetime.fromtimestamp(int(values["auth_date"]), UTC)
    except (KeyError, ValueError, OSError, OverflowError) as exc:
        raise TelegramInitDataError("Telegram auth_date отсутствует или неверен") from exc
    current = (now or datetime.now(UTC)).astimezone(UTC)
    age = (current - auth_date).total_seconds()
    if age < -30 or age > max_age_seconds:
        raise TelegramInitDataError("Срок действия Telegram initData истёк")

    try:
        payload: Any = json.loads(values["user"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise TelegramInitDataError("Telegram не передал пользователя") from exc
    if not isinstance(payload, dict):
        raise TelegramInitDataError("Telegram user имеет неверный формат")
    user_id = payload.get("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        raise TelegramInitDataError("Telegram user id имеет неверный формат")
    return TelegramMiniAppUser(
        id=user_id,
        username=_optional_string(payload.get("username"), 64),
        first_name=_optional_string(payload.get("first_name"), 128) or "",
        last_name=_optional_string(payload.get("last_name"), 128) or "",
    )


def _optional_string(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:limit] if cleaned else None
