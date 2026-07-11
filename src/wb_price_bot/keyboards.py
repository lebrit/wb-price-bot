from __future__ import annotations

from math import ceil

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from .models import Product


def main_keyboard(*, is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="➕ Добавить товар"), KeyboardButton(text="📦 Мои товары")],
        [KeyboardButton(text="👤 Аккаунт WB"), KeyboardButton(text="🩺 Статус")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="❓ Помощь")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🛠 Админ-панель")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить статистику", callback_data="admin:stats")],
            [
                InlineKeyboardButton(text="⏳ Заявки", callback_data="admin:pending"),
                InlineKeyboardButton(text="✅ Активные", callback_data="admin:approved"),
            ],
            [InlineKeyboardButton(text="⛔ Заблокированные", callback_data="admin:blocked")],
        ]
    )


def threshold_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📉 На процент", callback_data="addkind:percent")],
            [InlineKeyboardButton(text="💰 На сумму", callback_data="addkind:amount")],
            [InlineKeyboardButton(text="🎯 До целевой цены", callback_data="addkind:target")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")],
        ]
    )


def variant_keyboard(
    variants: list[tuple[int, str, int | None, bool]],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🔎 Минимальная доступная цена", callback_data="addvariant:0")]
    ]
    for option_id, name, price, available in variants[:30]:
        state = "🟢" if available else "⚪"
        price_label = f" — {price / 100:g} ₽" if price else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{state} {name}{price_label}",
                    callback_data=f"addvariant:{option_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def products_keyboard(
    products: list[Product], page: int, total: int, per_page: int
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for product in products:
        icon = "🟢" if product.is_active else "⏸"
        title = product.title if len(product.title) <= 36 else f"{product.title[:33]}…"
        rows.append(
            [InlineKeyboardButton(text=f"{icon} {title}", callback_data=f"product:{product.id}")]
        )
    pages = max(1, ceil(total / per_page))
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️", callback_data=f"products:{page - 1}"))
    navigation.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton(text="➡️", callback_data=f"products:{page + 1}"))
    rows.append(navigation)
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="add:start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_keyboard(product: Product) -> InlineKeyboardMarkup:
    toggle = "⏸ Приостановить" if product.is_active else "▶️ Возобновить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Проверить", callback_data=f"productcheck:{product.id}"
                ),
                InlineKeyboardButton(text="📈 История", callback_data=f"history:{product.id}"),
            ],
            [
                InlineKeyboardButton(text="📊 График", callback_data=f"charts:{product.id}"),
                InlineKeyboardButton(text="🔔 Правила", callback_data=f"rules:{product.id}"),
            ],
            [
                InlineKeyboardButton(
                    text="📐 Размер и продавец", callback_data=f"variantedit:{product.id}"
                )
            ],
            [InlineKeyboardButton(text="🏷 Организация", callback_data=f"organize:{product.id}")],
            [InlineKeyboardButton(text=toggle, callback_data=f"toggle:{product.id}")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"deleteask:{product.id}")],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="products:0")],
        ]
    )


def edit_variant_keyboard(
    product_id: int, variants: list[tuple[int, str, int | None, bool]]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="🔎 Минимальная доступная цена",
                callback_data=f"variantset:{product_id}:0",
            )
        ]
    ]
    for option_id, name, price, available in variants[:30]:
        state = "🟢" if available else "⚪"
        price_label = f" — {price / 100:g} ₽" if price else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{state} {name}{price_label}",
                    callback_data=f"variantset:{product_id}:{option_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ К товару", callback_data=f"product:{product_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def charts_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="7 дней", callback_data=f"chart:{product_id}:7"),
                InlineKeyboardButton(text="30 дней", callback_data=f"chart:{product_id}:30"),
                InlineKeyboardButton(text="90 дней", callback_data=f"chart:{product_id}:90"),
            ],
            [InlineKeyboardButton(text="⬅️ К товару", callback_data=f"product:{product_id}")],
        ]
    )


def rules_keyboard(product_id: int, rule_ids: list[int]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"🗑 Удалить правило {rule_id}",
                callback_data=f"ruledel:{product_id}:{rule_id}",
            )
        ]
        for rule_id in rule_ids
    ]
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="➕ Добавить правило", callback_data=f"ruleadd:{product_id}"
                )
            ],
            [InlineKeyboardButton(text="⬅️ К товару", callback_data=f"product:{product_id}")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_keyboard(digest_enabled: bool, quiet_enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📍 Регион по геопозиции", callback_data="settings:region")],
            [
                InlineKeyboardButton(
                    text="🌙 Изменить тихие часы" if quiet_enabled else "🌙 Включить тихие часы",
                    callback_data="settings:quiet",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Выключить сводку" if digest_enabled else "📋 Включить дневную сводку",
                    callback_data="settings:digest_toggle",
                ),
                InlineKeyboardButton(text="🕘 Время сводки", callback_data="settings:digest_time"),
            ],
            [InlineKeyboardButton(text="📥 Массовый импорт", callback_data="bulk:start")],
            [InlineKeyboardButton(text="📤 Экспорт", callback_data="export:show")],
        ]
    )


def location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить геопозицию", request_location=True)],
            [KeyboardButton(text="Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def export_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="CSV", callback_data="export:csv"),
                InlineKeyboardButton(text="JSON", callback_data="export:json"),
            ]
        ]
    )


def delete_confirmation(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить", callback_data=f"deleteconfirm:{product_id}"
                ),
                InlineKeyboardButton(text="Отмена", callback_data=f"product:{product_id}"),
            ]
        ]
    )


def account_keyboard(connected: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔐 Подключить / обновить", callback_data="account:warning")]
    ]
    if connected:
        rows.extend(
            [
                [InlineKeyboardButton(text="🧪 Проверить цену", callback_data="account:test")],
                [InlineKeyboardButton(text="🗑 Отключить", callback_data="account:deleteask")],
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_warning_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Понимаю риск, продолжить", callback_data="account:accept")],
            [InlineKeyboardButton(text="Отмена", callback_data="account:show")],
        ]
    )


def account_auth_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🌐 Открыть защищённое окно WB",
                    web_app=WebAppInfo(url=url),
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data="account:show")],
        ]
    )


def access_review_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Разрешить", callback_data=f"access:approve:{telegram_id}"
                ),
                InlineKeyboardButton(
                    text="⛔ Отклонить", callback_data=f"access:block:{telegram_id}"
                ),
            ]
        ]
    )


def account_delete_confirmation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, отключить", callback_data="account:delete"),
                InlineKeyboardButton(text="Отмена", callback_data="account:show"),
            ]
        ]
    )
