import json

import pytest
from cryptography.fernet import Fernet

from wb_price_bot.security import SessionCipher, SessionFormatError, normalize_wb_session


def test_session_cipher_round_trip() -> None:
    cipher = SessionCipher(Fernet.generate_key().decode("ascii"))
    encrypted = cipher.encrypt('{"secret":true}')
    assert "secret" not in encrypted
    assert cipher.decrypt(encrypted) == '{"secret":true}'


def test_normalize_playwright_state_filters_unrelated_domains() -> None:
    raw = json.dumps(
        {
            "cookies": [
                {
                    "name": "wb-session",
                    "value": "secret",
                    "domain": ".wildberries.ru",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "Lax",
                },
                {
                    "name": "evil",
                    "value": "leak",
                    "domain": ".example.com",
                    "path": "/",
                },
                {
                    "name": "wb-by-session",
                    "value": "secret-by",
                    "domain": ".wildberries.by",
                    "path": "/",
                },
            ],
            "origins": [
                {
                    "origin": "https://www.wildberries.ru",
                    "localStorage": [{"name": "token", "value": "opaque"}],
                },
                {
                    "origin": "https://example.com",
                    "localStorage": [{"name": "bad", "value": "leak"}],
                },
                {
                    "origin": "https://www.wildberries.by",
                    "localStorage": [{"name": "token-by", "value": "opaque-by"}],
                },
            ],
        }
    )
    normalized = json.loads(normalize_wb_session(raw))
    assert [item["name"] for item in normalized["cookies"]] == [
        "wb-session",
        "wb-by-session",
    ]
    assert [item["origin"] for item in normalized["origins"]] == [
        "https://www.wildberries.ru",
        "https://www.wildberries.by",
    ]


def test_session_without_browser_origin_is_rejected_by_default() -> None:
    raw = json.dumps(
        {
            "cookies": [{"name": "session", "value": "value", "domain": ".wildberries.ru"}],
            "origins": [],
        }
    )
    with pytest.raises(SessionFormatError, match="браузер"):
        normalize_wb_session(raw)


def test_legacy_cookie_can_be_normalized_only_when_explicitly_allowed() -> None:
    normalized = json.loads(
        normalize_wb_session("cookie: name=value; other=token", require_auth_marker=False)
    )
    assert len(normalized["cookies"]) == 2
    assert normalized["origins"] == []


def test_cookie_header_injection_is_rejected() -> None:
    with pytest.raises(SessionFormatError):
        normalize_wb_session("name=value\r\nX-Evil: yes", require_auth_marker=False)


def test_connector_metadata_is_filtered_and_can_be_required() -> None:
    raw = json.dumps(
        {
            "cookies": [{"name": "session", "value": "value", "domain": ".wildberries.ru"}],
            "origins": [
                {
                    "origin": "https://www.wildberries.ru",
                    "localStorage": [{"name": "token", "value": "opaque"}],
                }
            ],
            "connector": {
                "version": 1,
                "cardUrl": "https://card.wb.ru/cards/v4/detail?nm=1",
                "headers": {
                    "authorization": "Bearer connector-secret-token",
                    "x-userid": "123",
                    "evil-header": "leak",
                },
                "capturedAt": "2026-07-12T00:00:00Z",
                "storefrontOrigin": "https://www.wildberries.by",
            },
        }
    )
    normalized = json.loads(normalize_wb_session(raw, require_connector=True))
    assert normalized["connector"]["headers"] == {
        "authorization": "Bearer connector-secret-token",
        "x-userid": "123",
    }
    assert normalized["connector"]["storefrontOrigin"] == "https://www.wildberries.by"


def test_connector_rejects_non_wb_card_url() -> None:
    raw = json.dumps(
        {
            "cookies": [{"name": "session", "value": "value", "domain": ".wildberries.ru"}],
            "origins": [{"origin": "https://www.wildberries.ru", "localStorage": []}],
            "connector": {
                "version": 1,
                "cardUrl": "https://evil.example/cards/v4/detail",
                "headers": {"authorization": "Bearer connector-secret-token"},
            },
        }
    )
    with pytest.raises(SessionFormatError, match="безопасный адрес"):
        normalize_wb_session(raw, require_connector=True)
