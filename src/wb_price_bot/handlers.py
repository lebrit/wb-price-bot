from __future__ import annotations

import contextlib
import html
import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject

from .config import Settings
from .database import Database, ProductAlreadyExistsError, ProductLimitError
from .domain import (
    PriceSnapshot,
    ThresholdKind,
    VariantSelection,
    format_money,
    parse_user_number,
    percent_to_basis_points,
    rubles_to_kopecks,
)
from .features import register_feature_handlers
from .keyboards import (
    access_review_keyboard,
    account_auth_keyboard,
    account_delete_confirmation,
    account_keyboard,
    account_warning_keyboard,
    delete_confirmation,
    main_keyboard,
    product_keyboard,
    products_keyboard,
    threshold_keyboard,
    variant_keyboard,
)
from .monitor import PriceMonitor, seconds_since, source_label
from .security import SessionCipher
from .wildberries import (
    AccountSessionError,
    AccountWildberriesClient,
    FetchResult,
    MpstatsPriceClient,
    ProductReferenceError,
    ProductVariant,
    PublicWildberriesClient,
    WildberriesError,
)

_PRODUCTS_PER_PAGE = 8


@dataclass(frozen=True, slots=True)
class HandlerContext:
    settings: Settings
    database: Database
    public_client: PublicWildberriesClient
    account_client: AccountWildberriesClient
    licensed_client: MpstatsPriceClient
    cipher: SessionCipher
    monitor: PriceMonitor


class AddProductStates(StatesGroup):
    waiting_reference = State()
    waiting_variant = State()
    waiting_kind = State()
    waiting_value = State()


class AccessMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings, database: Database) -> None:
        self._settings = settings
        self._database = database

    async def __call__(
        self,
        handler: Any,
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return None
        stored = await self._database.get_user(user.id)
        if user.id in self._settings.allowed_users:
            if (
                stored is None
                or not stored.is_admin
                or stored.access_status != "approved"
                or not stored.is_enabled
            ):
                await self._database.ensure_user(
                    user.id,
                    user.username,
                    user.full_name,
                    is_admin=True,
                )
            return await handler(event, data)
        if (
            self._settings.registration_mode != "allowlist"
            and stored is not None
            and stored.access_status == "approved"
            and stored.is_enabled
        ):
            return await handler(event, data)
        command = (event.text or "").split(maxsplit=1) if isinstance(event, Message) else []
        is_start = bool(command) and command[0].split("@", 1)[0] == "/start"
        if is_start and self._settings.registration_mode != "allowlist":
            return await handler(event, data)
        status = stored.access_status if stored is not None else "unknown"
        message = (
            "⏳ Заявка на доступ ожидает решения администратора."
            if status == "pending"
            else "⛔ Доступ к боту не разрешён."
        )
        if isinstance(event, CallbackQuery):
            await event.answer(message, show_alert=True)
        elif isinstance(event, Message):
            await event.answer(message)
        return None


def create_router(context: HandlerContext) -> Router:
    router = Router(name="main")
    access = AccessMiddleware(context.settings, context.database)
    router.message.middleware(access)
    router.callback_query.middleware(access)

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        user = message.from_user
        assert user is not None
        is_admin = user.id in context.settings.allowed_users
        existing = await context.database.get_user(user.id)
        auto_approve = is_admin or context.settings.registration_mode == "open"
        registered = await context.database.ensure_user(
            user.id,
            user.username,
            user.full_name,
            is_admin=is_admin,
            auto_approve=auto_approve,
        )
        if registered.access_status != "approved":
            if registered.access_status == "blocked":
                await message.answer("⛔ Администратор отклонил доступ к этому боту.")
                return
            await message.answer(
                "⏳ <b>Заявка отправлена</b>\n\n"
                "Администратор получит уведомление и сможет разрешить доступ."
            )
            if existing is None:
                identity = (
                    f"@{html.escape(user.username)}"
                    if user.username
                    else html.escape(user.full_name)
                )
                for admin_id in context.settings.allowed_users:
                    try:
                        if message.bot is None:
                            break
                        await message.bot.send_message(
                            admin_id,
                            "👤 <b>Новая заявка на доступ</b>\n\n"
                            f"Пользователь: {identity}\n"
                            f"Telegram ID: <code>{user.id}</code>",
                            reply_markup=access_review_keyboard(user.id),
                        )
                    except Exception:
                        pass
            return
        await message.answer(
            "👋 <b>WB Price Bot готов.</b>\n\n"
            "Добавьте ссылку Wildberries, выберите условие — процент, сумму падения "
            "или целевую цену — и бот будет проверять товар в фоне.\n\n"
            "Цена зависит от региона, размера, способа оплаты и аккаунта. "
            "Источник всегда указан в карточке и уведомлении.",
            reply_markup=main_keyboard(),
        )

    @router.callback_query(F.data.startswith("access:"))
    async def review_access(callback: CallbackQuery) -> None:
        if callback.from_user.id not in context.settings.allowed_users:
            await callback.answer("Только для администратора", show_alert=True)
            return
        try:
            _, action, raw_id = str(callback.data).split(":", 2)
            target_id = int(raw_id)
            approve = action == "approve"
            if action not in {"approve", "block"}:
                raise ValueError
            reviewed = await context.database.review_user_access(
                callback.from_user.id,
                target_id,
                approve=approve,
                configured_admins=context.settings.allowed_users,
            )
        except (ValueError, PermissionError):
            await callback.answer("Не удалось изменить доступ", show_alert=True)
            return
        if reviewed is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        await callback.answer("Доступ разрешён" if approve else "Доступ отклонён")
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=None)
        if callback.bot is not None:
            with contextlib.suppress(Exception):
                await callback.bot.send_message(
                    target_id,
                    "✅ Доступ к WB Price Bot разрешён. Отправьте /start."
                    if approve
                    else "⛔ Администратор отклонил заявку на доступ.",
                )

    @router.message(Command("users"))
    async def show_users(message: Message) -> None:
        if message.from_user is None:
            return
        if message.from_user.id not in context.settings.allowed_users:
            await message.answer("Команда доступна администратору.")
            return
        requested = (message.text or "").partition(" ")[2].strip().lower() or "pending"
        aliases = {
            "pending": "pending",
            "new": "pending",
            "approved": "approved",
            "active": "approved",
            "blocked": "blocked",
        }
        status = aliases.get(requested)
        if status is None:
            await message.answer("Использование: <code>/users pending|approved|blocked</code>")
            return
        users = await context.database.users_by_access_status(status, limit=50)
        if not users:
            await message.answer(f"👥 Пользователей со статусом {status} нет.")
            return
        await message.answer(
            f"👥 Статус <b>{status}</b>: {len(users)}\n"
            "Фильтры: <code>/users pending</code>, <code>/users approved</code>, "
            "<code>/users blocked</code>"
        )
        for item in users:
            identity = (
                f"@{html.escape(item.username)}"
                if item.username
                else html.escape(item.display_name)
            )
            await message.answer(
                f"{identity}\nTelegram ID: <code>{item.telegram_id}</code>",
                reply_markup=access_review_keyboard(item.telegram_id),
            )

    @router.message(Command("cancel"))
    async def cancel_command(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=main_keyboard())

    @router.callback_query(F.data == "cancel")
    async def cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Отменено")
        if isinstance(callback.message, Message):
            await callback.message.answer("Действие отменено.", reply_markup=main_keyboard())

    async def begin_add(target: Message | CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddProductStates.waiting_reference)
        text = (
            "Отправьте ссылку на товар Wildberries или его числовой артикул.\n\n"
            "Можно добавлять сколько угодно товаров по очереди, в пределах лимита сервера."
        )
        if isinstance(target, Message):
            await target.answer(text)
        elif isinstance(target.message, Message):
            await target.message.answer(text)
            await target.answer()

    router.message.register(begin_add, Command("add"))
    router.message.register(begin_add, F.text == "➕ Добавить товар")
    router.callback_query.register(begin_add, F.data == "add:start")

    @router.message(AddProductStates.waiting_reference, F.text)
    async def receive_reference(message: Message, state: FSMContext) -> None:
        assert message.text is not None and message.from_user is not None
        wait_message = await message.answer("Проверяю карточку и текущую цену…")
        try:
            nm_id = await context.public_client.resolve_reference(message.text)
            result = await _fetch_catalog_for_user(context, message.from_user.id, nm_id)
            snapshot = result.products.get(nm_id)
            if snapshot is None:
                raise WildberriesError(result.errors.get(nm_id, "Товар не найден"))
        except (ProductReferenceError, WildberriesError, AccountSessionError, ValueError) as exc:
            await wait_message.edit_text(f"Не удалось получить товар: {html.escape(str(exc))}")
            return
        variants = result.variants.get(nm_id, [])
        minimum_snapshot = replace(snapshot, option_id=None, size_name=None)
        await state.update_data(
            snapshot=_snapshot_to_state(minimum_snapshot),
            variants=[_variant_to_state(item, snapshot.source) for item in variants],
        )
        if variants:
            await state.set_state(AddProductStates.waiting_variant)
            seller = variants[0].supplier_name or "не указан"
            await wait_message.edit_text(
                _snapshot_preview(minimum_snapshot)
                + f"\nПродавец закреплён: <b>{html.escape(seller)}</b>.\n\n"
                "Выберите конкретный размер:",
                reply_markup=variant_keyboard(
                    [
                        (item.option_id, item.size_name, item.price, item.available)
                        for item in variants
                    ]
                ),
            )
            return
        await state.set_state(AddProductStates.waiting_kind)
        await wait_message.edit_text(
            _snapshot_preview(snapshot) + "\n\nКакое условие уведомления установить?\n"
            "Процент и сумма считаются от максимальной цены после добавления или прошлого сигнала.",
            reply_markup=threshold_keyboard(),
        )

    @router.callback_query(AddProductStates.waiting_variant, F.data.startswith("addvariant:"))
    async def choose_variant(callback: CallbackQuery, state: FSMContext) -> None:
        try:
            option_id = int(str(callback.data).split(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("Некорректный размер", show_alert=True)
            return
        data = await state.get_data()
        if option_id == 0:
            snapshot = _snapshot_from_state(data["snapshot"])
            await state.set_state(AddProductStates.waiting_kind)
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    _snapshot_preview(snapshot) + "\nРежим: <b>минимальная доступная цена</b>.\n\n"
                    "Какое условие уведомления установить?",
                    reply_markup=threshold_keyboard(),
                )
            await callback.answer()
            return
        variants = data.get("variants", [])
        selected = next(
            (item for item in variants if int(item.get("option_id", 0)) == option_id), None
        )
        if not isinstance(selected, dict):
            await callback.answer("Размер больше не доступен", show_alert=True)
            return
        snapshot = _snapshot_from_state(selected["snapshot"])
        await state.update_data(snapshot=_snapshot_to_state(snapshot))
        await state.set_state(AddProductStates.waiting_kind)
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                _snapshot_preview(snapshot) + "\n\nКакое условие уведомления установить?",
                reply_markup=threshold_keyboard(),
            )
        await callback.answer()

    @router.callback_query(AddProductStates.waiting_kind, F.data.startswith("addkind:"))
    async def choose_threshold(callback: CallbackQuery, state: FSMContext) -> None:
        kind_value = str(callback.data).split(":", 1)[1]
        try:
            kind = ThresholdKind(kind_value)
        except ValueError:
            await callback.answer("Неизвестный тип условия", show_alert=True)
            return
        await state.update_data(threshold_kind=kind.value)
        await state.set_state(AddProductStates.waiting_value)
        prompts = {
            ThresholdKind.PERCENT: "Введите процент снижения, например 10 или 7,5:",
            ThresholdKind.AMOUNT: "Введите сумму снижения в рублях, например 500:",
            ThresholdKind.TARGET: "Введите целевую цену в рублях, например 2990:",
        }
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(prompts[kind])

    @router.message(AddProductStates.waiting_value, F.text)
    async def receive_threshold(message: Message, state: FSMContext) -> None:
        assert message.text is not None and message.from_user is not None
        data = await state.get_data()
        try:
            kind = ThresholdKind(str(data["threshold_kind"]))
            number = parse_user_number(message.text)
            value = (
                percent_to_basis_points(number)
                if kind is ThresholdKind.PERCENT
                else rubles_to_kopecks(number)
            )
            snapshot = _snapshot_from_state(data["snapshot"])
            product = await context.database.add_product(
                telegram_id=message.from_user.id,
                snapshot=snapshot,
                threshold_kind=kind,
                threshold_value=value,
                max_products=context.settings.max_products_per_user,
                selection=VariantSelection(
                    option_id=snapshot.option_id,
                    size_name=snapshot.size_name,
                    supplier_id=snapshot.supplier_id,
                    supplier_name=snapshot.supplier_name,
                ),
            )
        except (ValueError, KeyError, TypeError) as exc:
            await message.answer(html.escape(str(exc)))
            return
        except (ProductAlreadyExistsError, ProductLimitError) as exc:
            await state.clear()
            await message.answer(str(exc), reply_markup=main_keyboard())
            return
        await state.clear()
        await message.answer(
            "✅ <b>Товар добавлен</b>\n\n"
            f"{html.escape(product.title)}\n"
            f"Текущая цена: <b>{format_money(product.current_price)}</b>\n"
            f"Условие: {_threshold_label(product.threshold_kind, product.threshold_value)}\n"
            f"Следующая фоновая проверка — не позднее чем через "
            f"{context.settings.check_interval_seconds // 60} мин.",
            reply_markup=main_keyboard(),
        )

    async def show_products(target: Message | CallbackQuery, page: int = 0) -> None:
        user = target.from_user
        assert user is not None
        page = max(0, page)
        products, total = await context.database.list_products(
            user.id, offset=page * _PRODUCTS_PER_PAGE, limit=_PRODUCTS_PER_PAGE
        )
        if not products and page > 0:
            page = 0
            products, total = await context.database.list_products(
                user.id, offset=0, limit=_PRODUCTS_PER_PAGE
            )
        if not products:
            text = "У вас пока нет товаров. Нажмите «➕ Добавить товар»."
            markup = None
        else:
            active = sum(1 for item in products if item.is_active)
            text = f"📦 <b>Ваши товары</b>\nВсего: {total}. На этой странице активно: {active}."
            markup = products_keyboard(products, page, total, _PRODUCTS_PER_PAGE)
        if isinstance(target, Message):
            await target.answer(text, reply_markup=markup or main_keyboard())
        elif isinstance(target.message, Message):
            await target.message.edit_text(text, reply_markup=markup)
            await target.answer()

    router.message.register(show_products, Command("list"))
    router.message.register(show_products, F.text == "📦 Мои товары")

    @router.callback_query(F.data.startswith("products:"))
    async def paginate_products(callback: CallbackQuery) -> None:
        try:
            page = int(str(callback.data).split(":", 1)[1])
        except ValueError:
            page = 0
        await show_products(callback, page)

    @router.callback_query(F.data == "noop")
    async def noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.callback_query(F.data.startswith("product:"))
    async def product_detail(callback: CallbackQuery) -> None:
        product_id = _callback_id(callback)
        if product_id is None or callback.from_user is None:
            return
        product = await context.database.get_product(callback.from_user.id, product_id)
        if product is None:
            await callback.answer("Товар не найден", show_alert=True)
            return
        text = _product_text(product)
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                text,
                reply_markup=product_keyboard(product),
                disable_web_page_preview=False,
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("toggle:"))
    async def toggle_product(callback: CallbackQuery) -> None:
        product_id = _callback_id(callback)
        if product_id is None:
            return
        active = await context.database.toggle_product(callback.from_user.id, product_id)
        if active is None:
            await callback.answer("Товар не найден", show_alert=True)
            return
        await callback.answer("Мониторинг включён" if active else "Мониторинг приостановлен")
        product = await context.database.get_product(callback.from_user.id, product_id)
        if product is not None and isinstance(callback.message, Message):
            await callback.message.edit_text(
                _product_text(product), reply_markup=product_keyboard(product)
            )

    @router.callback_query(F.data.startswith("deleteask:"))
    async def ask_delete(callback: CallbackQuery) -> None:
        product_id = _callback_id(callback)
        if product_id is None:
            return
        product = await context.database.get_product(callback.from_user.id, product_id)
        if product is None:
            await callback.answer("Товар не найден", show_alert=True)
            return
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"Удалить «{html.escape(product.title)}» и его историю?",
                reply_markup=delete_confirmation(product.id),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("deleteconfirm:"))
    async def confirm_delete(callback: CallbackQuery) -> None:
        product_id = _callback_id(callback)
        if product_id is None:
            return
        await context.database.delete_product(callback.from_user.id, product_id)
        await show_products(callback, 0)

    @router.callback_query(F.data.startswith("history:"))
    async def history(callback: CallbackQuery) -> None:
        product_id = _callback_id(callback)
        if product_id is None:
            return
        product = await context.database.get_product(callback.from_user.id, product_id)
        rows = await context.database.recent_history(callback.from_user.id, product_id)
        if product is None:
            await callback.answer("Товар не найден", show_alert=True)
            return
        lines = [f"📈 <b>История: {html.escape(product.title)}</b>"]
        for row in rows:
            stamp = row.observed_at.strftime("%d.%m %H:%M")
            availability = "в наличии" if row.is_available else "нет в наличии"
            lines.append(f"{stamp} — {format_money(row.price)}, {availability}")
        if len(lines) == 1:
            lines.append("Изменений пока нет.")
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "\n".join(lines), reply_markup=product_keyboard(product)
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("productcheck:"))
    async def manual_check(callback: CallbackQuery) -> None:
        product_id = _callback_id(callback)
        if product_id is None:
            return
        await callback.answer("Проверяю…")
        try:
            snapshot = await context.monitor.check_product(callback.from_user.id, product_id)
        except (WildberriesError, AccountSessionError, ValueError) as exc:
            if isinstance(callback.message, Message):
                await callback.message.answer(f"Проверка не выполнена: {html.escape(str(exc))}")
            return
        if isinstance(callback.message, Message):
            await callback.message.answer(
                f"✅ Проверено: <b>{format_money(snapshot.price)}</b>\n"
                f"Источник: {source_label(snapshot.source)}"
            )

    async def show_account(target: Message | CallbackQuery) -> None:
        user = target.from_user
        assert user is not None
        account = await context.database.get_wb_account(user.id)
        if account is None:
            text = (
                "👤 <b>Аккаунт Wildberries не подключён</b>\n\n"
                "Бот использует публичную цену для выбранного региона. "
                "Можно подключить свой браузерный сеанс для персональной цены (beta)."
            )
        else:
            validated = (
                account.last_validated_at.strftime("%d.%m.%Y %H:%M")
                if account.last_validated_at
                else "ещё не проверена"
            )
            text = (
                "👤 <b>Аккаунт Wildberries</b>\n\n"
                f"Статус: <b>{html.escape(account.status)}</b>\n"
                f"Последняя проверка: {validated}"
            )
            if account.last_error:
                text += f"\nОшибка: {html.escape(account.last_error)}"
        markup = account_keyboard(account is not None)
        if isinstance(target, Message):
            await target.answer(text, reply_markup=markup)
        elif isinstance(target.message, Message):
            await target.message.edit_text(text, reply_markup=markup)
            await target.answer()

    router.message.register(show_account, Command("account"))
    router.message.register(show_account, F.text == "👤 Аккаунт WB")
    router.callback_query.register(show_account, F.data == "account:show")

    @router.callback_query(F.data == "account:warning")
    async def account_warning(callback: CallbackQuery) -> None:
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "⚠️ <b>Перед подключением</b>\n\n"
                "Wildberries не предоставляет официального API покупательских цен, а его "
                "условия ограничивают автоматический сбор цен и наличия. Функция экспериментальная: "
                "сессия может истечь, аккаунт может потребовать повторный вход.\n\n"
                "Сессия хранится только на вашем сервере в зашифрованном виде. Бот не просит "
                "телефон, SMS-код, пароль и не обходит CAPTCHA.",
                reply_markup=account_warning_keyboard(),
            )
        await callback.answer()

    @router.callback_query(F.data == "account:accept")
    async def accept_account_warning(callback: CallbackQuery) -> None:
        if (
            not isinstance(callback.message, Message)
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer("Подключение доступно только в личном чате", show_alert=True)
            return
        if not context.settings.auth_enabled:
            await callback.message.edit_text(
                "Web-авторизация не настроена на сервере. Укажите домен через "
                "<code>sudo wb-price-bot</code> → «Web-авторизация».",
                reply_markup=account_keyboard(
                    await context.database.get_wb_account(callback.from_user.id) is not None
                ),
            )
            await callback.answer()
            return
        auth_session = await context.database.create_auth_session(
            callback.from_user.id, context.settings.auth_session_ttl_seconds
        )
        url = f"{context.settings.auth_public_url}/login/{auth_session.id}"
        ttl_minutes = context.settings.auth_session_ttl_seconds // 60
        await callback.message.edit_text(
            "🔐 <b>Защищённое окно готово</b>\n\n"
            f"Оно действует {ttl_minutes} мин. и привязано к вашему Telegram ID. Войдите в Wildberries, "
            "затем нажмите внутри окна «Сохранить вход». Телефон, код и CAPTCHA не отправляются "
            "сообщениями боту.",
            reply_markup=account_auth_keyboard(url),
        )
        await callback.answer()

    @router.callback_query(F.data == "account:deleteask")
    async def account_delete_ask(callback: CallbackQuery) -> None:
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "Отключить аккаунт и удалить зашифрованную WB-сессию с сервера?",
                reply_markup=account_delete_confirmation(),
            )
        await callback.answer()

    @router.callback_query(F.data == "account:delete")
    async def account_delete(callback: CallbackQuery) -> None:
        await context.database.delete_wb_account(callback.from_user.id)
        await show_account(callback)

    @router.callback_query(F.data == "account:test")
    async def account_test(callback: CallbackQuery) -> None:
        account = await context.database.get_wb_account(callback.from_user.id)
        if account is None or account.status != "active":
            await callback.answer("Сначала подключите или обновите WB-сессию", show_alert=True)
            return
        product = await context.database.get_first_product(callback.from_user.id)
        if product is None:
            await callback.answer("Сначала добавьте хотя бы один товар", show_alert=True)
            return
        await callback.answer("Открываю WB в защищённом браузере…")
        try:
            snapshot = await context.monitor.check_product(callback.from_user.id, product.id)
        except (WildberriesError, AccountSessionError, ValueError) as exc:
            if isinstance(callback.message, Message):
                await callback.message.answer(f"Сессия не прошла проверку: {html.escape(str(exc))}")
            return
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "✅ Сессия работает.\n"
                f"Цена: <b>{format_money(snapshot.price)}</b>\n"
                f"Источник: {source_label(snapshot.source)}"
            )

    async def show_status(target: Message) -> None:
        assert target.from_user is not None
        _, own_total = await context.database.list_products(target.from_user.id, limit=1)
        stats = await context.database.stats()
        account = await context.database.get_wb_account(target.from_user.id)
        age = seconds_since(stats.last_monitor_cycle)
        cycle = "ещё не было" if age is None else f"{age} сек. назад"
        await target.answer(
            "🩺 <b>Состояние</b>\n\n"
            f"Ваших товаров: {own_total}\n"
            f"Активно во всём сервисе: {stats.products_active}\n"
            f"Уведомлений в очереди: {stats.alerts_pending}\n"
            f"Последний цикл: {cycle}\n"
            f"Аккаунт WB: {html.escape(account.status) if account else 'не подключён'}\n"
            f"Интервал: {context.settings.check_interval_seconds // 60} мин."
        )

    router.message.register(show_status, Command("status"))
    router.message.register(show_status, F.text == "🩺 Статус")

    async def help_message(message: Message) -> None:
        await message.answer(
            "❓ <b>Команды</b>\n\n"
            "/add — добавить товар\n"
            "/list — список товаров\n"
            "/account — аккаунт Wildberries\n"
            "/settings — регион, тихие часы и сводка\n"
            "/import — массовый импорт ссылок\n"
            "/export — экспорт CSV/JSON\n"
            "/folders — папки и теги\n"
            "/users pending|approved|blocked — пользователи (админ)\n"
            "/status — состояние сервиса\n"
            "/cancel — отменить текущий ввод\n\n"
            "Бот уведомляет о суммарном падении от контрольной цены. После уведомления "
            "новая цена становится контрольной. Если товар дорожает, контрольная цена повышается."
        )

    router.message.register(help_message, Command("help"))
    router.message.register(help_message, F.text == "❓ Помощь")

    register_feature_handlers(router, context)

    @router.message(StateFilter(None))
    async def unknown(message: Message) -> None:
        await message.answer(
            "Не понял команду. Выберите действие в меню.", reply_markup=main_keyboard()
        )

    return router


async def _fetch_catalog_for_user(
    context: HandlerContext, telegram_id: int, nm_id: int
) -> FetchResult:
    user = await context.database.get_user(telegram_id)
    destination = (
        user.wb_destination
        if user is not None and user.wb_destination is not None
        else context.settings.wb_destination
    )
    try:
        return await context.public_client.fetch_many([nm_id], destination=destination)
    except WildberriesError:
        if not context.licensed_client.enabled:
            raise
        return await context.licensed_client.fetch_many([nm_id])


def _snapshot_to_state(snapshot: PriceSnapshot) -> dict[str, Any]:
    return {
        "nm_id": snapshot.nm_id,
        "title": snapshot.title,
        "brand": snapshot.brand,
        "price": snapshot.price,
        "basic_price": snapshot.basic_price,
        "available": snapshot.available,
        "quantity": snapshot.quantity,
        "source": snapshot.source,
        "observed_at": snapshot.observed_at.isoformat(),
        "option_id": snapshot.option_id,
        "size_name": snapshot.size_name,
        "supplier_id": snapshot.supplier_id,
        "supplier_name": snapshot.supplier_name,
    }


def _snapshot_from_state(data: dict[str, Any]) -> PriceSnapshot:
    return PriceSnapshot(
        nm_id=int(data["nm_id"]),
        title=str(data["title"]),
        brand=str(data["brand"]) if data.get("brand") else None,
        price=int(data["price"]) if data.get("price") is not None else None,
        basic_price=int(data["basic_price"]) if data.get("basic_price") is not None else None,
        available=bool(data["available"]),
        quantity=int(data["quantity"]),
        source=str(data["source"]),
        observed_at=datetime.fromisoformat(str(data["observed_at"])),
        option_id=int(data["option_id"]) if data.get("option_id") is not None else None,
        size_name=str(data["size_name"]) if data.get("size_name") else None,
        supplier_id=(int(data["supplier_id"]) if data.get("supplier_id") is not None else None),
        supplier_name=str(data["supplier_name"]) if data.get("supplier_name") else None,
    )


def _variant_to_state(variant: ProductVariant, source: str) -> dict[str, Any]:
    snapshot = variant.snapshot(source)
    return {"option_id": variant.option_id, "snapshot": _snapshot_to_state(snapshot)}


def _snapshot_preview(snapshot: PriceSnapshot) -> str:
    availability = "в наличии" if snapshot.available else "нет в наличии"
    brand = f"{html.escape(snapshot.brand)} / " if snapshot.brand else ""
    return (
        f"<b>{brand}{html.escape(snapshot.title)}</b>\n"
        f"Артикул: <code>{snapshot.nm_id}</code>\n"
        f"Цена: <b>{format_money(snapshot.price)}</b>\n"
        f"Статус: {availability}\n"
        f"Источник: {source_label(snapshot.source)}"
    )


def _threshold_label(kind: str, value: int) -> str:
    if kind == ThresholdKind.PERCENT.value:
        return f"снижение на {value / 100:g}%"
    if kind == ThresholdKind.AMOUNT.value:
        return f"снижение на {format_money(value)}"
    return f"цена не выше {format_money(value)}"


def _product_text(product: Any) -> str:
    active = "активен" if product.is_active else "приостановлен"
    available = "в наличии" if product.is_available else "нет в наличии"
    checked = (
        product.last_checked_at.strftime("%d.%m.%Y %H:%M")
        if product.last_checked_at
        else "ещё не проверялся"
    )
    error = f"\nОшибка: {html.escape(product.last_error)}" if product.last_error else ""
    try:
        rules = json.loads(product.rules_json or "[]")
        tags = json.loads(product.tags_json or "[]")
    except json.JSONDecodeError:
        rules, tags = [], []
    rule_text = "; ".join(
        _threshold_label(str(item.get("kind")), int(item.get("value", 0)))
        for item in rules
        if isinstance(item, dict)
    ) or _threshold_label(product.threshold_kind, product.threshold_value)
    variant = ""
    if product.size_name:
        variant += f"Размер: <b>{html.escape(product.size_name)}</b>\n"
    else:
        variant += "Размер: <b>минимальная доступная цена</b>\n"
    if product.supplier_name:
        variant += f"Продавец: <b>{html.escape(product.supplier_name)}</b>\n"
    organization = ""
    if product.folder_name:
        organization += f"Папка: {html.escape(product.folder_name)}\n"
    if tags:
        organization += "Теги: " + " ".join(f"#{html.escape(str(tag))}" for tag in tags) + "\n"
    return (
        f'<a href="{html.escape(product.canonical_url, quote=True)}">'
        f"<b>{html.escape(product.title)}</b></a>\n\n"
        f"Артикул: <code>{product.nm_id}</code>\n"
        f"{variant}"
        f"Цена: <b>{format_money(product.current_price)}</b>\n"
        f"Минимум: {format_money(product.lowest_price)}\n"
        f"Наличие: {available}\n"
        f"Правила: {rule_text}\n"
        f"{organization}"
        f"Источник: {source_label(product.price_source)}\n"
        f"Мониторинг: {active}\n"
        f"Проверено: {checked}{error}"
    )


def _callback_id(callback: CallbackQuery) -> int | None:
    try:
        return int(str(callback.data).split(":", 1)[1])
    except (ValueError, IndexError):
        return None
