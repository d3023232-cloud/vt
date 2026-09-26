"""Хендлеры команды /inventory: просмотр подиков, экипировка, утилизация сломанных."""

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from config import config
from keyboards.inventory_kb import inventory_keyboard
from services import vape_service
from utils.helpers import format_vape

logger = logging.getLogger(__name__)
router: Router = Router()


async def build_inventory_text(user_id: int) -> tuple[str, list[dict[str, Any]]]:
    """Формирует текст инвентаря и список подиков игрока.

    Args:
        user_id: Telegram ID игрока.

    Returns:
        (HTML-текст, список подиков).
    """
    vapes = await vape_service.get_user_vapes(user_id)
    if not vapes:
        return (
            "🎒 Инвентарь пуст.\n"
            f"Места в инвентаре: 0/{config.max_vapes_per_user}",
            [],
        )
    blocks: list[str] = [format_vape(v) for v in vapes]
    header: str = f"🎒 <b>Инвентарь</b> ({len(vapes)}/{config.max_vapes_per_user})"
    return header + "\n\n" + "\n\n".join(blocks), vapes


async def render_inventory(callback: CallbackQuery) -> None:
    """Рендерит экран инвентаря по callback (используется и с главного экрана)."""
    text, vapes = await build_inventory_text(callback.from_user.id)
    try:
        await callback.message.edit_text(
            text, reply_markup=inventory_keyboard(vapes), parse_mode="HTML"
        )
    except Exception as exc:
        logger.warning("Не удалось отредактировать сообщение инвентаря: %s", exc)
        await callback.message.answer(text, reply_markup=inventory_keyboard(vapes), parse_mode="HTML")
    await callback.answer()


@router.message(Command("inventory"))
async def cmd_inventory(message: Message) -> None:
    """Команда /inventory — показывает все подики игрока (не более MAX_VAPES_PER_USER)."""
    try:
        user = await vape_service.get_user(message.from_user.id)
        if user is None:
            await message.answer("Ты не зарегистрирован. Используй /start")
            return
        text, vapes = await build_inventory_text(message.from_user.id)
        await message.answer(text, reply_markup=inventory_keyboard(vapes), parse_mode="HTML")
    except Exception:
        logger.exception("Ошибка в /inventory")
        await message.answer("⚠️ Ошибка, попробуй позже.")


@router.callback_query(F.data.startswith("equip:"))
async def cb_equip(callback: CallbackQuery) -> None:
    """Кнопка «Экипировать»: нельзя экипировать сломанный/чужой подик."""
    try:
        raw: str = callback.data or ""
        _, _, id_part = raw.partition(":")
        if not id_part.isdigit():
            await callback.answer("Некорректные данные кнопки.", show_alert=True)
            return
        vape_id: int = int(id_part)
        ok, msg = await vape_service.equip_vape(callback.from_user.id, vape_id)
        # Обновляем экран инвентаря
        text, vapes = await build_inventory_text(callback.from_user.id)
        try:
            await callback.message.edit_text(
                text, reply_markup=inventory_keyboard(vapes), parse_mode="HTML"
            )
        except Exception:
            pass
        await callback.answer(msg, show_alert=not ok)
    except Exception:
        logger.exception("Ошибка в кнопке equip")
        await callback.answer("⚠️ Ошибка, попробуй позже.", show_alert=True)


@router.callback_query(F.data.startswith("discard:"))
async def cb_discard(callback: CallbackQuery) -> None:
    """Кнопка «Выбросить»: удаляет только сломанный подик."""
    try:
        raw: str = callback.data or ""
        _, _, id_part = raw.partition(":")
        if not id_part.isdigit():
            await callback.answer("Некорректные данные кнопки.", show_alert=True)
            return
        vape_id: int = int(id_part)
        ok, msg = await vape_service.discard_vape(callback.from_user.id, vape_id)
        text, vapes = await build_inventory_text(callback.from_user.id)
        try:
            await callback.message.edit_text(
                text, reply_markup=inventory_keyboard(vapes), parse_mode="HTML"
            )
        except Exception:
            pass
        await callback.answer(msg, show_alert=not ok)
    except Exception:
        logger.exception("Ошибка в кнопке discard")
        await callback.answer("⚠️ Ошибка, попробуй позже.", show_alert=True)
