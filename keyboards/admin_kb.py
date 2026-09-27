"""Клавиатуры админ-панели и подтверждения запуска аукциона."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def admin_keyboard() -> InlineKeyboardMarkup:
    """Главное меню админ-панели: аукцион + выдача/изъятие подиков + статистика."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать аукцион", callback_data="admin_create")
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="❌ Отменить аукцион", callback_data="admin_cancel")
    builder.button(text="🎁 Выдать подик", callback_data="admin_give")
    builder.button(text="⛏ Забрать подики", callback_data="admin_remove")
    builder.adjust(1)
    return builder.as_markup()


def admin_back_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с одной кнопкой «⬅️ Назад» в админ-панель."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад в админ-панель", callback_data="back_admin")
    builder.adjust(1)
    return builder.as_markup()


class AuctionConfirmCallback(CallbackData, prefix="confirm"):
    """Колбэк подтверждения запуска аукциона (хранит id предпросмотра)."""

    draft_id: int


def auction_confirm_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения: [Запустить аукцион] / [Отмена]."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚀 Запустить аукцион",
        callback_data=AuctionConfirmCallback(draft_id=draft_id).pack(),
    )
    builder.button(text="🛑 Отмена", callback_data="admin_cancel_wizard")
    builder.adjust(1)
    return builder.as_markup()
