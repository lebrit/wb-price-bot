from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest

from wb_price_bot.telegram_auth import TelegramInitDataError, validate_init_data

TOKEN = "123456:abcdefghijklmnopqrstuvwxyzABCDE"


def signed_init_data(*, user_id: int = 1001, at: datetime | None = None) -> str:
    values = {
        "auth_date": str(int((at or datetime.now(UTC)).timestamp())),
        "query_id": "AAEAAAE",
        "signature": "telegram-ed25519-signature",
        "user": json.dumps(
            {"id": user_id, "first_name": "Иван", "username": "owner"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_validate_init_data_returns_signed_user() -> None:
    user = validate_init_data(signed_init_data(), TOKEN)
    assert user.id == 1001
    assert user.username == "owner"
    assert user.first_name == "Иван"


def test_validate_init_data_rejects_tampering() -> None:
    raw = signed_init_data().replace("1001", "1002")
    with pytest.raises(TelegramInitDataError, match="Подпись"):
        validate_init_data(raw, TOKEN)


def test_validate_init_data_rejects_expired_payload() -> None:
    raw = signed_init_data(at=datetime.now(UTC) - timedelta(minutes=10))
    with pytest.raises(TelegramInitDataError, match="истёк"):
        validate_init_data(raw, TOKEN)


def test_validate_init_data_rejects_duplicate_fields() -> None:
    raw = f"{signed_init_data()}&auth_date=1"
    with pytest.raises(TelegramInitDataError, match="повторяющиеся"):
        validate_init_data(raw, TOKEN)
