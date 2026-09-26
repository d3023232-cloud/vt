"""Клавиатура инвентаря: кнопки «Экипировать» и «Выбросить» для каждого подика."""

from typing import Any

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def inventory_keyboard(vapes: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Строит клавиатуру по списку подиков.

    Правила:
        - "Экипировать" показывается только если подик не экипирован и не сломан;
        - "Выбросить" показывается только если подик сломан.

    Args:
        vapes: список словарей-подиков из таблицы vapes.

    Returns:
        Готовая инлайн-клавиатура.
    """
    builder = InlineKeyboardBuilder()
    for vape in vapes:
        vape_id: int = int(vape["id"])
        is_broken: bool = bool(vape["is_broken"])
        is_equipped: bool = bool(vape["is_equipped"])
        row_buttons: list[Any] = []
        if not is_broken and not is_equipped:
            row_buttons.append(
                InlineKeyboardButton(
                    text=f"🔧 Экипировать #{vape_id}", callback_data=f"equip:{vape_id}"
                )
            )
        if is_broken:
            row_buttons.append(
                InlineKeyboardButton(
                    text=f"🗑 Выбросить #{vape_id}", callback_data=f"discard:{vape_id}"
                )
            )
        if row_buttons:
            builder.row(*row_buttons)
    # Кнопка возврата в главное меню
    builder.button(text="⬅️ Назад", callback_data="back_main")
    return builder.as_markup()
