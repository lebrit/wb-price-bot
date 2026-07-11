from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wb_price_bot.auth_web import ConnectorWebService
from wb_price_bot.config import Settings
from wb_price_bot.database import Database, auth_pairing_code
from wb_price_bot.security import SessionCipher


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, telegram_id: int, text: str, **__: Any) -> None:
        self.messages.append((telegram_id, text))


def connector_session() -> dict[str, Any]:
    return {
        "cookies": [
            {
                "name": "wb-session",
                "value": "secret-cookie",
                "domain": ".wildberries.ru",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [
            {
                "origin": "https://www.wildberries.ru",
                "localStorage": [{"name": "wbx-validation-key", "value": "opaque"}],
            }
        ],
        "connector": {
            "version": 1,
            "cardUrl": ("https://card.wb.ru/cards/v4/detail?appType=1&curr=rub&dest=-5827722&nm=1"),
            "headers": {"authorization": "Bearer very-secret-connector-token"},
            "capturedAt": "2026-07-12T00:00:00Z",
        },
    }


@pytest.mark.asyncio
async def test_connector_page_download_and_one_time_pairing(settings: Settings) -> None:
    configured = replace(settings, auth_public_url="https://auth.example.com")
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, "owner", "Owner", is_admin=True)
    auth_session = await database.create_auth_session(1001, 600)
    code = auth_pairing_code(auth_session.id)
    bot = FakeBot()
    service = ConnectorWebService(
        settings=configured,
        database=database,
        cipher=SessionCipher(settings.session_encryption_key),
        bot=bot,  # type: ignore[arg-type]
    )
    client = TestClient(TestServer(service.create_app()))
    await client.start_server()
    try:
        health = await client.get("/health")
        assert health.status == 200
        assert (await health.json())["mode"] == "browser-extension"

        page = await client.get(f"/connect/{auth_session.id}")
        body = await page.text()
        assert page.status == 200
        assert code in body
        assert "WB Price Bot Connector" in body
        assert page.headers["X-Frame-Options"] == "DENY"

        extension = await client.get("/extension/wb-price-bot-connector.zip")
        with zipfile.ZipFile(io.BytesIO(await extension.read())) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["manifest_version"] == 3
            assert "service-worker.js" in archive.namelist()

        response = await client.post(
            "/api/connector",
            json={"code": code, "session": connector_session()},
            headers={"Origin": "chrome-extension://abcdefghijklmnop"},
        )
        assert response.status == 200
        assert (await response.json())["ok"] is True
        assert response.headers["Access-Control-Allow-Origin"].startswith("chrome-extension://")
        account = await database.get_wb_account(1001)
        assert account is not None
        assert "secret-cookie" not in account.encrypted_session
        assert bot.messages and bot.messages[0][0] == 1001

        repeated = await client.post(
            "/api/connector", json={"code": code, "session": connector_session()}
        )
        assert repeated.status == 404
        used_page = await client.get(f"/connect/{auth_session.id}")
        assert used_page.status == 410
    finally:
        await client.close()
        await database.close()


@pytest.mark.asyncio
async def test_connector_rejects_missing_bearer_without_consuming_code(
    settings: Settings,
) -> None:
    configured = replace(settings, auth_public_url="https://auth.example.com")
    database = Database(settings.database_path)
    await database.initialize()
    await database.ensure_user(1001, "owner", "Owner", is_admin=True)
    auth_session = await database.create_auth_session(1001, 600)
    code = auth_pairing_code(auth_session.id)
    service = ConnectorWebService(
        settings=configured,
        database=database,
        cipher=SessionCipher(settings.session_encryption_key),
        bot=FakeBot(),  # type: ignore[arg-type]
    )
    client = TestClient(TestServer(service.create_app()))
    await client.start_server()
    try:
        payload = connector_session()
        payload["connector"]["headers"] = {}
        response = await client.post("/api/connector", json={"code": code, "session": payload})
        assert response.status == 400
        stored = await database.get_auth_session(auth_session.id, 1001)
        assert stored is not None and stored.status == "pending"
    finally:
        await client.close()
        await database.close()
