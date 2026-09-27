"""Хендлеры команды /start, главного экрана и кнопок навигации."""

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import config
from keyboards.main_kb import auction_channel_keyboard, main_keyboard
from services import auction_service, vape_service
from utils.helpers import channel_mention, format_stats_block, format_vape

logger = logging.getLogger(__name__)
router: Router = Router()


async def build_main_text(user_id: int) -> str:
    """Собирает текст главного экрана игрока: баланс, подик, статус аукциона.

    Args:
        user_id: Telegram ID игрока.

    Returns:
        HTML-текст главного экрана.
    """
    user = await vape_service.get_user(user_id)
    if user is None:
        return "Ты не зарегистрирован. Используй /start"
    vape = await vape_service.get_equipped_vape(user_id)
    lines: list[str] = [
        "💨 <b>Vape Tycoon — экономическая игра</b>",
        f"👤 Игрок: <code>{user['user_id']}</code>"
        + (f" (@{user['username']})" if user.get("username") else ""),
        f"⚖️ Баланс: <b>{user['balance']} Паров</b>",
    ]
    if vape is not None:
        lines.append("")
        lines.append(format_vape(vape))
        ready: int = int(vape["current_puffs"]) * int(vape["puffs_per_vapor"]) * int(vape["multiplier"])
        lines.append(f"   💨 Готово к сбору: ≈ {ready} Паров")
    else:
        lines.append("\n⚠️ У тебя нет экипированного подика! Загляни в /inventory.")

    auction = await auction_service.get_active_auction()
    lines.append("")
    if auction is not None:
        status_map = {"announced": "⏳ анонс", "active": "🔥 торги идут"}
        lines.append(
            f"🔨 Активный аукцион #{auction['id']} «{auction['lot_name']}» — "
            f"{status_map.get(auction['status'], auction['status'])}"
        )
        lines.append(f"📢 Канал: {channel_mention()}")
    else:
        lines.append(f"🔨 Сейчас нет активных аукционов. Следи за {channel_mention()}")
    return "\n".join(lines)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Команда /start: регистрация нового игрока либо загрузка существующего.

    Существующий пользователь НИКОГДА не удаляется и не пересоздаётся.
    """
    try:
        await state.clear()  # сбрасываем возможные зависшие FSM-состояния
        user_id: int = message.from_user.id
        username: str | None = message.from_user.username
        await vape_service.register_user(user_id, username)
        text: str = await build_main_text(user_id)
        await message.answer(text, reply_markup=main_keyboard(), parse_mode="HTML")
    except Exception:
        logger.exception("Ошибка в /start")
        await message.answer("⚠️ Произошла ошибка, попробуй ещё раз позже.")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Справка по игре."""
    await message.answer(
        "🎮 <b>Vape Tycoon — как играть</b>\n\n"
        "💨 Твой подик каждую минуту вырабатывает затяжки (power зат./мин).\n"
        f"• Нажимай «Забрать Пары», когда бак полон: 1 затяжка = "
        f"{config.puffs_per_vapor} Паров × множитель.\n"
        "• После сбора бак регенерируется, а подик изнашивается.\n"
        "• Сломанный подик выбрасывается, новый можно выиграть на аукционе.\n\n"
        "Команды: /start — главный экран, /inventory — инвентарь, "
        "/auction — информация об аукционе.",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "collect")
async def cb_collect(callback: CallbackQuery) -> None:
    """Кнопка «Забрать Пары» с главного экрана."""
    try:
        status, text = await vape_service.collect_vapors(callback.from_user.id)
        if status == "ok":
            # Обновляем главный экран после успешного сбора
            await callback.message.edit_text(
                await build_main_text(callback.from_user.id),
                reply_markup=main_keyboard(), parse_mode="HTML",
            )
        await callback.answer(text, show_alert=status != "ok")
    except Exception:
        logger.exception("Ошибка в кнопке collect")
        await callback.answer("⚠️ Ошибка, попробуй позже.", show_alert=True)


@router.callback_query(F.data == "show_inventory")
async def cb_show_inventory(callback: CallbackQuery) -> None:
    """Кнопка «Инвентарь» с главного экрана."""
    from handlers.inventory import render_inventory
    try:
        await render_inventory(callback)
    except Exception:
        logger.exception("Ошибка в кнопке show_inventory")
        await callback.answer("⚠️ Ошибка, попробуй позже.", show_alert=True)


@router.callback_query(F.data == "show_auction")
async def cb_show_auction(callback: CallbackQuery) -> None:
    """Кнопка «Аукцион»: показывает состояние текущего аукциона."""
    try:
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
                + f"\n💰 Стартовая цена: {auction['start_price']} Паров\n\n"
                f"Жми кнопки [+100]/[+250]/[+500]/[+1000] под постом в {channel_mention()}"
            )
        await callback.message.edit_text(
            text, reply_markup=auction_channel_keyboard(config.auction_channel_username),
            parse_mode="HTML",
        )
        await callback.answer()
    except Exception:
        logger.exception("Ошибка в кнопке show_auction")
        await callback.answer("⚠️ Ошибка, попробуй позже.", show_alert=True)


@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery) -> None:
    """Кнопка «Назад» — возврат на главный экран."""
    try:
        await callback.message.edit_text(
            await build_main_text(callback.from_user.id),
            reply_markup=main_keyboard(), parse_mode="HTML",
        )
        await callback.answer()
    except Exception:
        logger.exception("Ошибка в кнопке back_main")
        await callback.answer("⚠️ Ошибка, попробуй позже.", show_alert=True)
