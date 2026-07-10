from __future__ import annotations

from math import ceil

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .models import Product


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить товар"), KeyboardButton(text="📦 Мои товары")],
            [KeyboardButton(text="👤 Аккаунт WB"), KeyboardButton(text="🩺 Статус")],
            [KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
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
            [InlineKeyboardButton(text=toggle, callback_data=f"toggle:{product.id}")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"deleteask:{product.id}")],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="products:0")],
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


def account_delete_confirmation() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, отключить", callback_data="account:delete"),
                InlineKeyboardButton(text="Отмена", callback_data="account:show"),
            ]
        ]
    )
