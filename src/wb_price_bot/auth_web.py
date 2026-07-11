from __future__ import annotations

import asyncio
import contextlib
import html
import io
import logging
import secrets
import time
import zipfile
from collections import defaultdict, deque
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

from .config import Settings
from .database import Database, auth_pairing_code, normalize_datetime
from .security import SessionCipher, SessionFormatError, normalize_wb_session

logger = logging.getLogger(__name__)


class ConnectorWebService:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        cipher: SessionCipher,
        bot: Bot,
    ) -> None:
        self.settings = settings
        self.database = database
        self.cipher = cipher
        self.bot = bot
        self._attempts: defaultdict[str, deque[float]] = defaultdict(deque)

    def create_app(self) -> web.Application:
        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.on_response_prepare.append(self._security_headers)
        app.add_routes(
            [
                web.get("/", self.index),
                web.get("/health", self.health),
                web.get("/connect/{session_id}", self.connect_page),
                web.get("/login/{session_id}", self.connect_page),
                web.get("/extension/wb-price-bot-connector.zip", self.download_extension),
                web.options("/api/connector", self.connector_options),
                web.post("/api/connector", self.connect_account),
            ]
        )
        return app

    async def _security_headers(self, _: web.Request, response: web.StreamResponse) -> None:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )

    async def index(self, _: web.Request) -> web.Response:
        return web.Response(
            text="WB Price Bot Connector: создайте одноразовый код командой /account в Telegram.",
            content_type="text/plain",
        )

    async def health(self, _: web.Request) -> web.Response:
        await self.database.ping()
        access = await self.database.admin_access_stats()
        return web.json_response(
            {
                "ok": True,
                "mode": "browser-extension",
                "active": access.auth_active,
                "pending": access.auth_pending,
            }
        )

    async def connect_page(self, request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        auth_session = await self.database.get_auth_session(session_id)
        if auth_session is None:
            raise web.HTTPNotFound(text="Код подключения не найден")
        expires_at = normalize_datetime(auth_session.expires_at)
        if expires_at is None or expires_at <= _utcnow():
            raise web.HTTPGone(text="Срок действия кода подключения истёк")
        if auth_session.status != "pending":
            raise web.HTTPGone(text="Этот код подключения уже использован")
        code = auth_pairing_code(auth_session.id)
        nonce = secrets.token_urlsafe(18)
        response = web.Response(
            text=_connector_html(code, nonce),
            content_type="text/html",
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            f"style-src 'nonce-{nonce}'; "
            "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        return response

    async def download_extension(self, _: web.Request) -> web.Response:
        archive = io.BytesIO()
        extension_root = resources.files("wb_price_bot").joinpath("extension")
        if not extension_root.is_dir():
            extension_root = Path(__file__).resolve().parents[2] / "extension"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for item in extension_root.iterdir():
                if item.is_file():
                    bundle.writestr(item.name, item.read_bytes())
        return web.Response(
            body=archive.getvalue(),
            content_type="application/zip",
            headers={"Content-Disposition": ('attachment; filename="wb-price-bot-connector.zip"')},
        )

    async def connector_options(self, request: web.Request) -> web.Response:
        response = web.Response(status=204)
        _set_connector_cors(request, response)
        return response

    async def connect_account(self, request: web.Request) -> web.Response:
        if not self._allow_attempt(request):
            return _json_error(request, "Слишком много попыток. Подождите одну минуту.", 429)
        try:
            payload: Any = await request.json()
        except Exception:
            return _json_error(request, "Неверный формат запроса", 400)
        if not isinstance(payload, dict):
            return _json_error(request, "Неверный формат запроса", 400)
        code = str(payload.get("code", "")).strip().upper()
        session_payload = payload.get("session")
        try:
            normalized = normalize_wb_session(
                _compact_json(session_payload),
                require_auth_marker=True,
                require_connector=True,
            )
        except SessionFormatError as exc:
            return _json_error(request, str(exc), 400)
        activated = await self.database.activate_connector_session(code)
        if activated is None:
            return _json_error(request, "Код неверен, истёк или уже использован", 404)
        session_id, telegram_id = activated
        saved = await self.database.complete_auth_session(
            session_id,
            telegram_id,
            self.cipher.encrypt(normalized),
        )
        if not saved:
            await self.database.set_auth_session_status(
                session_id,
                "failed",
                "Не удалось атомарно сохранить сессию расширения",
                expected_statuses=("active",),
            )
            return _json_error(request, "Код подключения уже использован", 409)
        with contextlib.suppress(Exception):
            await self.bot.send_message(
                telegram_id,
                "✅ <b>Аккаунт Wildberries подключён</b>\n\n"
                "Сессия передана расширением, зашифрована и привязана к вашему Telegram ID. "
                "Телефон и SMS-код бот не получал.",
            )
        response = web.json_response(
            {"ok": True, "message": "Аккаунт Wildberries подключён. Вернитесь в Telegram."}
        )
        _set_connector_cors(request, response)
        return response

    def _allow_attempt(self, request: web.Request) -> bool:
        forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        key = forwarded or request.remote or "unknown"
        now = time.monotonic()
        attempts = self._attempts[key]
        while attempts and attempts[0] < now - 60:
            attempts.popleft()
        if len(attempts) >= 12:
            return False
        attempts.append(now)
        return True


def _compact_json(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _set_connector_cors(request: web.Request, response: web.StreamResponse) -> None:
    origin = request.headers.get("Origin", "")
    if origin.startswith("chrome-extension://"):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"


def _json_error(request: web.Request, message: str, status: int) -> web.Response:
    response = web.json_response({"ok": False, "message": message[:500]}, status=status)
    _set_connector_cors(request, response)
    return response


def _connector_html(code: str, nonce: str) -> str:
    safe_code = html.escape(code)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Подключение Wildberries</title>
  <style nonce="{nonce}">
    :root {{ color-scheme:light dark; font-family:system-ui,-apple-system,sans-serif; }}
    body {{ margin:0; background:#111827; color:#f9fafb; }}
    main {{ max-width:620px; margin:auto; padding:28px 18px 48px; }}
    .card {{ background:#1f2937; border:1px solid #374151; border-radius:18px; padding:22px; }}
    h1 {{ font-size:24px; margin-top:0; }}
    code {{ display:block; margin:20px 0; padding:18px; border-radius:12px; background:#0f172a;
            color:#c4b5fd; font-size:30px; font-weight:800; letter-spacing:4px; text-align:center; }}
    a {{ display:block; padding:13px 16px; border-radius:11px; background:#7c3aed; color:white;
         text-align:center; text-decoration:none; font-weight:700; }}
    li {{ margin:10px 0; line-height:1.45; }}
    .muted {{ color:#cbd5e1; line-height:1.45; }}
  </style>
</head>
<body><main><div class="card">
  <h1>Подключение аккаунта WB</h1>
  <p class="muted">Вход выполняется только на настоящем сайте Wildberries в вашем браузере.</p>
  <code>{safe_code}</code>
  <ol>
    <li>Скачайте и распакуйте расширение.</li>
    <li>Откройте <b>chrome://extensions</b> (Edge: <b>edge://extensions</b>),
        включите режим разработчика и нажмите
        «Загрузить распакованное расширение».</li>
    <li>Войдите на wildberries.ru обычным способом и обновите страницу.</li>
    <li>Откройте WB Price Bot Connector, укажите адрес этого сервера и код выше.</li>
  </ol>
  <a href="/extension/wb-price-bot-connector.zip">Скачать WB Price Bot Connector</a>
  <p class="muted">Код действует один раз. Расширение не получает телефон или SMS-код.</p>
</div></main></body>
</html>"""


async def run_auth_server(settings: Settings) -> None:
    if not settings.auth_enabled:
        raise RuntimeError("AUTH_PUBLIC_URL не настроен")
    database = Database(settings.database_path)
    await database.initialize()
    cipher = SessionCipher(settings.session_encryption_key)
    bot = Bot(
        settings.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    service = ConnectorWebService(
        settings=settings,
        database=database,
        cipher=cipher,
        bot=bot,
    )
    runner = web.AppRunner(service.create_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.auth_bind_host, settings.auth_port)
    await site.start()
    logger.info("WB Connector API запущен на %s:%s", settings.auth_bind_host, settings.auth_port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await bot.session.close()
        await database.close()


def _utcnow() -> datetime:
    from .domain import utcnow

    return utcnow()
