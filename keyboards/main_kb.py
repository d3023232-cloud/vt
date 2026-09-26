"""Главная инлайн-клавиатура: [Забрать Пары], [Инвентарь], [Аукцион]."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура главного экрана после /start."""
    builder = InlineKeyboardBuilder()
    builder.button(text="💨 Забрать Пары", callback_data="collect")
    builder.button(text="🎒 Инвентарь", callback_data="show_inventory")
    builder.button(text="🔨 Аукцион", callback_data="show_auction")
    builder.adjust(1)  # кнопки в одну колонку
    return builder.as_markup()


def auction_channel_keyboard(channel_username: str) -> InlineKeyboardMarkup:
    """Клавиатура со ссылкой на канал аукционов (username из .env)."""
    builder = InlineKeyboardBuilder()
    if channel_username:
        builder.button(
            text=f"📢 Открыть канал @{channel_username.lstrip('@')}",
            url=f"https://t.me/{channel_username.lstrip('@')}",
        )
    builder.button(text="⬅️ Назад", callback_data="back_main")
    builder.adjust(1)
    return builder.as_markup()
