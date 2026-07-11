from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from wb_price_bot.auth_web import (
    AuthWebService,
    _allowed_wb_navigation,
    _browser_placeholder,
)
from wb_price_bot.config import Settings
from wb_price_bot.database import Database
from wb_price_bot.security import SessionCipher

from .test_telegram_auth import signed_init_data


class FakeBot:
    async def send_message(self, *_: Any, **__: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_auth_web_health_and_one_time_login_page(settings: Settings) -> None:
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
        health = await client.get("/health")
        assert health.status == 200
        assert (await health.json())["limit"] == 2

        page = await client.get(f"/login/{auth_session.id}")
        body = await page.text()
        assert page.status == 200
        assert "telegram-web-app.js" in body
        assert "Сохранить вход" in body
        assert "no-store" in page.headers["Cache-Control"]
        assert "https://web.telegram.org" in page.headers["Content-Security-Policy"]
        assert "X-Frame-Options" not in page.headers

        with pytest.raises(WSServerHandshakeError) as rejected:
            await client.ws_connect(
                f"/ws/{auth_session.id}", headers={"Origin": "https://evil.example.com"}
            )
        assert rejected.value.status == 403

        socket = await client.ws_connect(
            f"/ws/{auth_session.id}", headers={"Origin": "https://auth.example.com"}
        )
        await socket.send_json({"type": "init", "initData": ""})
        assert (await socket.receive_json())["type"] == "error"
        await socket.close()
    finally:
        await client.close()
        await database.close()

    assert service._valid_origin("https://auth.example.com") is True
    assert service._valid_origin("https://evil.example.com") is False


def test_remote_browser_only_allows_wb_top_level_navigation() -> None:
    assert _allowed_wb_navigation("https://www.wildberries.ru/") is True
    assert _allowed_wb_navigation("https://id.wb.ru/") is True
    assert _allowed_wb_navigation("https://wildberries.ru.evil.example/") is False
    assert _allowed_wb_navigation("http://www.wildberries.ru/") is False


@pytest.mark.asyncio
async def test_browser_failure_keeps_login_page_reusable(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
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

    async def fail_browser(*_: Any, **__: Any) -> None:
        raise RuntimeError("browser failed")

    monkeypatch.setattr(service, "_browser_session", fail_browser)
    client = TestClient(TestServer(service.create_app()))
    await client.start_server()
    try:
        socket = await client.ws_connect(
            f"/ws/{auth_session.id}", headers={"Origin": "https://auth.example.com"}
        )
        await socket.send_json({"type": "init", "initData": signed_init_data()})
        messages = []
        while True:
            message = await socket.receive_json()
            messages.append(message)
            if message["type"] == "error":
                break
        assert "откройте эту кнопку снова" in messages[-1]["text"]
        await socket.close()

        for _ in range(20):
            stored = await database.get_auth_session(auth_session.id, 1001)
            if stored is not None and stored.status == "pending":
                break
            await asyncio.sleep(0.01)
        assert stored is not None and stored.status == "pending"
        retry = await client.get(f"/login/{auth_session.id}")
        assert retry.status == 200
    finally:
        await client.close()
        await database.close()


def test_browser_placeholder_escapes_text() -> None:
    placeholder = _browser_placeholder("Запуск <WB>")
    assert "Запуск &lt;WB&gt;" in placeholder
