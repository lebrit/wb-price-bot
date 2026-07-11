from __future__ import annotations

import html
import io
from dataclasses import replace
from datetime import timedelta
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .charts import render_price_chart
from .database import ProductAlreadyExistsError, ProductLimitError
from .domain import (
    ThresholdKind,
    VariantSelection,
    format_clock,
    parse_clock,
    parse_user_number,
    percent_to_basis_points,
    rubles_to_kopecks,
    utcnow,
)
from .exports import export_products
from .keyboards import (
    charts_keyboard,
    edit_variant_keyboard,
    export_keyboard,
    location_keyboard,
    main_keyboard,
    rules_keyboard,
    settings_keyboard,
    threshold_keyboard,
)
from .wildberries import WildberriesError


class RuleStates(StatesGroup):
    waiting_kind = State()
    waiting_value = State()


class OrganizeStates(StatesGroup):
    waiting_folder = State()
    waiting_tags = State()


class SettingsStates(StatesGroup):
    waiting_location = State()
    waiting_quiet = State()
    waiting_digest_time = State()


class BulkStates(StatesGroup):
    waiting_kind = State()
    waiting_value = State()
    waiting_links = State()


def register_feature_handlers(router: Router, context: Any) -> None:
    def menu_for(message: Message) -> Any:
        actor = message.from_user
        return main_keyboard(
            is_admin=actor is not None and actor.id in context.settings.allowed_users
        )

    @router.message(StateFilter("*"), F.text == "Отмена")
    async def cancel_feature(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменено.", reply_markup=menu_for(message))

    async def show_settings(target: Message | CallbackQuery) -> None:
        actor = target.from_user
        assert actor is not None
        user = await context.database.get_user(actor.id)
        if user is None:
            if isinstance(target, Message):
                await target.answer("Сначала отправьте /start")
            return
        region = user.region_label or f"dest {context.settings.wb_destination}"
        quiet = (
            f"{format_clock(user.quiet_start_minute)}–{format_clock(user.quiet_end_minute)}"
            if user.quiet_start_minute is not None
            else "выключены"
        )
        text = (
            "⚙️ <b>Настройки</b>\n\n"
            f"Регион: {html.escape(region)}\n"
            f"Тихие часы: {quiet}\n"
            f"Дневная сводка: {'включена' if user.daily_digest_enabled else 'выключена'}, "
            f"{format_clock(user.daily_digest_minute)} ({context.settings.timezone_name})"
        )
        markup = settings_keyboard(user.daily_digest_enabled, user.quiet_start_minute is not None)
        if isinstance(target, Message):
            await target.answer(text, reply_markup=markup)
        elif isinstance(target.message, Message):
            await target.message.edit_text(text, reply_markup=markup)
            await target.answer()

    router.message.register(show_settings, Command("settings"))
    router.message.register(show_settings, F.text == "⚙️ Настройки")
    router.callback_query.register(show_settings, F.data == "settings:show")

    @router.callback_query(F.data == "settings:region")
    async def ask_region(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(SettingsStates.waiting_location)
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Нажмите кнопку и разрешите Telegram отправить текущую геопозицию. "
                "Координаты используются один раз для получения регионального dest WB.",
                reply_markup=location_keyboard(),
            )
        await callback.answer()

    @router.message(SettingsStates.waiting_location, F.location)
    async def receive_region(message: Message, state: FSMContext) -> None:
        assert message.location is not None and message.from_user is not None
        try:
            region = await context.public_client.resolve_geo(
                message.location.latitude, message.location.longitude
            )
        except WildberriesError as exc:
            await message.answer(html.escape(str(exc)))
            return
        await context.database.set_region(
            message.from_user.id,
            destination=region.destination,
            label=f"Telegram-геопозиция (dest {region.destination})",
        )
        await state.clear()
        await message.answer(
            f"✅ Регион WB обновлён: <code>{region.destination}</code>. "
            "Первая цена нового региона станет новой контрольной.",
            reply_markup=menu_for(message),
        )

    @router.callback_query(F.data == "settings:quiet")
    async def ask_quiet(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(SettingsStates.waiting_quiet)
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Введите интервал тихих часов, например <code>22:00-08:00</code>, "
                "или <code>off</code>. Часовой пояс: " + context.settings.timezone_name
            )
        await callback.answer()

    @router.message(SettingsStates.waiting_quiet, F.text)
    async def receive_quiet(message: Message, state: FSMContext) -> None:
        assert message.text is not None and message.from_user is not None
        if message.text.strip().lower() in {"off", "выкл", "нет", "-"}:
            start = end = None
        else:
            try:
                start_raw, end_raw = message.text.replace("—", "-").split("-", 1)
                start, end = parse_clock(start_raw), parse_clock(end_raw)
                if start == end:
                    raise ValueError("Начало и конец тихих часов должны различаться")
            except ValueError as exc:
                await message.answer(html.escape(str(exc)))
                return
        await context.database.set_quiet_hours(message.from_user.id, start, end)
        await state.clear()
        await message.answer("✅ Тихие часы сохранены.", reply_markup=menu_for(message))

    @router.callback_query(F.data == "settings:digest_toggle")
    async def toggle_digest(callback: CallbackQuery) -> None:
        user = await context.database.get_user(callback.from_user.id)
        if user is None:
            await callback.answer("Сначала /start", show_alert=True)
            return
        await context.database.set_digest(
            callback.from_user.id, enabled=not user.daily_digest_enabled
        )
        await show_settings(callback)

    @router.callback_query(F.data == "settings:digest_time")
    async def ask_digest_time(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(SettingsStates.waiting_digest_time)
        if isinstance(callback.message, Message):
            await callback.message.answer(
                f"Введите время сводки ЧЧ:ММ ({context.settings.timezone_name})."
            )
        await callback.answer()

    @router.message(SettingsStates.waiting_digest_time, F.text)
    async def receive_digest_time(message: Message, state: FSMContext) -> None:
        assert message.text is not None and message.from_user is not None
        try:
            minute = parse_clock(message.text)
        except ValueError as exc:
            await message.answer(html.escape(str(exc)))
            return
        await context.database.set_digest(message.from_user.id, enabled=True, minute=minute)
        await state.clear()
        await message.answer("✅ Время дневной сводки сохранено.", reply_markup=menu_for(message))

    @router.callback_query(F.data.startswith("charts:"))
    async def show_charts(callback: CallbackQuery) -> None:
        product_id = _callback_part(callback, 1)
        if product_id is None:
            return
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "Выберите период графика:", reply_markup=charts_keyboard(product_id)
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("variantedit:"))
    async def show_variants(callback: CallbackQuery) -> None:
        product_id = _callback_part(callback, 1)
        if product_id is None:
            return
        product = await context.database.get_product(callback.from_user.id, product_id)
        user = await context.database.get_user(callback.from_user.id)
        if product is None:
            await callback.answer("Товар не найден", show_alert=True)
            return
        destination = (
            user.wb_destination
            if user is not None and user.wb_destination is not None
            else context.settings.wb_destination
        )
        try:
            result = await context.public_client.fetch_many(
                [product.nm_id], destination=destination
            )
        except WildberriesError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        variants = result.variants.get(product.nm_id, [])
        if not variants:
            await callback.answer("WB не вернул варианты размера", show_alert=True)
            return
        seller = variants[0].supplier_name or "не указан"
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"Продавец: <b>{html.escape(seller)}</b>. Выберите размер:",
                reply_markup=edit_variant_keyboard(
                    product_id,
                    [
                        (item.option_id, item.size_name, item.price, item.available)
                        for item in variants
                    ],
                ),
            )
        await callback.answer()

    @router.callback_query(F.data.startswith("variantset:"))
    async def set_variant(callback: CallbackQuery) -> None:
        product_id = _callback_part(callback, 1)
        option_id = _callback_part(callback, 2)
        if product_id is None or option_id is None:
            return
        product = await context.database.get_product(callback.from_user.id, product_id)
        user = await context.database.get_user(callback.from_user.id)
        if product is None:
            await callback.answer("Товар не найден", show_alert=True)
            return
        destination = (
            user.wb_destination
            if user is not None and user.wb_destination is not None
            else context.settings.wb_destination
        )
        try:
            result = await context.public_client.fetch_many(
                [product.nm_id], destination=destination
            )
        except WildberriesError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        snapshot = (
            replace(result.products[product.nm_id], option_id=None, size_name=None)
            if option_id == 0 and product.nm_id in result.products
            else result.select(product.nm_id, VariantSelection(option_id=option_id))
        )
        if snapshot is None:
            await callback.answer("Размер больше не доступен", show_alert=True)
            return
        await context.database.set_variant(callback.from_user.id, product_id, snapshot)
        await callback.answer("Режим цены и продавец обновлены")
        if isinstance(callback.message, Message):
            mode = (
                f"размер <b>{html.escape(snapshot.size_name)}</b>"
                if snapshot.size_name
                else "<b>минимальная доступная цена</b>"
            )
            await callback.message.answer(
                f"✅ Выбран режим: {mode}; продавец "
                f"<b>{html.escape(snapshot.supplier_name or 'не указан')}</b>."
            )

    @router.callback_query(F.data.startswith("chart:"))
    async def send_chart(callback: CallbackQuery) -> None:
        product_id = _callback_part(callback, 1)
        days = _callback_part(callback, 2)
        if product_id is None or days not in {7, 30, 90}:
            return
        product = await context.database.get_product(callback.from_user.id, product_id)
        if product is None:
            await callback.answer("Товар не найден", show_alert=True)
            return
        rows = await context.database.history_since(
            callback.from_user.id, product_id, since=utcnow() - timedelta(days=days)
        )
        try:
            image = render_price_chart(rows, title=product.title, days=days)
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if isinstance(callback.message, Message):
            await callback.message.answer_photo(
                BufferedInputFile(image, filename=f"wb-{product.nm_id}-{days}d.png"),
                caption=f"📊 {html.escape(product.title)} — {days} дней",
            )
        await callback.answer()

    async def render_rules(
        callback: CallbackQuery, product_id: int, *, answer: bool = True
    ) -> None:
        product = await context.database.get_product(callback.from_user.id, product_id)
        rules = await context.database.product_rules(callback.from_user.id, product_id)
        if product is None:
            if answer:
                await callback.answer("Товар не найден", show_alert=True)
            return
        lines = [f"🔔 <b>Правила: {html.escape(product.title)}</b>"]
        lines.extend(f"{rule.id}. {html.escape(rule.label())}" for rule in rules)
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "\n".join(lines),
                reply_markup=rules_keyboard(product_id, [item.id for item in rules]),
            )
        if answer:
            await callback.answer()

    @router.callback_query(F.data.startswith("rules:"))
    async def show_rules(callback: CallbackQuery) -> None:
        product_id = _callback_part(callback, 1)
        if product_id is not None:
            await render_rules(callback, product_id)

    @router.callback_query(F.data.startswith("ruleadd:"))
    async def begin_rule(callback: CallbackQuery, state: FSMContext) -> None:
        product_id = _callback_part(callback, 1)
        if product_id is None:
            return
        await state.update_data(rule_product_id=product_id)
        await state.set_state(RuleStates.waiting_kind)
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Выберите тип нового правила:", reply_markup=threshold_keyboard()
            )
        await callback.answer()

    @router.callback_query(RuleStates.waiting_kind, F.data.startswith("addkind:"))
    async def choose_rule_kind(callback: CallbackQuery, state: FSMContext) -> None:
        try:
            kind = ThresholdKind(str(callback.data).split(":", 1)[1])
        except ValueError:
            return
        await state.update_data(rule_kind=kind.value)
        await state.set_state(RuleStates.waiting_value)
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Введите процент" if kind is ThresholdKind.PERCENT else "Введите сумму в рублях"
            )
        await callback.answer()

    @router.message(RuleStates.waiting_value, F.text)
    async def receive_rule_value(message: Message, state: FSMContext) -> None:
        assert message.text is not None and message.from_user is not None
        data = await state.get_data()
        try:
            kind = ThresholdKind(str(data["rule_kind"]))
            raw = parse_user_number(message.text)
            value = (
                percent_to_basis_points(raw)
                if kind is ThresholdKind.PERCENT
                else rubles_to_kopecks(raw)
            )
            rule = await context.database.add_rule(
                message.from_user.id,
                int(data["rule_product_id"]),
                kind,
                value,
                max_rules=context.settings.max_rules_per_product,
            )
        except (ValueError, KeyError, ProductLimitError, RuntimeError) as exc:
            await message.answer(html.escape(str(exc)))
            return
        await state.clear()
        await message.answer(f"✅ Добавлено правило: {html.escape(rule.label())}")

    @router.callback_query(F.data.startswith("ruledel:"))
    async def delete_rule(callback: CallbackQuery) -> None:
        product_id = _callback_part(callback, 1)
        rule_id = _callback_part(callback, 2)
        if product_id is None or rule_id is None:
            return
        try:
            deleted = await context.database.delete_rule(callback.from_user.id, product_id, rule_id)
        except RuntimeError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await callback.answer("Удалено" if deleted else "Правило не найдено")
        await render_rules(callback, product_id, answer=False)

    @router.callback_query(F.data.startswith("organize:"))
    async def begin_organize(callback: CallbackQuery, state: FSMContext) -> None:
        product_id = _callback_part(callback, 1)
        if product_id is None:
            return
        await state.update_data(organize_product_id=product_id)
        await state.set_state(OrganizeStates.waiting_folder)
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Введите название папки или <code>-</code>, чтобы убрать папку:"
            )
        await callback.answer()

    @router.message(OrganizeStates.waiting_folder, F.text)
    async def receive_folder(message: Message, state: FSMContext) -> None:
        assert message.text is not None
        folder = message.text.strip()
        await state.update_data(folder=None if folder == "-" else folder[:100])
        await state.set_state(OrganizeStates.waiting_tags)
        await message.answer("Введите теги через запятую или <code>-</code> без тегов:")

    @router.message(OrganizeStates.waiting_tags, F.text)
    async def receive_tags(message: Message, state: FSMContext) -> None:
        assert message.text is not None and message.from_user is not None
        data = await state.get_data()
        tags = [] if message.text.strip() == "-" else message.text.split(",")
        await context.database.organize_product(
            message.from_user.id,
            int(data["organize_product_id"]),
            folder=data.get("folder"),
            tags=tags,
        )
        await state.clear()
        await message.answer("✅ Папка и теги сохранены.", reply_markup=menu_for(message))

    @router.message(Command("folders"))
    async def folders(message: Message) -> None:
        assert message.from_user is not None
        folder_counts, tag_counts = await context.database.organization_summary(
            message.from_user.id
        )
        folder_text = (
            ", ".join(f"{html.escape(k)} ({v})" for k, v in folder_counts.items()) or "нет"
        )
        tag_text = ", ".join(f"#{html.escape(k)} ({v})" for k, v in tag_counts.items()) or "нет"
        await message.answer(f"📁 <b>Папки:</b> {folder_text}\n🏷 <b>Теги:</b> {tag_text}")

    @router.message(Command("folder"))
    async def filter_folder(message: Message) -> None:
        assert message.from_user is not None
        name = (message.text or "").partition(" ")[2].strip()
        if not name:
            await message.answer("Использование: <code>/folder Название</code>")
            return
        products, total = await context.database.list_products(
            message.from_user.id, limit=100, folder=name
        )
        await message.answer(_filtered_products_text(f"Папка {name}", products, total))

    @router.message(Command("tag"))
    async def filter_tag(message: Message) -> None:
        assert message.from_user is not None
        name = (message.text or "").partition(" ")[2].strip().lstrip("#")
        if not name:
            await message.answer("Использование: <code>/tag скидки</code>")
            return
        products, total = await context.database.list_products(
            message.from_user.id, limit=100, tag=name
        )
        await message.answer(_filtered_products_text(f"Тег #{name}", products, total))

    async def show_export(target: Message | CallbackQuery) -> None:
        text = "Выберите формат экспорта. Сессии и секреты в файл не включаются."
        if isinstance(target, Message):
            await target.answer(text, reply_markup=export_keyboard())
        elif isinstance(target.message, Message):
            await target.message.answer(text, reply_markup=export_keyboard())
            await target.answer()

    router.message.register(show_export, Command("export"))
    router.callback_query.register(show_export, F.data == "export:show")

    @router.callback_query(F.data.in_({"export:csv", "export:json"}))
    async def send_export(callback: CallbackQuery) -> None:
        output_format = str(callback.data).split(":", 1)[1]
        products, _ = await context.database.list_products(callback.from_user.id, limit=10_000)
        rules = {
            product.id: await context.database.product_rules(callback.from_user.id, product.id)
            for product in products
        }
        content = export_products(products, rules, output_format=output_format)
        if isinstance(callback.message, Message):
            await callback.message.answer_document(
                BufferedInputFile(content, filename=f"wb-price-bot-export.{output_format}")
            )
        await callback.answer()

    async def begin_bulk(target: Message | CallbackQuery, state: FSMContext) -> None:
        await state.set_state(BulkStates.waiting_kind)
        text = "Выберите одно начальное правило для импортируемых товаров:"
        if isinstance(target, Message):
            await target.answer(text, reply_markup=threshold_keyboard())
        elif isinstance(target.message, Message):
            await target.message.answer(text, reply_markup=threshold_keyboard())
            await target.answer()

    router.message.register(begin_bulk, Command("import"))
    router.callback_query.register(begin_bulk, F.data == "bulk:start")

    @router.callback_query(BulkStates.waiting_kind, F.data.startswith("addkind:"))
    async def choose_bulk_kind(callback: CallbackQuery, state: FSMContext) -> None:
        try:
            kind = ThresholdKind(str(callback.data).split(":", 1)[1])
        except ValueError:
            return
        await state.update_data(bulk_kind=kind.value)
        await state.set_state(BulkStates.waiting_value)
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Введите процент" if kind is ThresholdKind.PERCENT else "Введите сумму в рублях"
            )
        await callback.answer()

    @router.message(BulkStates.waiting_value, F.text)
    async def receive_bulk_value(message: Message, state: FSMContext) -> None:
        assert message.text is not None
        data = await state.get_data()
        try:
            kind = ThresholdKind(str(data["bulk_kind"]))
            raw = parse_user_number(message.text)
            value = (
                percent_to_basis_points(raw)
                if kind is ThresholdKind.PERCENT
                else rubles_to_kopecks(raw)
            )
        except (ValueError, KeyError) as exc:
            await message.answer(html.escape(str(exc)))
            return
        await state.update_data(bulk_value=value)
        await state.set_state(BulkStates.waiting_links)
        await message.answer(
            f"Отправьте до {context.settings.max_bulk_import} ссылок/артикулов — "
            "по одному в строке или TXT-файлом. Размер по умолчанию можно изменить позднее."
        )

    @router.message(BulkStates.waiting_links, F.text | F.document)
    async def receive_bulk_links(message: Message, state: FSMContext) -> None:
        assert message.from_user is not None
        try:
            raw = await _message_text(message)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        candidates = [item.strip() for item in raw.replace(";", "\n").splitlines() if item.strip()]
        if not candidates:
            await message.answer("Список пуст.")
            return
        if len(candidates) > context.settings.max_bulk_import:
            await message.answer(
                f"Слишком много строк; максимум {context.settings.max_bulk_import}."
            )
            return
        nm_ids: list[int] = []
        invalid = 0
        for candidate in candidates:
            try:
                nm_id = await context.public_client.resolve_reference(candidate)
            except WildberriesError:
                invalid += 1
                continue
            if nm_id not in nm_ids:
                nm_ids.append(nm_id)
        if not nm_ids:
            await message.answer("Не найдено ни одной ссылки Wildberries.")
            return
        user = await context.database.get_user(message.from_user.id)
        destination = (
            user.wb_destination
            if user is not None and user.wb_destination is not None
            else context.settings.wb_destination
        )
        status = await message.answer(f"Получаю {len(nm_ids)} карточек…")
        try:
            result = await context.public_client.fetch_many(nm_ids, destination=destination)
        except WildberriesError as public_exc:
            if not context.licensed_client.enabled:
                await status.edit_text(f"Импорт остановлен: {html.escape(str(public_exc))}")
                return
            try:
                result = await context.licensed_client.fetch_many(nm_ids)
            except WildberriesError as licensed_exc:
                await status.edit_text(
                    f"Оба источника недоступны: {html.escape(str(licensed_exc))}"
                )
                return
        data = await state.get_data()
        kind = ThresholdKind(str(data["bulk_kind"]))
        value = int(data["bulk_value"])
        added = duplicates = missing = 0
        for nm_id in nm_ids:
            snapshot = result.products.get(nm_id)
            if snapshot is None:
                missing += 1
                continue
            snapshot = replace(snapshot, option_id=None, size_name=None)
            try:
                await context.database.add_product(
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
            except ProductAlreadyExistsError:
                duplicates += 1
            except ProductLimitError:
                break
            else:
                added += 1
        await state.clear()
        await status.edit_text(
            f"✅ Импорт завершён. Добавлено: {added}; уже были: {duplicates}; "
            f"не найдены: {missing}; неверных строк: {invalid}."
        )


def _callback_part(callback: CallbackQuery, index: int) -> int | None:
    try:
        return int(str(callback.data).split(":")[index])
    except (ValueError, IndexError):
        return None


async def _message_text(message: Message) -> str:
    if message.text:
        return message.text
    if message.document is None:
        return ""
    if message.document.file_size and message.document.file_size > 1_000_000:
        raise ValueError("Файл больше 1 МБ")
    if message.document.file_name and not message.document.file_name.lower().endswith(".txt"):
        raise ValueError("Для импорта поддерживается TXT-файл")
    output = io.BytesIO()
    bot = message.bot
    if bot is None:
        raise ValueError("Telegram Bot API недоступен")
    await bot.download(message.document, destination=output)
    try:
        return output.getvalue().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("TXT должен быть в UTF-8") from exc


def _filtered_products_text(title: str, products: list[Any], total: int) -> str:
    lines = [f"<b>{html.escape(title)}</b> — {total}"]
    for product in products[:50]:
        lines.append(
            f'• <a href="{html.escape(product.canonical_url, quote=True)}">'
            f"{html.escape(product.title[:70])}</a>"
        )
    if total == 0:
        lines.append("Ничего не найдено.")
    return "\n".join(lines)
