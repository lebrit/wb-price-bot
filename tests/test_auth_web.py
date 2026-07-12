from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from wb_price_bot.auth_web import (
    AuthWebService,
    _allowed_wb_navigation,
    _connector_confirms_account,
    _contains_captcha,
    _localized_connector_url,
    _login_feedback,
    _normalize_code,
    _normalize_phone,
    _open_phone_form,
    _phone_country,
)
from wb_price_bot.config import Settings
from wb_price_bot.database import Database
from wb_price_bot.security import SessionCipher


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, telegram_id: int, text: str, **__: Any) -> None:
        self.messages.append((telegram_id, text))


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_json(self, value: dict[str, Any]) -> None:
        self.messages.append(value)


class FakeLocator:
    def __init__(self, present: bool) -> None:
        self.present = present

    async def count(self) -> int:
        return int(self.present)

    def nth(self, _: int) -> FakeLocator:
        return self

    async def is_visible(self) -> bool:
        return self.present

    async def is_enabled(self) -> bool:
        return self.present


class FallbackStorefrontPage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.visited: list[str] = []

    async def goto(self, url: str, **__: Any) -> Any:
        self.url = url
        self.visited.append(url)
        return type("Response", (), {"status": 498 if "wildberries.ru" in url else 200})()

    async def wait_for_load_state(self, *_: Any, **__: Any) -> None:
        return None

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(
            self.url.startswith("https://www.wildberries.by")
            and selector == 'input[autocomplete="tel"]'
        )


def _signed_init_data(token: str, telegram_id: int) -> str:
    values = {
        "auth_date": str(int(datetime.now(UTC).timestamp())),
        "query_id": "AAEAAAE",
        "user": json.dumps(
            {"id": telegram_id, "first_name": "Test"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


@pytest.mark.asyncio
async def test_form_page_is_one_time_and_has_no_remote_screen(settings: Settings) -> None:
    configured = replace(settings, auth_public_url="https://auth.example.com")
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, "owner", "Owner", is_admin=True)
    auth_session = await database.create_auth_session(1001, 600)
    service = AuthWebService(
        settings=configured,
        database=database,
        cipher=SessionCipher(settings.session_encryption_key),
        bot=FakeBot(),  # type: ignore[arg-type]
    )
    client = TestClient(TestServer(service.create_app()))
    await client.start_server()
    try:
        assert service._allow_code_request(1001) is True
        assert service._allow_code_request(1001) is False
        assert service._allow_code_request(1002) is True

        health = await client.get("/health")
        assert health.status == 200
        assert await health.json() == {
            "ok": True,
            "mode": "telegram-form-browser",
            "active": 0,
            "pending": 1,
            "limit": 1,
        }

        page = await client.get(f"/login/{auth_session.id}")
        body = await page.text()
        assert page.status == 200
        assert 'autocomplete="tel"' in body
        assert 'autocomplete="one-time-code"' in body
        assert "снимках или записях экрана" in body
        assert "screen-wrap" not in body
        assert "pointerdown" not in body
        assert "extension" not in body.lower()
        assert "telegram.org/js/telegram-web-app.js" in body
        assert "frame-ancestors" in page.headers["Content-Security-Policy"]

        assert await database.set_auth_session_status(
            auth_session.id, "active", expected_statuses=("pending",)
        )
        used_page = await client.get(f"/login/{auth_session.id}")
        assert used_page.status == 410
    finally:
        await client.close()
        await database.close()


@pytest.mark.asyncio
async def test_phone_form_uses_official_fallback_after_498() -> None:
    page = FallbackStorefrontPage()
    websocket = FakeWebSocket()
    locator, origin = await _open_phone_form(
        page,
        websocket,  # type: ignore[arg-type]
        TimeoutError,
    )
    assert locator.present is True
    assert origin == "https://www.wildberries.by"
    assert page.visited == [
        "https://www.wildberries.ru/security/login?returnUrl=https%3A%2F%2Fwww.wildberries.ru%2F",
        "https://www.wildberries.by/security/login?returnUrl=https%3A%2F%2Fwww.wildberries.by%2F",
    ]
    assert any("резервный домен" in item["text"] for item in websocket.messages)


@pytest.mark.asyncio
async def test_websocket_rejects_foreign_origin_and_unsigned_telegram(
    settings: Settings,
) -> None:
    configured = replace(settings, auth_public_url="https://auth.example.com")
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, "owner", "Owner", is_admin=True)
    auth_session = await database.create_auth_session(1001, 600)
    service = AuthWebService(
        settings=configured,
        database=database,
        cipher=SessionCipher(settings.session_encryption_key),
        bot=FakeBot(),  # type: ignore[arg-type]
    )
    client = TestClient(TestServer(service.create_app()))
    await client.start_server()
    try:
        with pytest.raises(WSServerHandshakeError) as rejected:
            await client.ws_connect(
                f"/ws/{auth_session.id}", headers={"Origin": "https://evil.example"}
            )
        assert rejected.value.status == 403

        ws = await client.ws_connect(
            f"/ws/{auth_session.id}", headers={"Origin": "https://auth.example.com"}
        )
        await ws.send_json({"type": "init", "initData": "unsigned"})
        response = await ws.receive_json()
        assert response["type"] == "error"
        assert response["fatal"] is True
        await ws.close()

        wrong_owner = await client.ws_connect(
            f"/ws/{auth_session.id}", headers={"Origin": "https://auth.example.com"}
        )
        await wrong_owner.send_json(
            {"type": "init", "initData": _signed_init_data(settings.telegram_token, 1002)}
        )
        owner_response = await wrong_owner.receive_json()
        assert owner_response == {
            "type": "error",
            "text": "Окно входа создано для другого пользователя",
            "fatal": True,
        }
        await wrong_owner.close()
        stored = await database.get_auth_session(auth_session.id, 1001)
        assert stored is not None and stored.status == "pending"
    finally:
        await client.close()
        await database.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9991234567", "+79991234567"),
        ("8 (999) 123-45-67", "+79991234567"),
        ("+7 999 123-45-67", "+79991234567"),
        ("+375291234567", "+375291234567"),
    ],
)
def test_phone_normalization(raw: str, expected: str) -> None:
    assert _normalize_phone(raw) == expected


def test_phone_country_for_official_fallback_form() -> None:
    assert _phone_country("+79991234567") == "ru"
    assert _phone_country("+375291234567") == "by"
    assert _phone_country("+37499123456") == "am"
    assert _phone_country("+12025550123") is None


@pytest.mark.parametrize("raw", ["", "123", "123456789", "+7 abc 123"])
def test_phone_or_code_rejects_invalid_values(raw: str) -> None:
    with pytest.raises(ValueError):
        if raw == "123":
            _normalize_code(raw)
        else:
            _normalize_phone(raw)


def test_code_and_feedback_helpers() -> None:
    assert _normalize_code(" 123456 ") == "123456"
    assert _login_feedback("Код введён неверно") == (
        "Код не подошёл. Введите новый код от Wildberries."
    )
    assert _contains_captcha("Подтвердите, что вы не робот") is True
    assert _connector_confirms_account("guest", "account", "") is True
    assert _connector_confirms_account("", "public", "") is False
    assert _connector_confirms_account("same", "same", "0") is False
    assert _connector_confirms_account("", "account", "123456") is True
    localized = _localized_connector_url(
        "https://card.wb.ru/cards/v4/detail?appType=1&curr=byn&dest=123&nm=42",
        currency="rub",
        language="ru",
        destination=-5827722,
    )
    assert "curr=rub" in localized
    assert "dest=-5827722" in localized
    assert "lang=ru" in localized
    assert "nm=42" in localized


def test_navigation_guard_does_not_accept_lookalike_domains() -> None:
    assert _allowed_wb_navigation("https://www.wildberries.ru/lk") is True
    assert _allowed_wb_navigation("https://www.wildberries.by/lk") is True
    assert _allowed_wb_navigation("https://id.wb.ru/") is True
    assert _allowed_wb_navigation("about:blank") is True
    assert _allowed_wb_navigation("https://wildberries.ru.evil.example/") is False
    assert _allowed_wb_navigation("https://wildberries.by.evil.example/") is False
    assert _allowed_wb_navigation("http://www.wildberries.ru/") is False
