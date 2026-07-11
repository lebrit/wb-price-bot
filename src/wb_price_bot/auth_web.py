from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import secrets
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import WSMsgType, web

from .config import Settings
from .database import Database, normalize_datetime
from .security import SessionCipher, SessionFormatError, normalize_wb_session
from .telegram_auth import TelegramInitDataError, validate_init_data

logger = logging.getLogger(__name__)

_WB_HOME = "https://www.wildberries.ru/"
_FRAME_INTERVAL_SECONDS = 0.7
_VIEWPORT = {"width": 430, "height": 780}


class AuthWebService:
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
        self._slots = asyncio.Semaphore(settings.auth_max_concurrent_sessions)
        self._active_session_ids: set[str] = set()
        self._active_lock = asyncio.Lock()

    def create_app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024)
        app.on_response_prepare.append(self._security_headers)
        app.add_routes(
            [
                web.get("/", self.index),
                web.get("/health", self.health),
                web.get("/login/{session_id}", self.login_page),
                web.get("/ws/{session_id}", self.websocket),
            ]
        )
        return app

    async def _security_headers(self, _: web.Request, response: web.StreamResponse) -> None:
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )

    async def index(self, _: web.Request) -> web.Response:
        return web.Response(
            text=(
                "WB Price Bot: окно авторизации открывается только одноразовой кнопкой "
                "из личного чата Telegram."
            ),
            content_type="text/plain",
        )

    async def health(self, _: web.Request) -> web.Response:
        await self.database.ping()
        return web.json_response(
            {
                "ok": True,
                "active": len(self._active_session_ids),
                "limit": self.settings.auth_max_concurrent_sessions,
            }
        )

    async def login_page(self, request: web.Request) -> web.Response:
        session_id = request.match_info["session_id"]
        auth_session = await self.database.get_auth_session(session_id)
        if auth_session is None:
            raise web.HTTPNotFound(text="Окно авторизации не найдено")
        expires_at = normalize_datetime(auth_session.expires_at)
        if expires_at is None or expires_at <= _utcnow():
            raise web.HTTPGone(text="Срок действия окна авторизации истёк")
        if auth_session.status != "pending":
            raise web.HTTPGone(text="Это одноразовое окно авторизации уже использовано")
        nonce = secrets.token_urlsafe(18)
        body = _login_html(session_id, nonce)
        response = web.Response(text=body, content_type="text/html")
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            f"script-src https://telegram.org 'nonce-{nonce}'; "
            f"style-src 'nonce-{nonce}'; "
            "img-src blob: data:; connect-src 'self' wss:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self' https://web.telegram.org"
        )
        return response

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        if not self._valid_origin(request.headers.get("Origin")):
            raise web.HTTPForbidden(text="Недопустимый Origin")
        session_id = request.match_info["session_id"]
        ws = web.WebSocketResponse(
            heartbeat=25,
            max_msg_size=64 * 1024,
            compress=False,
            autoclose=True,
        )
        await ws.prepare(request)
        try:
            first = await asyncio.wait_for(ws.receive(), timeout=20)
            if first.type is not WSMsgType.TEXT:
                await _send_error(ws, "Telegram не передал данные запуска")
                return ws
            payload = _json_object(first.data)
            if payload.get("type") != "init":
                await _send_error(ws, "Неверная команда запуска")
                return ws
            telegram_user = validate_init_data(
                str(payload.get("initData", "")), self.settings.telegram_token
            )
            if (
                self.settings.registration_mode == "allowlist"
                and telegram_user.id not in self.settings.allowed_users
            ):
                await _send_error(ws, "Доступ пользователя больше не разрешён")
                return ws
            auth_session = await self.database.get_auth_session(session_id, telegram_user.id)
            if auth_session is None:
                await _send_error(ws, "Окно входа создано для другого пользователя")
                return ws
            expires_at = normalize_datetime(auth_session.expires_at)
            if expires_at is None or expires_at <= _utcnow():
                await self.database.set_auth_session_status(
                    session_id, "expired", expected_statuses=("pending",)
                )
                await _send_error(ws, "Срок действия окна входа истёк")
                return ws
            if auth_session.status != "pending":
                await _send_error(ws, "Это одноразовое окно входа уже использовано")
                return ws
            async with self._active_lock:
                if session_id in self._active_session_ids:
                    await _send_error(ws, "Это окно входа уже открыто на другом устройстве")
                    return ws
                self._active_session_ids.add(session_id)
            if not await self.database.queue_auth_session(session_id, telegram_user.id):
                await _send_error(ws, "Окно входа отменено, истекло или заменено новым")
                return ws
            await ws.send_json({"type": "status", "text": "Ожидаю свободный браузер…"})
            remaining = max(1.0, (expires_at - _utcnow()).total_seconds())
            try:
                await asyncio.wait_for(self._slots.acquire(), timeout=remaining)
            except TimeoutError:
                await self.database.set_auth_session_status(
                    session_id, "expired", expected_statuses=("queued",)
                )
                await _send_error(ws, "Не удалось дождаться свободного браузера")
                return ws
            try:
                if not await self.database.activate_auth_session(session_id, telegram_user.id):
                    await _send_error(ws, "Окно входа отменено или истекло")
                    return ws
                await self._browser_session(ws, session_id, telegram_user.id, expires_at)
            finally:
                self._slots.release()
        except (TelegramInitDataError, ValueError) as exc:
            await _send_error(ws, str(exc))
        except TimeoutError:
            await _send_error(ws, "Время запуска окна истекло")
        except Exception as exc:
            logger.exception("Ошибка web-авторизации WB: %s", type(exc).__name__)
            await self.database.set_auth_session_status(
                session_id,
                "pending",
                f"Браузер можно открыть повторно: {type(exc).__name__}",
                expected_statuses=("queued", "active"),
            )
            await _send_error(
                ws,
                "Не удалось открыть защищённый браузер. Закройте окно и откройте эту кнопку снова.",
            )
        finally:
            current = await self.database.get_auth_session(session_id)
            if current is not None and current.status in {"active", "queued"}:
                current_expires = normalize_datetime(current.expires_at)
                await self.database.set_auth_session_status(
                    session_id,
                    "expired"
                    if current_expires is not None and current_expires <= _utcnow()
                    else "pending",
                    "Соединение закрыто; окно можно открыть повторно",
                    expected_statuses=("active", "queued"),
                )
            async with self._active_lock:
                self._active_session_ids.discard(session_id)
            if not ws.closed:
                await ws.close()
        return ws

    async def _browser_session(
        self,
        ws: web.WebSocketResponse,
        session_id: str,
        telegram_id: int,
        expires_at: datetime,
    ) -> None:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright не установлен") from exc

        await ws.send_json(
            {
                "type": "status",
                "text": "Браузер готовится. Вводите данные только на странице Wildberries.",
            }
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-sync",
                    "--disable-extensions",
                    "--no-first-run",
                ],
            )
            try:
                context = await browser.new_context(
                    locale="ru-RU",
                    viewport=cast(Any, _VIEWPORT),
                    java_script_enabled=True,
                )
                page = await context.new_page()

                async def guard_navigation(route: Any) -> None:
                    request = route.request
                    if (
                        request.is_navigation_request()
                        and request.frame == page.main_frame
                        and not _allowed_wb_navigation(request.url)
                    ):
                        await route.abort("blockedbyclient")
                        with contextlib.suppress(Exception):
                            await _send_error(
                                ws,
                                "Переход за пределы доменов Wildberries заблокирован",
                                fatal=False,
                            )
                        return
                    await route.continue_()

                await context.route("**/*", guard_navigation)
                auth_seen = asyncio.Event()
                inspection_tasks: set[asyncio.Task[None]] = set()

                async def inspect_response(response: Any) -> None:
                    with contextlib.suppress(Exception):
                        headers = await response.request.all_headers()
                        if headers.get("authorization", "").lower().startswith("bearer "):
                            auth_seen.set()

                def on_response(response: Any) -> None:
                    task = asyncio.create_task(inspect_response(response))
                    inspection_tasks.add(task)
                    task.add_done_callback(inspection_tasks.discard)

                page.on("response", on_response)
                await page.set_content(_browser_placeholder("Запускаю страницу Wildberries…"))
                frame_task = asyncio.create_task(
                    self._stream_frames(page, ws, session_id, telegram_id)
                )

                async def open_wildberries() -> bool:
                    await ws.send_json({"type": "status", "text": "Подключаюсь к Wildberries…"})
                    try:
                        await page.goto(_WB_HOME, wait_until="commit", timeout=25_000)
                    except PlaywrightTimeoutError:
                        with contextlib.suppress(Exception):
                            await page.evaluate("window.stop()")
                        with contextlib.suppress(Exception):
                            await page.set_content(
                                _browser_placeholder(
                                    "Wildberries не ответил. Нажмите ↻ для повторной попытки."
                                )
                            )
                        await _send_error(
                            ws,
                            "Wildberries не ответил вовремя. Нажмите ↻ для повторной попытки.",
                            fatal=False,
                        )
                        return False
                    with contextlib.suppress(PlaywrightTimeoutError):
                        await page.wait_for_load_state("domcontentloaded", timeout=5_000)
                    await ws.send_json(
                        {
                            "type": "status",
                            "text": "Откройте вход в Wildberries, введите телефон и код, затем нажмите «Сохранить вход».",
                        }
                    )
                    return True

                try:
                    await open_wildberries()
                    while not ws.closed:
                        remaining = max(0.0, (expires_at - _utcnow()).total_seconds())
                        if remaining <= 0:
                            await self.database.set_auth_session_status(
                                session_id,
                                "expired",
                                "Срок действия окна входа истёк",
                                expected_statuses=("active",),
                            )
                            await _send_error(ws, "Срок действия окна входа истёк")
                            return
                        message = await asyncio.wait_for(ws.receive(), timeout=remaining)
                        if message.type is WSMsgType.TEXT:
                            command = _json_object(message.data)
                            action = str(command.get("type", ""))
                            current = await self.database.get_auth_session(session_id, telegram_id)
                            if current is None or current.status != "active":
                                await _send_error(ws, "Это окно входа отменено или заменено новым")
                                return
                            if action == "reload":
                                await open_wildberries()
                                continue
                            if action == "complete":
                                await ws.send_json(
                                    {"type": "status", "text": "Проверяю вход Wildberries…"}
                                )
                                if not auth_seen.is_set():
                                    with contextlib.suppress(PlaywrightTimeoutError):
                                        await page.reload(wait_until="commit", timeout=20_000)
                                with contextlib.suppress(TimeoutError):
                                    await asyncio.wait_for(auth_seen.wait(), timeout=12)
                                if not auth_seen.is_set():
                                    await _send_error(
                                        ws,
                                        "Авторизованный запрос WB не обнаружен. Завершите вход и повторите.",
                                        fatal=False,
                                    )
                                    continue
                                try:
                                    await page.evaluate(
                                        "localStorage.setItem('_wb_price_bot_web_auth', '1')"
                                    )
                                    try:
                                        state = await context.storage_state(indexed_db=True)
                                    except TypeError:
                                        state = await context.storage_state()
                                    normalized = normalize_wb_session(
                                        json.dumps(state, ensure_ascii=False)
                                    )
                                except SessionFormatError as exc:
                                    await _send_error(ws, str(exc), fatal=False)
                                    continue
                                saved = await self.database.complete_auth_session(
                                    session_id,
                                    telegram_id,
                                    self.cipher.encrypt(normalized),
                                )
                                if not saved:
                                    await _send_error(
                                        ws, "Это окно входа отменено, истекло или заменено новым"
                                    )
                                    return
                                await ws.send_json(
                                    {
                                        "type": "success",
                                        "text": "Аккаунт Wildberries подключён. Окно можно закрыть.",
                                    }
                                )
                                with contextlib.suppress(Exception):
                                    await self.bot.send_message(
                                        telegram_id,
                                        "✅ <b>Аккаунт Wildberries подключён</b>\n\n"
                                        "Сессия зашифрована и привязана к вашему Telegram ID. "
                                        "Проверьте её кнопкой «🧪 Проверить цену».",
                                    )
                                await asyncio.sleep(1)
                                return
                            if action == "cancel":
                                await self.database.set_auth_session_status(
                                    session_id,
                                    "cancelled",
                                    "Отменено пользователем",
                                    expected_statuses=("active",),
                                )
                                return
                            await _apply_browser_command(page, command)
                        elif message.type in {
                            WSMsgType.CLOSE,
                            WSMsgType.CLOSED,
                            WSMsgType.CLOSING,
                            WSMsgType.ERROR,
                        }:
                            return
                finally:
                    frame_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await frame_task
                    page.remove_listener("response", on_response)
                    for task in inspection_tasks:
                        task.cancel()
                    if inspection_tasks:
                        await asyncio.gather(*inspection_tasks, return_exceptions=True)
                    await context.close()
            finally:
                await browser.close()

    async def _stream_frames(
        self,
        page: Any,
        ws: web.WebSocketResponse,
        session_id: str,
        telegram_id: int,
    ) -> None:
        frame_number = 0
        last_host = ""
        while not ws.closed:
            if frame_number % 3 == 0:
                current = await self.database.get_auth_session(session_id, telegram_id)
                if current is None or current.status != "active":
                    if current is None or current.status != "succeeded":
                        await _send_error(ws, "Окно входа отменено или завершено")
                        await ws.close()
                    return
            try:
                host = urlparse(page.url).hostname or "Wildberries"
                if host != last_host:
                    await ws.send_json({"type": "address", "text": f"🔒 {host}"})
                    last_host = host
                frame = await page.screenshot(type="jpeg", quality=65, animations="disabled")
                await ws.send_bytes(frame)
            except Exception:
                if not ws.closed:
                    logger.debug("Не удалось отправить кадр web-авторизации", exc_info=True)
                    await asyncio.sleep(_FRAME_INTERVAL_SECONDS)
                    continue
                return
            frame_number += 1
            await asyncio.sleep(_FRAME_INTERVAL_SECONDS)

    def _valid_origin(self, origin: str | None) -> bool:
        if not origin or not self.settings.auth_public_url:
            return False
        expected = urlparse(self.settings.auth_public_url)
        actual = urlparse(origin)
        return (
            actual.scheme == expected.scheme
            and actual.hostname == expected.hostname
            and actual.port == expected.port
        )


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
    service = AuthWebService(settings=settings, database=database, cipher=cipher, bot=bot)
    runner = web.AppRunner(service.create_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.auth_bind_host, settings.auth_port)
    await site.start()
    logger.info("Web-авторизация запущена на %s:%s", settings.auth_bind_host, settings.auth_port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await bot.session.close()
        await database.close()


async def _apply_browser_command(page: Any, command: dict[str, Any]) -> None:
    action = str(command.get("type", ""))
    if action in {"click", "down", "up", "move"}:
        x = _coordinate(command.get("x"), _VIEWPORT["width"])
        y = _coordinate(command.get("y"), _VIEWPORT["height"])
        if action == "click":
            await page.mouse.click(x, y)
        elif action == "down":
            await page.mouse.move(x, y)
            await page.mouse.down()
        elif action == "up":
            await page.mouse.move(x, y)
            await page.mouse.up()
        else:
            await page.mouse.move(x, y)
        return
    if action == "wheel":
        await page.mouse.wheel(0, max(-1500, min(1500, int(command.get("dy", 0)))))
    elif action == "text":
        value = str(command.get("value", ""))[:512]
        if value:
            await page.keyboard.insert_text(value)
    elif action == "key":
        key = str(command.get("key", ""))
        if key in {"Backspace", "Delete", "Enter", "Tab", "Escape", "ArrowUp", "ArrowDown"}:
            await page.keyboard.press(key)
    elif action == "reload":
        await page.reload(wait_until="domcontentloaded", timeout=60_000)
    elif action == "back":
        await page.go_back(wait_until="domcontentloaded", timeout=60_000)


def _coordinate(value: Any, maximum: int) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректная координата") from exc
    return max(0.0, min(float(maximum), result))


def _allowed_wb_navigation(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme == "about" and parsed.path == "blank":
        return True
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    hostname = parsed.hostname.lower()
    return hostname in {"wildberries.ru", "wb.ru"} or hostname.endswith(
        (".wildberries.ru", ".wb.ru")
    )


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Неверный JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Ожидался JSON-объект")
    return value


async def _send_error(ws: web.WebSocketResponse, message: str, *, fatal: bool = True) -> None:
    if not ws.closed:
        await ws.send_json({"type": "error", "text": message[:500], "fatal": fatal})


def _utcnow() -> datetime:
    from .domain import utcnow

    return utcnow()


def _browser_placeholder(message: str) -> str:
    safe_message = html.escape(message)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    html,body {{ height:100%; margin:0; }}
    body {{ display:grid; place-items:center; padding:24px; box-sizing:border-box;
            background:#fff; color:#475569; font:16px system-ui,sans-serif; text-align:center; }}
  </style>
</head>
<body>{safe_message}</body>
</html>"""


def _login_html(session_id: str, nonce: str) -> str:
    safe_session = html.escape(session_id, quote=True)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
  <title>Вход в Wildberries</title>
  <script nonce="{nonce}" src="https://telegram.org/js/telegram-web-app.js"></script>
  <style nonce="{nonce}">
    :root {{ color-scheme: dark; font-family: system-ui,-apple-system,sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; background:#10131a; color:#f7f8fa; }}
    main {{ min-height:100vh; display:flex; flex-direction:column; gap:8px; padding:10px; }}
    h1 {{ margin:0; font-size:18px; }}
    #status {{ min-height:38px; color:#cbd5e1; font-size:13px; }}
    #address {{ overflow:hidden; color:#86efac; font-size:12px; text-overflow:ellipsis; white-space:nowrap; }}
    #screen-wrap {{ flex:1; display:grid; place-items:center; overflow:hidden; border:1px solid #333b4d; border-radius:12px; background:#fff; touch-action:none; }}
    #placeholder {{ grid-area:1/1; color:#64748b; font-size:13px; }}
    #screen {{ grid-area:1/1; display:none; max-width:100%; max-height:calc(100vh - 230px); user-select:none; -webkit-user-drag:none; touch-action:none; }}
    #screen.ready {{ display:block; }}
    .row {{ display:flex; gap:6px; }}
    button,input {{ min-height:42px; border-radius:10px; border:1px solid #374151; font-size:14px; }}
    button {{ background:#252b38; color:#fff; padding:8px 12px; }}
    button.primary {{ background:#7c3aed; border-color:#8b5cf6; flex:1; }}
    button.danger {{ color:#fecaca; }}
    input {{ min-width:0; flex:1; padding:8px 10px; background:#171b24; color:#fff; }}
    small {{ color:#94a3b8; line-height:1.3; }}
  </style>
</head>
<body>
<main>
  <h1>🔐 Вход в Wildberries</h1>
  <div id="address">🔒 wildberries.ru</div>
  <div id="status">Подключаю защищённый браузер…</div>
  <div id="screen-wrap"><div id="placeholder">Окно Wildberries появится здесь</div><img id="screen" alt=""></div>
  <div class="row">
    <button id="back">←</button><button id="reload">↻</button>
    <button id="scrollup">▲</button><button id="scrolldown">▼</button>
    <button id="backspace">⌫</button><button id="enter">Enter</button>
  </div>
  <div class="row">
    <input id="text" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Текст для выбранного поля">
    <button id="type">Ввести</button>
  </div>
  <div class="row"><button class="primary" id="complete">Сохранить вход</button><button class="danger" id="cancel">Отмена</button></div>
  <small>Телефон, код и CAPTCHA вводятся в изолированном окне WB. Кадры и введённый текст не записываются в журнал.</small>
</main>
<script nonce="{nonce}">
(() => {{
  const tg = window.Telegram?.WebApp;
  tg?.ready(); tg?.expand();
  const status = document.getElementById('status');
  const address = document.getElementById('address');
  const screen = document.getElementById('screen');
  const placeholder = document.getElementById('placeholder');
  const input = document.getElementById('text');
  let lastUrl = null, dragging = false;
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${{scheme}}://${{location.host}}/ws/{safe_session}`);
  ws.binaryType = 'blob';
  const send = value => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(value));
  ws.onopen = () => send({{type:'init', initData: tg?.initData || ''}});
  ws.onmessage = event => {{
    if (typeof event.data !== 'string') {{
      if (lastUrl) URL.revokeObjectURL(lastUrl);
      lastUrl = URL.createObjectURL(event.data); screen.src = lastUrl; screen.classList.add('ready'); placeholder.hidden=true; return;
    }}
    const data = JSON.parse(event.data);
    if (data.type === 'address') {{ address.textContent=data.text || ''; return; }}
    status.textContent = data.text || '';
    if (data.type === 'error') status.style.color = '#fca5a5';
    if (data.type === 'success') {{ status.style.color='#86efac'; setTimeout(() => tg?.close(), 1800); }}
  }};
  ws.onerror = () => {{ status.textContent='Не удалось подключиться к серверу авторизации'; status.style.color='#fca5a5'; }};
  const point = event => {{
    const r=screen.getBoundingClientRect();
    return {{x:(event.clientX-r.left)*430/r.width, y:(event.clientY-r.top)*780/r.height}};
  }};
  screen.addEventListener('pointerdown', e => {{ dragging=true; screen.setPointerCapture(e.pointerId); send({{type:'down',...point(e)}}); }});
  screen.addEventListener('pointermove', e => {{ if(dragging) send({{type:'move',...point(e)}}); }});
  screen.addEventListener('pointerup', e => {{ send({{type:'up',...point(e)}}); dragging=false; }});
  screen.addEventListener('wheel', e => {{ e.preventDefault(); send({{type:'wheel',dy:e.deltaY}}); }}, {{passive:false}});
  document.getElementById('type').onclick=()=>{{ send({{type:'text',value:input.value}}); input.value=''; }};
  input.addEventListener('keydown', e=>{{ if(e.key==='Enter') document.getElementById('type').click(); }});
  document.getElementById('backspace').onclick=()=>send({{type:'key',key:'Backspace'}});
  document.getElementById('enter').onclick=()=>send({{type:'key',key:'Enter'}});
  document.getElementById('reload').onclick=()=>send({{type:'reload'}});
  document.getElementById('back').onclick=()=>send({{type:'back'}});
  document.getElementById('scrollup').onclick=()=>send({{type:'wheel',dy:-600}});
  document.getElementById('scrolldown').onclick=()=>send({{type:'wheel',dy:600}});
  document.getElementById('complete').onclick=()=>send({{type:'complete'}});
  document.getElementById('cancel').onclick=()=>{{ send({{type:'cancel'}}); tg?.close(); }};
}})();
</script>
</body></html>"""
