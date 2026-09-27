"""Хендлеры аукциона для игроков: команда /auction и кнопки ставок в канале."""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from config import config
from keyboards.main_kb import auction_channel_keyboard
from services import auction_service, vape_service
from utils.helpers import channel_mention, format_stats_block

logger = logging.getLogger(__name__)
router: Router = Router()


@router.message(Command("auction"))
async def cmd_auction(message: Message) -> None:
    """Команда /auction — информация о текущем аукционе и ссылка на канал."""
    try:
        user = await vape_service.get_user(message.from_user.id)
        if user is None:
            await message.answer("Ты не зарегистрирован. Используй /start")
            return
        auction = await auction_service.get_active_auction()
        if auction is None:
            text: str = (
                "🔨 Сейчас нет активных аукционов.\n"
                f"Следи за анонсами в канале {channel_mention()}!"
            )
        else:
            status_map = {"announced": "⏳ Анонс — торги скоро начнутся",
                          "active": "🔥 Торги идут прямо сейчас!"}
            text = (
                f"🔨 <b>Аукцион #{auction['id']}</b> — "
                f"{status_map.get(auction['status'], auction['status'])}\n"
                + format_stats_block(
                    name=auction["lot_name"],
                    multiplier=auction["multiplier"],
                    power=auction["power"],
                    max_puffs=auction["max_puffs"],
                    condition=auction["condition"],
                    quantity=auction["quantity"],
                    lots_sold=auction["lots_sold"],
                    indent="   ",
                )
                + f"\n💰 Стартовая цена: {auction['start_price']} Паров\n"
                f"⚖️ Твой баланс: {user['balance']} Паров\n\n"
                f"Делай ставки кнопками [+100]/[+250]/[+500]/[+1000] под постом в {channel_mention()}"
            )
        await message.answer(
            text, reply_markup=auction_channel_keyboard(config.auction_channel_username),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Ошибка в /auction")
        await message.answer("⚠️ Ошибка, попробуй позже.")


@router.callback_query(F.data.startswith("bid:"))
async def cb_bid(callback: CallbackQuery) -> None:
    """Кнопки ставок [+100], [+250], [+500], [+1000] в посте аукциона в канале.

    Проверка регистрации и баланса выполняется в сервисе place_bid
    внутри одной транзакции БД.
    """
    try:
        raw: str = callback.data or ""
        _, _, step_part = raw.partition(":")
        ok, msg = await auction_service.place_bid(callback.bot, callback.from_user.id, step_part)
        # Ответ показываем всем одинаково; при успехе — без alert, при отказе — alert
        await callback.answer(msg, show_alert=not ok)
    except Exception:
        logger.exception("Ошибка в кнопке bid")
        await callback.answer("⚠️ Ошибка ставки, попробуй позже.", show_alert=True)
