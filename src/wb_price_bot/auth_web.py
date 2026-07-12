from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

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
_WB_FALLBACK_HOME = "https://www.wildberries.by/"
_VIEWPORT = {"width": 720, "height": 800}
_MAX_OTP_ATTEMPTS = 5
_PHONE_SELECTORS = (
    'input[autocomplete="tel"]',
    'input[type="tel"]',
    'input[inputmode="tel"]',
    'input[name*="phone" i]',
    'input[placeholder*="телефон" i]',
    'input[placeholder*="+7"]',
)
_CODE_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[name*="code" i]',
    '[data-testid*="code" i] input',
    'input[inputmode="numeric"][maxlength]:not([type="tel"]):not([autocomplete="tel"]):not([name*="phone" i])',
)
_LOGIN_TRIGGER_SELECTORS = (
    "a.j-main-login",
    "button.j-main-login",
    '[data-wba-header-name="Login"]',
    'a[href="/lk"]',
    'a[href*="/lk"]',
    'button:has-text("Войти")',
    'a:has-text("Войти")',
)
_SUBMIT_SELECTORS = (
    'button:has-text("Получить код")',
    'button:has-text("Продолжить")',
    'button:has-text("Подтвердить")',
    'button:has-text("Войти")',
)
_OTP_SUBMIT_SELECTORS = (
    'button:has-text("Подтвердить")',
    'button:has-text("Продолжить")',
    'button:has-text("Войти")',
)
_PROFILE_SELECTORS = (
    'a[href*="/lk/details"]',
    '[data-wba-header-name="Profile"]',
    '[class*="profile"] [class*="user"]',
)
_CAPTCHA_SELECTORS = (
    'iframe[src*="captcha" i]',
    '[class*="captcha" i]',
    '[id*="captcha" i]',
    '[data-testid*="captcha" i]',
)
_ALLOWED_CONNECTOR_HEADERS = {
    "authorization",
    "x-client-version",
    "x-queryid",
    "x-spa-version",
    "x-userid",
}


class CaptchaRequired(RuntimeError):
    """Wildberries requires an interactive CAPTCHA that this service will not solve."""


class LoginFlowError(RuntimeError):
    """The visible Wildberries login flow could not be completed safely."""


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
        # The target VPS is intentionally limited to one short-lived Chromium.
        self._slots = asyncio.Semaphore(1)
        self._active_session_ids: set[str] = set()
        self._active_lock = asyncio.Lock()
        self._code_requests: defaultdict[int, deque[float]] = defaultdict(deque)

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
                "WB Price Bot: одноразовая форма входа открывается только кнопкой "
                "из личного чата Telegram."
            ),
            content_type="text/plain",
        )

    async def health(self, _: web.Request) -> web.Response:
        await self.database.ping()
        access = await self.database.admin_access_stats()
        return web.json_response(
            {
                "ok": True,
                "mode": "telegram-form-browser",
                "active": access.auth_active,
                "pending": access.auth_pending,
                "limit": 1,
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
            raise web.HTTPGone(text="Это одноразовое окно авторизации уже используется")
        nonce = secrets.token_urlsafe(18)
        response = web.Response(
            text=_login_html(session_id, nonce),
            content_type="text/html",
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            f"script-src https://telegram.org 'nonce-{nonce}'; "
            f"style-src 'nonce-{nonce}'; "
            "connect-src 'self' wss:; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'self' https://web.telegram.org"
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
                await _send_error(ws, "Это одноразовое окно входа уже используется")
                return ws
            async with self._active_lock:
                if session_id in self._active_session_ids:
                    await _send_error(ws, "Это окно входа уже открыто на другом устройстве")
                    return ws
                self._active_session_ids.add(session_id)
            if not await self.database.queue_auth_session(session_id, telegram_user.id):
                await _send_error(ws, "Окно входа отменено, истекло или заменено новым")
                return ws
            phone = await self._wait_for_value(
                ws,
                session_id,
                telegram_user.id,
                expires_at,
                "phone",
                "Введите номер телефона, привязанный к Wildberries.",
                max_wait_seconds=90,
                allowed_statuses=("queued",),
            )
            if phone is None:
                return ws
            normalized_phone = _normalize_phone(phone)
            del phone
            if not self._allow_code_request(telegram_user.id):
                normalized_phone = ""
                await _send_error(
                    ws,
                    "Слишком частые запросы кода. Повторите вход позже.",
                )
                return ws
            await ws.send_json({"type": "status", "text": "Ожидаю свободный браузер…"})
            remaining = max(1.0, (expires_at - _utcnow()).total_seconds())
            try:
                await asyncio.wait_for(self._slots.acquire(), timeout=min(remaining, 90.0))
            except TimeoutError:
                await self.database.set_auth_session_status(
                    session_id,
                    "pending",
                    "Браузер занят; окно можно открыть повторно",
                    expected_statuses=("queued",),
                )
                normalized_phone = ""
                await _send_error(ws, "Браузер занят. Закройте окно и повторите через минуту.")
                return ws
            try:
                if not await self.database.activate_auth_session(session_id, telegram_user.id):
                    normalized_phone = ""
                    await _send_error(ws, "Окно входа отменено или истекло")
                    return ws
                await self._browser_session(
                    ws,
                    session_id,
                    telegram_user.id,
                    expires_at,
                    normalized_phone,
                )
                normalized_phone = ""
            finally:
                self._slots.release()
        except (TelegramInitDataError, ValueError) as exc:
            await _send_error(ws, str(exc))
        except TimeoutError:
            await _send_error(ws, "Время ожидания истекло")
        except Exception as exc:
            # Playwright error text may contain a recently filled value. Log only
            # the exception class so phone and OTP can never reach container logs.
            logger.error("Ошибка web-авторизации WB: %s", type(exc).__name__)
            await self.database.set_auth_session_status(
                session_id,
                "pending",
                f"Окно можно открыть повторно: {type(exc).__name__}",
                expected_statuses=("queued", "active"),
            )
            await _send_error(
                ws,
                "Не удалось открыть Wildberries. Закройте окно и попробуйте ещё раз.",
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
        phone: str,
    ) -> None:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright не установлен") from exc

        await ws.send_json({"type": "status", "text": "Запускаю одноразовый браузер Wildberries…"})
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-extensions",
                    "--disable-features=MediaRouter,Translate",
                    "--disable-sync",
                    "--no-first-run",
                    "--renderer-process-limit=2",
                ],
            )
            try:
                context = await browser.new_context(
                    locale="ru-RU",
                    viewport=cast(Any, _VIEWPORT),
                    java_script_enabled=True,
                    service_workers="block",
                )
                page = await context.new_page()
                page.set_default_timeout(5_000)
                page.set_default_navigation_timeout(30_000)
                inspection_tasks: set[asyncio.Task[None]] = set()

                def on_popup(popup: Any) -> None:
                    task = asyncio.create_task(popup.close())
                    inspection_tasks.add(task)
                    task.add_done_callback(inspection_tasks.discard)

                page.on("popup", on_popup)

                async def guard_navigation(route: Any) -> None:
                    browser_request = route.request
                    if browser_request.resource_type in {"font", "image", "media"}:
                        await route.abort("blockedbyclient")
                        return
                    if (
                        browser_request.is_navigation_request()
                        and browser_request.frame == page.main_frame
                        and not _allowed_wb_navigation(browser_request.url)
                    ):
                        await route.abort("blockedbyclient")
                        return
                    await route.continue_()

                await context.route("**/*", guard_navigation)
                connector: dict[str, Any] = {}
                baseline_bearer = ""
                baseline_frozen = False
                capture_enabled = False
                capture_nm_id: int | None = None
                card_seen = asyncio.Event()

                async def inspect_response(
                    response: Any,
                    *,
                    capture_response: bool,
                    expected_nm_id: int | None,
                    update_baseline: bool,
                ) -> None:
                    nonlocal baseline_bearer
                    with contextlib.suppress(Exception):
                        headers = await response.request.all_headers()
                        bearer = headers.get("authorization", "").strip()
                        parsed = urlparse(response.request.url)
                        is_card_request = (
                            parsed.hostname == "card.wb.ru" and parsed.path == "/cards/v4/detail"
                        )
                        if (
                            bearer.lower().startswith("bearer ")
                            and update_baseline
                            and is_card_request
                        ):
                            baseline_bearer = bearer
                        if not capture_response or not bearer.lower().startswith("bearer "):
                            return
                        if not is_card_request:
                            return
                        requested_ids = {
                            item
                            for value in parse_qs(parsed.query).get("nm", [])
                            for item in value.split(";")
                        }
                        if expected_nm_id is None or str(expected_nm_id) not in requested_ids:
                            return
                        safe_headers = {
                            name.lower(): value
                            for name, value in headers.items()
                            if name.lower() in _ALLOWED_CONNECTOR_HEADERS and value
                        }
                        if "authorization" not in safe_headers:
                            return
                        connector.clear()
                        connector.update(
                            {
                                "version": 1,
                                "cardUrl": response.request.url,
                                "headers": safe_headers,
                                "capturedAt": datetime.now(UTC).isoformat(),
                            }
                        )
                        card_seen.set()

                def on_response(response: Any) -> None:
                    task = asyncio.create_task(
                        inspect_response(
                            response,
                            capture_response=capture_enabled,
                            expected_nm_id=capture_nm_id,
                            update_baseline=not baseline_frozen,
                        )
                    )
                    inspection_tasks.add(task)
                    task.add_done_callback(inspection_tasks.discard)

                page.on("response", on_response)
                try:
                    try:
                        phone_input, storefront_origin = await _open_phone_form(
                            page, ws, PlaywrightTimeoutError
                        )
                        await ws.send_json(
                            {"type": "status", "text": "Запрашиваю код у Wildberries…"}
                        )
                        await _submit_phone(page, phone_input, phone)
                        phone = ""
                        code_fields = await _wait_for_code_form(page)
                        attempts = 0
                        while attempts < _MAX_OTP_ATTEMPTS:
                            code = await self._wait_for_value(
                                ws,
                                session_id,
                                telegram_id,
                                expires_at,
                                "code",
                                "Введите одноразовый код от Wildberries.",
                                max_wait_seconds=180,
                                allowed_statuses=("active",),
                            )
                            if code is None:
                                return
                            normalized_code = _normalize_code(code)
                            del code
                            attempts += 1
                            baseline_frozen = True
                            card_seen.clear()
                            connector.clear()
                            await ws.send_json(
                                {"type": "status", "text": "Проверяю код и сохраняю вход…"}
                            )
                            await _fill_code(page, code_fields, normalized_code)
                            normalized_code = ""
                            result = await _wait_for_login_result(page)
                            if result is not None:
                                await _send_error(ws, result, fatal=False)
                                code_fields = await _wait_for_code_form(page, max_wait_seconds=8.0)
                                continue
                            product = await self.database.get_first_product(telegram_id)
                            if product is None:
                                raise LoginFlowError(
                                    "Добавленный товар не найден. Вернитесь в бот и добавьте товар."
                                )
                            # Discard every card request made before confirmed OTP completion.
                            # Only a fresh request from the product navigation below may be saved.
                            capture_enabled = True
                            capture_nm_id = product.nm_id
                            card_seen.clear()
                            connector.clear()
                            await page.goto(
                                f"{storefront_origin}/catalog/{product.nm_id}/detail.aspx",
                                wait_until="domcontentloaded",
                                timeout=45_000,
                            )
                            try:
                                await asyncio.wait_for(card_seen.wait(), timeout=25)
                            except TimeoutError as exc:
                                raise LoginFlowError(
                                    "WB не подтвердил персональную сессию. Повторите вход позже."
                                ) from exc
                            current_bearer = str(
                                cast(dict[str, str], connector.get("headers", {})).get(
                                    "authorization", ""
                                )
                            )
                            user_marker = str(
                                cast(dict[str, str], connector.get("headers", {})).get(
                                    "x-userid", ""
                                )
                            )
                            if not _connector_confirms_account(
                                baseline_bearer,
                                current_bearer,
                                user_marker,
                            ):
                                raise LoginFlowError(
                                    "WB не выдал персональный токен после входа. Повторите позже."
                                )
                            user = await self.database.get_user(telegram_id)
                            destination = (
                                user.wb_destination
                                if user is not None and user.wb_destination is not None
                                else self.settings.wb_destination
                            )
                            connector["cardUrl"] = _localized_connector_url(
                                str(connector["cardUrl"]),
                                currency=self.settings.wb_currency,
                                language=self.settings.wb_language,
                                destination=destination,
                            )
                            connector["storefrontOrigin"] = storefront_origin
                            try:
                                raw_state = await context.storage_state(indexed_db=True)
                            except TypeError:
                                raw_state = await context.storage_state()
                            state: dict[str, Any] = dict(raw_state)
                            state["connector"] = connector
                            normalized = normalize_wb_session(
                                json.dumps(state, ensure_ascii=False),
                                require_connector=True,
                            )
                            saved = await self.database.complete_auth_session(
                                session_id,
                                telegram_id,
                                self.cipher.encrypt(normalized),
                            )
                            if not saved:
                                await _send_error(
                                    ws, "Окно входа отменено, истекло или заменено новым"
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
                            await asyncio.sleep(0.5)
                            return
                        await self.database.set_auth_session_status(
                            session_id,
                            "failed",
                            "Превышено число попыток ввода кода",
                            expected_statuses=("active",),
                        )
                        await _send_error(
                            ws, "Слишком много неверных попыток. Начните вход заново."
                        )
                    except CaptchaRequired:
                        await self.database.set_auth_session_status(
                            session_id,
                            "failed",
                            "Wildberries запросил CAPTCHA; обход не выполняется",
                            expected_statuses=("active",),
                        )
                        await _send_error(
                            ws,
                            "Wildberries запросил CAPTCHA. Бот её не обходит; повторите вход позже.",
                        )
                    except (LoginFlowError, SessionFormatError) as exc:
                        await _send_error(ws, str(exc))
                finally:
                    page.remove_listener("response", on_response)
                    page.remove_listener("popup", on_popup)
                    for task in inspection_tasks:
                        task.cancel()
                    if inspection_tasks:
                        await asyncio.gather(*inspection_tasks, return_exceptions=True)
                    await context.close()
            finally:
                await browser.close()

    async def _wait_for_value(
        self,
        ws: web.WebSocketResponse,
        session_id: str,
        telegram_id: int,
        expires_at: datetime,
        expected: str,
        prompt: str,
        *,
        max_wait_seconds: float,
        allowed_statuses: tuple[str, ...],
    ) -> str | None:
        await ws.send_json({"type": "step", "step": expected, "text": prompt})
        stage_deadline = time.monotonic() + max_wait_seconds
        while not ws.closed:
            session_remaining = max(0.0, (expires_at - _utcnow()).total_seconds())
            stage_remaining = max(0.0, stage_deadline - time.monotonic())
            if session_remaining <= 0:
                await self.database.set_auth_session_status(
                    session_id,
                    "expired",
                    "Срок действия окна входа истёк",
                    expected_statuses=allowed_statuses,
                )
                await _send_error(ws, "Срок действия окна входа истёк")
                return None
            if stage_remaining <= 0:
                await _send_error(
                    ws,
                    "Время ввода истекло. Закройте окно и откройте кнопку входа снова.",
                )
                return None
            remaining = min(session_remaining, stage_remaining)
            try:
                message = await asyncio.wait_for(ws.receive(), timeout=remaining)
            except TimeoutError:
                await _send_error(
                    ws,
                    "Время ввода истекло. Закройте окно и откройте кнопку входа снова.",
                )
                return None
            if message.type is not WSMsgType.TEXT:
                return None
            command = _json_object(message.data)
            action = str(command.get("type", ""))
            current = await self.database.get_auth_session(session_id, telegram_id)
            if current is None or current.status not in allowed_statuses:
                await _send_error(ws, "Это окно входа отменено или заменено новым")
                return None
            if action == "cancel":
                await self.database.set_auth_session_status(
                    session_id,
                    "cancelled",
                    "Отменено пользователем",
                    expected_statuses=allowed_statuses,
                )
                return None
            if action != expected:
                await _send_error(ws, "Дождитесь текущего шага входа", fatal=False)
                continue
            value = str(command.get("value", ""))
            try:
                if expected == "phone":
                    _normalize_phone(value)
                else:
                    _normalize_code(value)
            except ValueError as exc:
                await _send_error(ws, str(exc), fatal=False)
                await ws.send_json({"type": "step", "step": expected, "text": prompt})
                continue
            return value
        return None

    def _allow_code_request(self, telegram_id: int) -> bool:
        now = time.monotonic()
        attempts = self._code_requests[telegram_id]
        while attempts and attempts[0] < now - 3_600:
            attempts.popleft()
        if attempts and attempts[-1] > now - 60:
            return False
        if len(attempts) >= 3:
            return False
        attempts.append(now)
        return True

    def _valid_origin(self, origin: str | None) -> bool:
        if not origin or not self.settings.auth_public_url:
            return False
        try:
            actual = urlparse(origin)
            expected = urlparse(self.settings.auth_public_url)
        except ValueError:
            return False
        return (
            actual.scheme == expected.scheme
            and actual.hostname == expected.hostname
            and actual.port == expected.port
        )


async def _open_phone_form(
    page: Any, ws: web.WebSocketResponse, timeout_error: type[Exception]
) -> tuple[Any, str]:
    await ws.send_json({"type": "status", "text": "Открываю официальный сайт Wildberries…"})
    storefront_origin = ""
    blocked = False
    for home in (_WB_HOME, _WB_FALLBACK_HOME):
        try:
            response = await page.goto(home, wait_until="commit", timeout=20_000)
        except timeout_error:
            continue
        if response is not None and response.status in {403, 429, 498}:
            blocked = True
            if home == _WB_HOME:
                await ws.send_json(
                    {
                        "type": "status",
                        "text": "Основной домен WB запросил защитную проверку; открываю официальный резервный домен…",
                    }
                )
            continue
        parsed = urlparse(page.url)
        if parsed.scheme == "https" and parsed.hostname:
            storefront_origin = f"https://{parsed.hostname}"
        else:
            storefront_origin = home.rstrip("/")
        with contextlib.suppress(timeout_error):
            await page.wait_for_load_state("domcontentloaded", timeout=20_000)
        break
    if not storefront_origin:
        if blocked:
            raise CaptchaRequired
        raise LoginFlowError("Wildberries не ответил. Повторите вход позже.")
    started = asyncio.get_running_loop().time()
    clicked = False
    opened_account = False
    while asyncio.get_running_loop().time() - started < 65:
        phone = await _unique_visible(page, _PHONE_SELECTORS, "поле телефона")
        if phone is not None:
            return phone, storefront_origin
        if await _captcha_element_visible(page):
            raise CaptchaRequired
        if not clicked:
            clicked = await _click_unique(page, _LOGIN_TRIGGER_SELECTORS, "кнопку входа")
        if not opened_account and asyncio.get_running_loop().time() - started > 15:
            opened_account = True
            try:
                await page.goto(
                    f"{storefront_origin}/lk",
                    wait_until="commit",
                    timeout=20_000,
                )
                with contextlib.suppress(timeout_error):
                    await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            except timeout_error:
                with contextlib.suppress(Exception):
                    await page.evaluate("window.stop()")
        await asyncio.sleep(0.8)
    body = await _page_text(page)
    if _contains_captcha(body):
        raise CaptchaRequired
    raise LoginFlowError("Wildberries не показал поле телефона. Повторите вход позже.")


async def _submit_phone(page: Any, phone_input: Any, phone: str) -> None:
    await _select_phone_country(page, phone)
    await phone_input.fill(phone)
    if not await _click_unique(page, _SUBMIT_SELECTORS, "кнопку отправки кода"):
        await phone_input.press("Enter")


async def _select_phone_country(page: Any, phone: str) -> None:
    country = _phone_country(phone)
    container = page.locator('[data-testid="auth-phone-input-mask-input-countries"]')
    if await container.count() == 0:
        return
    if country is None:
        raise LoginFlowError("Страна номера пока не поддерживается формой Wildberries")
    radio = page.locator(f'input[type="radio"][name="phoneNumber"][value="{country}"]')
    if await radio.count() != 1:
        raise LoginFlowError("Wildberries не показал нужную страну номера")
    if await radio.is_checked():
        return
    opener = container.locator('[data-class="btn"]')
    if await opener.count() != 1:
        raise LoginFlowError("Wildberries изменил выбор страны номера")
    await opener.click()
    label = page.locator(f'label[data-testid="{country}"]')
    if await label.count() != 1 or not await label.is_visible():
        raise LoginFlowError("Wildberries не показал нужную страну номера")
    await label.click()
    if not await radio.is_checked():
        raise LoginFlowError("Wildberries не применил страну номера")


async def _wait_for_code_form(page: Any, max_wait_seconds: float = 45.0) -> list[Any]:
    started = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - started < max_wait_seconds:
        fields = await _code_fields(page)
        if fields:
            return fields
        if await _captcha_element_visible(page):
            raise CaptchaRequired
        feedback = _login_feedback(await _page_text(page))
        if feedback:
            raise LoginFlowError(feedback)
        await asyncio.sleep(0.5)
    body = await _page_text(page)
    if _contains_captcha(body):
        raise CaptchaRequired
    raise LoginFlowError("Wildberries не показал поле SMS-кода. Проверьте номер и повторите вход.")


async def _fill_code(page: Any, fields: list[Any], code: str) -> None:
    submit_button = await _unique_visible(
        page,
        _OTP_SUBMIT_SELECTORS,
        "кнопку подтверждения кода",
        require_enabled=False,
    )
    if len(fields) == 1:
        await fields[0].fill(code)
        last_field = fields[0]
    else:
        if len(fields) < len(code):
            raise LoginFlowError("Форма SMS-кода Wildberries изменилась. Повторите вход позже.")
        for field, digit in zip(fields, code, strict=False):
            await field.fill(digit)
        last_field = fields[min(len(code), len(fields)) - 1]
    if submit_button is not None:
        with contextlib.suppress(Exception):
            await submit_button.click(timeout=5_000)
        return
    # Separate one-digit fields normally auto-submit after the last digit.
    if len(fields) > 1:
        return
    with contextlib.suppress(Exception):
        await last_field.press("Enter")


async def _wait_for_login_result(page: Any, max_wait_seconds: float = 35.0) -> str | None:
    started = asyncio.get_running_loop().time()
    code_was_hidden_at: float | None = None
    while asyncio.get_running_loop().time() - started < max_wait_seconds:
        if await _captcha_element_visible(page):
            raise CaptchaRequired
        feedback = _login_feedback(await _page_text(page))
        if feedback:
            return feedback
        fields = await _code_fields(page, require_enabled=False)
        if not fields:
            code_was_hidden_at = code_was_hidden_at or asyncio.get_running_loop().time()
            if await _any_visible(page, _PROFILE_SELECTORS) or (
                asyncio.get_running_loop().time() - code_was_hidden_at > 2
            ):
                return None
        else:
            code_was_hidden_at = None
        await asyncio.sleep(0.5)
    if _contains_captcha(await _page_text(page)):
        raise CaptchaRequired
    return "Wildberries не подтвердил код. Проверьте код и попробуйте ещё раз."


async def _code_fields(page: Any, *, require_enabled: bool = True) -> list[Any]:
    for index, selector in enumerate(_CODE_SELECTORS):
        visible = await _visible(page, selector, require_enabled=require_enabled)
        if index == len(_CODE_SELECTORS) - 1:
            filtered: list[Any] = []
            for item in visible:
                maximum = await item.get_attribute("maxlength")
                placeholder = (await item.get_attribute("placeholder") or "").lower()
                try:
                    maximum_value = int(maximum or "0")
                except ValueError:
                    maximum_value = 0
                if (
                    1 <= maximum_value <= 8
                    and "+7" not in placeholder
                    and "телефон" not in placeholder
                ):
                    filtered.append(item)
            visible = filtered
        if visible:
            return visible
    return []


async def _visible(page: Any, selector: str, *, require_enabled: bool = True) -> list[Any]:
    try:
        locator = page.locator(selector)
        count = min(await locator.count(), 12)
    except Exception:
        return []
    result: list[Any] = []
    for index in range(count):
        item = locator.nth(index)
        with contextlib.suppress(Exception):
            if await item.is_visible() and (not require_enabled or await item.is_enabled()):
                result.append(item)
    return result


async def _any_visible(page: Any, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        if await _visible(page, selector):
            return True
    return False


async def _unique_visible(
    page: Any,
    selectors: tuple[str, ...],
    label: str,
    *,
    required: bool = False,
    require_enabled: bool = True,
) -> Any | None:
    for selector in selectors:
        visible = await _visible(page, selector, require_enabled=require_enabled)
        if len(visible) == 1:
            return visible[0]
        if len(visible) > 1:
            raise LoginFlowError(f"Wildberries показал неоднозначное {label}. Повторите позже.")
    if required:
        raise LoginFlowError(f"Wildberries не показал {label}. Повторите вход позже.")
    return None


async def _click_unique(
    page: Any, selectors: tuple[str, ...], label: str, *, required: bool = False
) -> bool:
    item = await _unique_visible(page, selectors, label, required=required)
    if item is None:
        return False
    await item.click()
    return True


async def _captcha_element_visible(page: Any) -> bool:
    for selector in _CAPTCHA_SELECTORS:
        if await _visible(page, selector):
            return True
    return False


async def _page_text(page: Any) -> str:
    with contextlib.suppress(Exception):
        return str(await page.locator("body").inner_text(timeout=1_000))[:100_000]
    return ""


def _contains_captcha(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in ("captcha", "капча", "я не робот", "подтвердите, что вы не робот")
    )


def _login_feedback(text: str) -> str | None:
    lowered = " ".join(text.lower().split())
    patterns = (
        (
            ("неверный код", "код введен неверно", "код введён неверно"),
            "Код не подошёл. Введите новый код от Wildberries.",
        ),
        (
            ("срок действия кода", "код устарел", "код истек", "код истёк"),
            "Срок действия кода истёк. Начните вход заново.",
        ),
        (
            ("слишком много попыток", "повторите попытку позже", "превышено количество"),
            "Wildberries временно ограничил вход. Повторите позже.",
        ),
        (
            ("некорректный номер", "неверный номер", "проверьте номер"),
            "Wildberries не принял номер телефона. Проверьте его.",
        ),
    )
    for markers, message in patterns:
        if any(marker in lowered for marker in markers):
            return message
    return None


def _normalize_phone(raw: str) -> str:
    value = raw.strip()
    if not value or len(value) > 32 or any(char.isalpha() for char in value):
        raise ValueError("Введите корректный номер телефона")
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) < 11 or len(digits) > 15:
        raise ValueError("Номер должен содержать от 11 до 15 цифр")
    return "+" + digits


def _phone_country(phone: str) -> str | None:
    prefixes = (
        ("+375", "by"),
        ("+374", "am"),
        ("+996", "kg"),
        ("+998", "uz"),
        ("+992", "tj"),
        ("+995", "ge"),
        ("+972", "il"),
        ("+251", "et"),
        ("+886", "cn2"),
        ("+86", "cn"),
        ("+85", "cn1"),
        ("+7", "ru"),
    )
    return next((country for prefix, country in prefixes if phone.startswith(prefix)), None)


def _normalize_code(raw: str) -> str:
    value = raw.strip().replace(" ", "")
    if not value.isdecimal() or len(value) < 4 or len(value) > 8:
        raise ValueError("Код должен содержать от 4 до 8 цифр")
    return value


def _connector_confirms_account(
    baseline_bearer: str, current_bearer: str, user_marker: str
) -> bool:
    has_user_marker = user_marker.strip().lower() not in {
        "",
        "0",
        "guest",
        "null",
        "undefined",
    }
    bearer_changed = bool(baseline_bearer) and current_bearer != baseline_bearer
    return has_user_marker or bearer_changed


def _localized_connector_url(
    raw_url: str, *, currency: str, language: str, destination: int
) -> str:
    parsed = urlparse(raw_url)
    if parsed.scheme != "https" or parsed.hostname != "card.wb.ru":
        raise LoginFlowError("WB вернул небезопасный адрес персональной цены")
    parameters = dict(parse_qsl(parsed.query, keep_blank_values=True))
    parameters.update(
        {
            "appType": parameters.get("appType", "1") or "1",
            "curr": currency,
            "dest": str(destination),
            "lang": language,
        }
    )
    return urlunparse(parsed._replace(query=urlencode(parameters)))


def _allowed_wb_navigation(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme == "about" and parsed.path == "blank":
        return True
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    hostname = parsed.hostname.lower()
    return hostname in {"wildberries.ru", "wildberries.by", "wb.ru"} or hostname.endswith(
        (".wildberries.ru", ".wildberries.by", ".wb.ru")
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
    :root {{ color-scheme:dark; font-family:system-ui,-apple-system,sans-serif; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:#10131a; color:#f7f8fa; }}
    main {{ min-height:100vh; display:grid; place-items:center; padding:18px; }}
    .card {{ width:min(100%,480px); background:#181d27; border:1px solid #323a4b; border-radius:20px; padding:22px; }}
    h1 {{ margin:0 0 8px; font-size:22px; }}
    #status {{ min-height:44px; margin:12px 0; color:#cbd5e1; line-height:1.4; }}
    .step {{ display:none; gap:10px; }}
    .step.active {{ display:grid; }}
    label {{ color:#e2e8f0; font-size:14px; font-weight:650; }}
    input,button {{ min-height:48px; border-radius:12px; font-size:16px; }}
    input {{ width:100%; padding:10px 12px; background:#0f141d; color:#fff; border:1px solid #465066; }}
    button {{ border:1px solid #7040dd; background:#7c3aed; color:#fff; padding:10px 14px; font-weight:700; }}
    button.secondary {{ width:100%; margin-top:10px; background:#252c39; border-color:#3c4659; color:#fecaca; }}
    button:disabled {{ opacity:.55; }}
    small {{ display:block; margin-top:16px; color:#94a3b8; line-height:1.45; }}
    .lock {{ color:#86efac; font-size:13px; }}
  </style>
</head>
<body><main><section class="card">
  <div class="lock">🔒 Защищённое соединение</div>
  <h1>Вход в Wildberries</h1>
  <div id="status">Подключаюсь к серверу авторизации…</div>
  <div id="phone-step" class="step">
    <label for="phone">Номер телефона</label>
    <input id="phone" type="tel" inputmode="tel" autocomplete="tel" placeholder="+7 999 123-45-67" maxlength="32">
    <button id="phone-send">Получить код</button>
  </div>
  <div id="code-step" class="step">
    <label for="code">Код от Wildberries</label>
    <input id="code" type="text" inputmode="numeric" autocomplete="one-time-code" placeholder="Код из SMS или приложения" maxlength="8">
    <button id="code-send">Подтвердить</button>
  </div>
  <button id="cancel" class="secondary">Отмена</button>
  <small>Номер и одноразовый код кратковременно проходят через этот сервер по TLS и вводятся в официальный сайт Wildberries. Они не сохраняются в базе, журналах, снимках или записях экрана. CAPTCHA бот не обходит.</small>
</section></main>
<script nonce="{nonce}">
(() => {{
  const tg=window.Telegram?.WebApp; tg?.ready(); tg?.expand();
  const status=document.getElementById('status');
  const phoneStep=document.getElementById('phone-step');
  const codeStep=document.getElementById('code-step');
  const phone=document.getElementById('phone');
  const code=document.getElementById('code');
  const phoneSend=document.getElementById('phone-send');
  const codeSend=document.getElementById('code-send');
  let current='';
  const scheme=location.protocol==='https:'?'wss':'ws';
  const ws=new WebSocket(`${{scheme}}://${{location.host}}/ws/{safe_session}`);
  const setStep=step=>{{
    current=step; phoneStep.classList.toggle('active',step==='phone');
    codeStep.classList.toggle('active',step==='code');
    phoneSend.disabled=false; codeSend.disabled=false;
    if(step==='phone') phone.focus(); if(step==='code') code.focus();
  }};
  const sendValue=(type,input,button)=>{{
    if(current!==type || ws.readyState!==WebSocket.OPEN) return;
    button.disabled=true; ws.send(JSON.stringify({{type,value:input.value}}));
    input.value='';
  }};
  ws.onopen=()=>ws.send(JSON.stringify({{type:'init',initData:tg?.initData||''}}));
  ws.onmessage=event=>{{
    const data=JSON.parse(event.data); status.textContent=data.text||'';
    if(data.type==='step') setStep(data.step);
    if(data.type==='status') {{ phoneSend.disabled=true; codeSend.disabled=true; }}
    if(data.type==='error') {{ status.style.color='#fca5a5'; if(!data.fatal && current) setStep(current); }}
    if(data.type==='success') {{ status.style.color='#86efac'; phoneStep.classList.remove('active'); codeStep.classList.remove('active'); setTimeout(()=>tg?.close(),1200); }}
  }};
  ws.onerror=()=>{{ status.textContent='Не удалось подключиться к серверу авторизации'; status.style.color='#fca5a5'; }};
  ws.onclose=()=>{{ if(!status.textContent.includes('подключён')) {{ status.textContent='Соединение закрыто. Окно можно открыть повторно.'; }} }};
  phoneSend.onclick=()=>sendValue('phone',phone,phoneSend);
  codeSend.onclick=()=>sendValue('code',code,codeSend);
  phone.addEventListener('keydown',e=>{{if(e.key==='Enter')phoneSend.click();}});
  code.addEventListener('keydown',e=>{{if(e.key==='Enter')codeSend.click();}});
  document.getElementById('cancel').onclick=()=>{{ if(ws.readyState===WebSocket.OPEN) ws.send(JSON.stringify({{type:'cancel'}})); tg?.close(); }};
}})();
</script></body></html>"""


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
    service = AuthWebService(
        settings=settings,
        database=database,
        cipher=cipher,
        bot=bot,
    )
    runner = web.AppRunner(service.create_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.auth_bind_host, settings.auth_port)
    await site.start()
    logger.info(
        "Telegram-форма авторизации WB запущена на %s:%s",
        settings.auth_bind_host,
        settings.auth_port,
    )
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await bot.session.close()
        await database.close()
