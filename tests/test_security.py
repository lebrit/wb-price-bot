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
            ],
        }
    )
    normalized = json.loads(normalize_wb_session(raw))
    assert [item["name"] for item in normalized["cookies"]] == ["wb-session"]
    assert [item["origin"] for item in normalized["origins"]] == ["https://www.wildberries.ru"]


def test_session_without_browser_origin_is_rejected_by_default() -> None:
    raw = json.dumps(
        {
            "cookies": [{"name": "session", "value": "value", "domain": ".wildberries.ru"}],
            "origins": [],
        }
    )
    with pytest.raises(SessionFormatError, match="браузера"):
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
