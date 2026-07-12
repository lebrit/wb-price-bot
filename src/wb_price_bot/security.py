from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

_COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_MAX_SESSION_SIZE = 2 * 1024 * 1024
_CONNECTOR_HEADERS = {
    "authorization",
    "x-client-version",
    "x-queryid",
    "x-spa-version",
    "x-userid",
}


class SessionFormatError(ValueError):
    """The imported browser session is malformed or does not look authenticated."""


class SessionCipher:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("SESSION_ENCRYPTION_KEY не является ключом Fernet") from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc:
            raise ValueError("Не удалось расшифровать сессию Wildberries") from exc


def _allowed_cookie_domain(domain: str) -> bool:
    normalized = domain.lstrip(".").lower()
    return (
        normalized == "wb.ru"
        or normalized.endswith(".wb.ru")
        or normalized == "wildberries.ru"
        or normalized.endswith(".wildberries.ru")
    )


def _allowed_origin(origin: str) -> bool:
    return origin in {
        "https://www.wildberries.ru",
        "https://wildberries.ru",
        "https://global.wildberries.ru",
        "https://id.wb.ru",
    }


def _sanitize_storage_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("cookies"), list):
        raise SessionFormatError("Нужен JSON storage_state из браузерного помощника")
    cookies: list[dict[str, Any]] = []
    for item in payload["cookies"]:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain", "")).strip().lower()
        name = str(item.get("name", "")).strip()
        value = str(item.get("value", ""))
        if not _allowed_cookie_domain(domain) or not name or not value:
            continue
        if not _COOKIE_NAME_RE.fullmatch(name) or any(char in value for char in "\r\n;"):
            raise SessionFormatError("Cookie содержит недопустимые символы")
        cleaned: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": str(item.get("path", "/")) or "/",
            "httpOnly": bool(item.get("httpOnly", False)),
            "secure": bool(item.get("secure", True)),
            "sameSite": str(item.get("sameSite", "Lax")),
        }
        expires = item.get("expires")
        if isinstance(expires, int | float):
            cleaned["expires"] = expires
        cookies.append(cleaned)

    origins: list[dict[str, Any]] = []
    raw_origins = payload.get("origins", [])
    if isinstance(raw_origins, list):
        for origin_data in raw_origins:
            if not isinstance(origin_data, dict):
                continue
            origin = str(origin_data.get("origin", ""))
            if _allowed_origin(origin):
                origins.append(origin_data)
    connector = _sanitize_connector(payload.get("connector"))
    if not cookies:
        raise SessionFormatError("В файле нет cookies Wildberries/WB ID")
    result: dict[str, Any] = {"cookies": cookies, "origins": origins}
    if connector is not None:
        result["connector"] = connector
    return result


def _sanitize_connector(payload: Any) -> dict[str, Any] | None:
    if payload is None:
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise SessionFormatError("Расширение передало неизвестный формат подключения")
    card_url = str(payload.get("cardUrl", ""))
    parsed = urlparse(card_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "card.wb.ru"
        or parsed.path != "/cards/v4/detail"
    ):
        raise SessionFormatError("Расширение не передало безопасный адрес WB card API")
    raw_headers = payload.get("headers")
    if not isinstance(raw_headers, dict):
        raise SessionFormatError("Расширение не передало заголовки авторизации WB")
    headers: dict[str, str] = {}
    for raw_name, raw_value in raw_headers.items():
        name = str(raw_name).strip().lower()
        value = str(raw_value).strip()
        if name not in _CONNECTOR_HEADERS or not value:
            continue
        if len(value) > 4096 or any(char in value for char in "\r\n\x00"):
            raise SessionFormatError("Заголовок авторизации WB имеет неверный формат")
        headers[name] = value
    authorization = headers.get("authorization", "")
    if not authorization.lower().startswith("bearer ") or len(authorization) < 20:
        raise SessionFormatError(
            "Авторизация WB не обнаружена. Обновите страницу Wildberries и повторите."
        )
    captured_at = str(payload.get("capturedAt", ""))[:64]
    return {
        "version": 1,
        "cardUrl": card_url,
        "headers": headers,
        "capturedAt": captured_at,
    }


def normalize_wb_session(
    raw: str, *, require_auth_marker: bool = True, require_connector: bool = False
) -> str:
    """Convert Playwright storage state or a legacy Cookie header to canonical JSON."""
    value = raw.strip()
    if not value:
        raise SessionFormatError("Сессия пуста")
    if len(value.encode("utf-8")) > _MAX_SESSION_SIZE:
        raise SessionFormatError("Сессия слишком большая")
    if "\x00" in value or "\r" in value:
        raise SessionFormatError("Сессия содержит недопустимые символы")

    state: dict[str, Any]
    if value.startswith("[") or value.startswith("{"):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SessionFormatError("Не удалось разобрать JSON сессии") from exc
        if isinstance(payload, list):
            payload = {"cookies": payload, "origins": []}
        state = _sanitize_storage_state(payload)
    else:
        if value.lower().startswith("cookie:"):
            value = value.split(":", 1)[1].strip()
        cookies: list[dict[str, Any]] = []
        for chunk in value.replace("\n", ";").split(";"):
            if "=" not in chunk:
                continue
            name, cookie_value = chunk.split("=", 1)
            name = name.strip()
            cookie_value = cookie_value.strip()
            if not name or not cookie_value:
                continue
            if not _COOKIE_NAME_RE.fullmatch(name) or any(char in cookie_value for char in "\r\n;"):
                raise SessionFormatError("Cookie содержит недопустимые символы")
            cookies.append(
                {
                    "name": name,
                    "value": cookie_value,
                    "domain": ".wildberries.ru",
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
        state = {"cookies": cookies, "origins": []}

    if not state["cookies"]:
        raise SessionFormatError("В сессии не найдены cookies")
    if require_auth_marker and not state.get("origins"):
        raise SessionFormatError(
            "В браузерной сессии нет локальных данных Wildberries; завершите вход и повторите"
        )
    if require_connector and not state.get("connector"):
        raise SessionFormatError("WB не выдал данные для лёгкой проверки персональной цены")
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
