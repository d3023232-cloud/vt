"""Клавиатуры админ-панели и подтверждения запуска аукциона."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def admin_keyboard() -> InlineKeyboardMarkup:
    """Главное меню админ-панели: [Создать аукцион], [Статистика], [Отменить аукцион]."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать аукцион", callback_data="admin_create")
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="❌ Отменить аукцион", callback_data="admin_cancel")
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
