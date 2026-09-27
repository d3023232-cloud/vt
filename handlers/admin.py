"""Админ-хендлеры: /admin панель, мастер создания аукциона (FSM 3 шага),
выдача/изъятие подиков (FSM), статистика, отмена.

Доступ к этому модулю защищён middleware AdminCheckMiddleware:
команда /admin и админ-колбэки обрабатываются только для ADMIN_ID из .env.
"""

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import config
from keyboards.admin_kb import admin_back_keyboard, admin_keyboard, auction_confirm_keyboard
from services import auction_service, scheduler_service, vape_service
from utils.helpers import channel_mention, format_stats_block

logger = logging.getLogger(__name__)
router: Router = Router()


class AuctionCreateStates(StatesGroup):
    """FSM-состояния мастера создания аукциона (три шага)."""

    waiting_name = State()      # Шаг 1/3: название лота
    waiting_params = State()    # Шаг 2/3: пять чисел через пробел
    waiting_price = State()     # Шаг 3/3: стартовая цена


class VapeGiveStates(StatesGroup):
    """FSM-состояния мастера выдачи подика игроку (два шага)."""

    waiting_user = State()   # Шаг 1/2: Telegram ID получателя
    waiting_params = State() # Шаг 2/2: название + пять чисел через пробел


class VapeRemoveStates(StatesGroup):
    """FSM-состояния мастера изъятия подика у игрока (один шаг)."""

    waiting_user = State()   # Ввод Telegram ID игрока


@router.message(Command("admin"))
async def cmd_admin(message: Message, is_admin: bool) -> None:
    """Команда /admin — открывает админ-панель (только ADMIN_ID, см. middleware)."""
    if not is_admin:
        # Middleware уже должен был пропустить сюда только админа — двойная защита
        return
    try:
        await message.answer(
            f"🛠 <b>Админ-панель Vape Tycoon</b>\n\n"
            f"Канал аукционов: {channel_mention()} (<code>{config.auction_channel_id}</code>)\n"
            f"Анонс → старт: {config.auction_announce_minutes} мин | "
            f"Таймер ставки: {config.bid_timer_seconds} сек | "
            f"Перерыв между лотами: {config.lot_break_minutes} мин",
            reply_markup=admin_keyboard(), parse_mode="HTML",
        )
    except Exception:
        logger.exception("Ошибка в /admin")


@router.callback_query(F.data == "admin_create")
async def cb_admin_create(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 1/3: запрашиваем название лота."""
    try:
        if await auction_service.has_active_auction():
            await callback.answer("⛔ Уже есть активный аукцион — сначала заверши или отмени его.",
                                  show_alert=True)
            return
        await state.set_state(AuctionCreateStates.waiting_name)
        await callback.message.edit_text(
            "➕ <b>Создание аукциона — шаг 1 из 3</b>\n\n"
            "Введи <b>название лота</b> (например: 'Ghost Mod X'):",
            parse_mode="HTML",
        )
        await callback.answer()
    except Exception:
        logger.exception("Ошибка admin_create")
        await callback.answer("⚠️ Ошибка.", show_alert=True)


@router.message(AuctionCreateStates.waiting_name, F.text)
async def step_name(message: Message, state: FSMContext) -> None:
    """Обрабатывает ввод названия лота, переходит к шагу 2/3."""
    try:
        name: str = (message.text or "").strip()
        if not name or len(name) > 64:
            await message.answer("⚠️ Название должно быть от 1 до 64 символов. Попробуй ещё раз.")
            return
        await state.update_data(lot_name=name)
        await state.set_state(AuctionCreateStates.waiting_params)
        await message.answer(
            "➕ <b>Создание аукциона — шаг 2 из 3</b>\n\n"
            "Введи параметры в формате:\n"
            "<code>множитель / мощность / макс_затяжек / состояние% / кол-во_штук</code>\n\n"
            "Пример: <code>7 35 700 100 3</code>\n\n"
            "Диапазоны: множитель 1..100, мощность 1..1000, бак 10..100000, "
            "состояние 1..100%, количество 1..10.",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Ошибка шага name")
        await state.clear()
        await message.answer("⚠️ Ошибка, мастер прерван.")


def _parse_params(raw: str) -> tuple[bool, str, dict[str, int]]:
    """Парсит и валидирует строку из пяти чисел шага 2/3.

    Args:
        raw: пользовательский ввод.

    Returns:
        (успех, сообщение об ошибке, словарь параметров).
    """
    parts: list[str] = raw.split()
    if len(parts) != 5:
        return False, f"Ожидается ровно 5 чисел через пробел, получено {len(parts)}.", {}
    numbers: list[int] = []
    for part in parts:
        try:
            numbers.append(int(part))
        except ValueError:
            return False, f"«{part}» не является целым числом.", {}
    names = ("multiplier", "power", "max_puffs", "condition", "quantity")
    for name, value in zip(names, numbers):
        low, high = auction_service.LOT_LIMITS[name]
        if not low <= value <= high:
            return False, f"{name}={value} вне диапазона {low}..{high}.", {}
    params: dict[str, int] = dict(zip(names, numbers))
    return True, "", params


@router.message(AuctionCreateStates.waiting_params, F.text)
async def step_params(message: Message, state: FSMContext) -> None:
    """Валидирует пять чисел, показывает превью, переходит к шагу 3/3."""
    try:
        ok, err, params = _parse_params((message.text or "").strip())
        if not ok:
            await message.answer(f"⚠️ {err}\nФормат: <code>7 35 700 100 3</code>", parse_mode="HTML")
            return
        data = await state.get_data()
        await state.update_data(**params)
        await state.set_state(AuctionCreateStates.waiting_price)
        preview: str = (
            "➕ <b>Создание аукциона — шаг 3 из 3</b>\n\n"
            f"📦 Лот: <b>{data['lot_name']}</b>\n"
            + format_stats_block(
                multiplier=params["multiplier"],
                power=params["power"],
                max_puffs=params["max_puffs"],
                condition=params["condition"],
                quantity=params["quantity"],
                indent="   ",
            )
            + "\n\nТеперь введи <b>стартовую цену</b> (целое число больше 0):"
        )
        await message.answer(preview, parse_mode="HTML")
    except Exception:
        logger.exception("Ошибка шага params")
        await state.clear()
        await message.answer("⚠️ Ошибка, мастер прерван.")


@router.message(AuctionCreateStates.waiting_price, F.text)
async def step_price(message: Message, state: FSMContext) -> None:
    """Валидирует стартовую цену и показывает финальное превью с кнопкой запуска."""
    try:
        raw: str = (message.text or "").strip()
        if not raw.isdigit():
            await message.answer("⚠️ Стартовая цена должна быть целым числом больше 0.")
            return
        price: int = int(raw)
        if price <= 0:
            await message.answer("⚠️ Стартовая цена должна быть больше 0.")
            return
        data: dict[str, Any] = await state.get_data()
        await state.update_data(start_price=price)
        final_preview: str = (
            "📋 <b>Финальное превью аукциона</b>\n\n"
            f"📦 Лот: <b>{data['lot_name']}</b>\n"
            + format_stats_block(
                multiplier=data["multiplier"],
                power=data["power"],
                max_puffs=data["max_puffs"],
                condition=data["condition"],
                quantity=data["quantity"],
                indent="   ",
            )
            + f"\n💰 Стартовая цена: {price} Паров\n"
            f"📢 Канал публикации: {channel_mention()} (<code>{config.auction_channel_id}</code>)\n"
            f"⏳ Старт торгов через {config.auction_announce_minutes} мин после анонса\n\n"
            "Запускать?"
        )
        # draft_id = хеш данных FSM; используется только как метка подтверждения
        draft_id: int = abs(hash(tuple(sorted(data.items())))) % 10**9
        await state.update_data(draft_id=draft_id)
        await message.answer(final_preview,
                             reply_markup=auction_confirm_keyboard(draft_id), parse_mode="HTML")
        await state.set_state(AuctionCreateStates.waiting_price)  # ждём подтверждения кнопкой
    except Exception:
        logger.exception("Ошибка шага price")
        await state.clear()
        await message.answer("⚠️ Ошибка, мастер прерван.")


@router.callback_query(F.data.startswith("confirm:"))
async def cb_confirm_launch(callback: CallbackQuery, state: FSMContext, is_admin: bool) -> None:
    """Кнопка «Запустить аукцион»: создаёт запись, публикует анонс, планирует старт торгов."""
    if not is_admin:
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    try:
        data: dict[str, Any] = await state.get_data()
        expected_id: int = int(data.get("draft_id") or -1)
        raw: str = callback.data or ""
        # Формат колбэка: confirm:draft_id
        got_id: int = int(raw.split(":", 1)[1]) if ":" in raw else -1
        if got_id != expected_id:
            await callback.answer("⚠️ Превью устарело, запусти мастер заново.", show_alert=True)
            await state.clear()
            return
        required = ("lot_name", "multiplier", "power", "max_puffs", "condition", "quantity", "start_price")
        if any(k not in data for k in required):
            await callback.answer("⚠️ Данные мастера потеряны, начни заново.", show_alert=True)
            await state.clear()
            return
        auction_id: int = await auction_service.create_auction(
            lot_name=str(data["lot_name"]),
            multiplier=int(data["multiplier"]),
            power=int(data["power"]),
            max_puffs=int(data["max_puffs"]),
            condition=int(data["condition"]),
            quantity=int(data["quantity"]),
            start_price=int(data["start_price"]),
        )
        await state.clear()
        # Публикуем анонс в канал (с проверкой прав бота внутри сервиса)
        message_id = await auction_service.publish_announce(callback.bot, auction_id)
        if message_id is None:
            await callback.message.edit_text(
                f"❌ Аукцион #{auction_id} создан, но публикация в канал не удалась "
                f"(проверь, что бот — админ канала {channel_mention()}). Аукцион отменён."
            )
            await callback.answer()
            return
        # Планируем старт торгов через AUCTION_ANNOUNCE_MINUTES минут
        scheduler_service.schedule_auction_start(auction_id, config.auction_announce_minutes)
        await callback.message.edit_text(
            f"✅ <b>Аукцион #{auction_id} запущен!</b>\n"
            f"📢 Анос опубликован в канале {channel_mention()}.\n"
            f"⏳ Торги начнутся через {config.auction_announce_minutes} мин.",
            parse_mode="HTML",
        )
        await callback.answer()
    except ValueError as exc:
        # Ожидаемые ошибки валидации (например, уже есть активный аукцион)
        logger.warning("Отказ в создании аукциона: %s", exc)
        await callback.answer(f"⛔ {exc}", show_alert=True)
    except Exception:
        logger.exception("Ошибка запуска аукциона")
        await state.clear()
        await callback.answer("⚠️ Ошибка при создании аукциона.", show_alert=True)


@router.callback_query(F.data == "admin_cancel_wizard")
async def cb_cancel_wizard(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «Отмена» мастера создания аукциона."""
    await state.clear()
    await callback.message.edit_text("🛑 Мастер создания аукциона отменён.", reply_markup=admin_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin_stats")
async def cb_stats(callback: CallbackQuery) -> None:
    """Кнопка «Статистика»: сводка по игрокам, подам, аукционам и балансам."""
    try:
        s = await auction_service.get_stats()
        top_lines: list[str] = [
            f"{i + 1}. <code>{row['user_id']}</code>"
            + (f" (@{row['username']})" if row.get("username") else "")
            + f" — {row['balance']} Паров"
            for i, row in enumerate(s["top"])
        ]
        text: str = (
            "📊 <b>Статистика</b>\n\n"
            f"👥 Игроков: {s['users']}\n"
            f"💨 Подиков: {s['vapes']} (сломанных: {s['broken']})\n"
            f"🔨 Аукционов всего: {s['auctions']}\n"
            f"💰 Ставок всего: {s['bids']}\n"
            f"⚖️ Паров в экономике: {s['pool']}\n\n"
            "🏆 <b>Топ по балансу:</b>\n" + ("\n".join(top_lines) if top_lines else "—")
        )
        await callback.message.edit_text(text, reply_markup=admin_keyboard(), parse_mode="HTML")
        await callback.answer()
    except Exception:
        logger.exception("Ошибка admin_stats")
        await callback.answer("⚠️ Ошибка.", show_alert=True)


@router.callback_query(F.data == "admin_cancel")
async def cb_admin_cancel(callback: CallbackQuery) -> None:
    """Кнопка «Отменить аукцион»: доступна, только если lots_sold == 0."""
    try:
        auction = await auction_service.get_active_auction()
        if auction is None:
            await callback.answer("Нет активных аукционов.", show_alert=True)
            return
        ok, msg = await auction_service.cancel_auction(callback.bot, int(auction["id"]))
        await callback.answer(msg, show_alert=True)
    except Exception:
        logger.exception("Ошибка admin_cancel")
        await callback.answer("⚠️ Ошибка отмены.", show_alert=True)


# ============================ Выдача / изъятие подиков ============================


def _parse_give_params(raw: str) -> tuple[bool, str, dict[str, Any]]:
    """Парсит строку шага 2/2 мастера выдачи: 'название ... множ мощность бак состояние кол-во'.

    Последние пять токенов — числа (множитель мощность макс_затяжек состояние% кол-во),
    всё, что перед ними, — название подика (может содержать пробелы).

    Returns:
        (успех, сообщение об ошибке, словарь {name, multiplier, power, max_puffs,
         condition, quantity}).
    """
    empty: dict[str, Any] = {}
    parts: list[str] = raw.split()
    if len(parts) < 6:
        return False, ("Формат: <code>Название множитель мощность бак состояние кол-во</code> "
                       "(минимум 6 токенов)."), empty
    numbers: list[int] = []
    for part in parts[-5:]:
        try:
            numbers.append(int(part))
        except ValueError:
            return False, f"Последние 5 значений должны быть числами, получено «{part}».", empty
    name: str = " ".join(parts[:-5]).strip()
    if not name or len(name) > 64:
        return False, "Название подика должно быть от 1 до 64 символов.", empty
    keys = ("multiplier", "power", "max_puffs", "condition", "quantity")
    for key, value in zip(keys, numbers):
        low, high = vape_service.LOT_PARAM_LIMITS[key]
        if not low <= value <= high:
            return False, f"{key}={value} вне диапазона {low}..{high}.", empty
    params: dict[str, Any] = {"name": name}
    params.update(zip(keys, numbers))
    return True, "", params


@router.callback_query(F.data == "admin_give")
async def cb_admin_give(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 1/2 мастера выдачи подика: запрашиваем Telegram ID получателя."""
    try:
        await state.clear()
        await state.set_state(VapeGiveStates.waiting_user)
        await callback.message.edit_text(
            "🎁 <b>Выдача подика — шаг 1 из 2</b>\n\n"
            "Введи <b>Telegram ID игрока</b>, которому выдаём подик\n"
            "(например: <code>123456789</code>). Игрок может быть ещё не зарегистрирован — "
            "он будет зарегистрирован автоматически.\n\n"
            "Для выхода нажми «Отмена».",
            parse_mode="HTML", reply_markup=admin_back_keyboard(),
        )
        await callback.answer()
    except Exception:
        logger.exception("Ошибка admin_give")
        await callback.answer("⚠️ Ошибка.", show_alert=True)


@router.message(VapeGiveStates.waiting_user, F.text)
async def give_step_user(message: Message, state: FSMContext) -> None:
    """Проверяет ID получателя и просит параметры подика (шаг 2/2)."""
    try:
        raw: str = (message.text or "").strip().lstrip("@")
        if not raw.lstrip("-").isdigit():
            await message.answer("⚠️ Введи числовой Telegram ID, например <code>123456789</code>.",
                                 parse_mode="HTML")
            return
        user_id: int = int(raw)
        await state.update_data(target_user=user_id)
        await state.set_state(VapeGiveStates.waiting_params)
        await message.answer(
            "🎁 <b>Выдача подика — шаг 2 из 2</b>\n\n"
            "Введи параметры в формате:\n"
            "<code>название множитель мощность макс_затяжек состояние% кол-во</code>\n\n"
            "Пример: <code>Ghost Mod X 7 35 700 100 1</code>\n\n"
            "Диапазоны: множитель 1..100, мощность 1..1000, бак 10..100000, "
            "состояние 1..100%, количество 1..10.",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Ошибка шага выдачи (user)")
        await state.clear()
        await message.answer("⚠️ Ошибка, мастер прерван.", reply_markup=admin_keyboard())


@router.message(VapeGiveStates.waiting_params, F.text)
async def give_step_params(message: Message, state: FSMContext) -> None:
    """Валидирует параметры, выдаёт подики игроку и шлёт ему уведомление."""
    try:
        ok, err, params = _parse_give_params((message.text or "").strip())
        if not ok:
            await message.answer(f"⚠️ {err}\nПример: <code>Ghost Mod X 7 35 700 100 1</code>",
                                 parse_mode="HTML")
            return
        data: dict[str, Any] = await state.get_data()
        target_user: int = int(data["target_user"])
        success, result_msg = await vape_service.admin_give_vape(
            user_id=target_user,
            name=str(params["name"]),
            multiplier=int(params["multiplier"]),
            power=int(params["power"]),
            max_puffs=int(params["max_puffs"]),
            condition=int(params["condition"]),
            quantity=int(params["quantity"]),
        )
        await state.clear()
        icon = "✅" if success else "⚠️"
        await message.answer(
            f"{icon} <b>Результат выдачи</b>\n\n{result_msg}",
            parse_mode="HTML", reply_markup=admin_keyboard(),
        )
        if success:
            # Уведомляем получателя (ошибки доставки не критичны)
            try:
                await message.bot.send_message(
                    chat_id=target_user,
                    text=(
                        "🎁 <b>Администратор выдал тебе подик!</b>\n\n"
                        + format_stats_block(
                            name=params["name"],
                            multiplier=params["multiplier"],
                            power=params["power"],
                            max_puffs=params["max_puffs"],
                            condition=params["condition"],
                            quantity=None,
                        )
                        + (f"\n📦 Количество: {params['quantity']} шт"
                           if int(params["quantity"]) > 1 else "")
                        + "\n\nПодик добавлен в /inventory."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                logger.warning("Не удалось уведомить игрока %s о выдаче", target_user)
    except Exception:
        logger.exception("Ошибка шага выдачи (params)")
        await state.clear()
        await message.answer("⚠️ Ошибка, мастер прерван.", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin_remove")
async def cb_admin_remove(callback: CallbackQuery, state: FSMContext) -> None:
    """Мастер изъятия подиков: запрашиваем Telegram ID игрока."""
    try:
        await state.clear()
        await state.set_state(VapeRemoveStates.waiting_user)
        await callback.message.edit_text(
            "⛏ <b>Изъятие подиков</b>\n\n"
            "Введи <b>Telegram ID игрока</b>, у которого забираем подики "
            "(например: <code>123456789</code>).\n"
            "Дальше появится список его подиков с кнопками удаления.\n\n"
            "Для выхода нажми «Назад».",
            parse_mode="HTML", reply_markup=admin_back_keyboard(),
        )
        await callback.answer()
    except Exception:
        logger.exception("Ошибка admin_remove")
        await callback.answer("⚠️ Ошибка.", show_alert=True)


@router.message(VapeRemoveStates.waiting_user, F.text)
async def remove_step_user(message: Message, state: FSMContext) -> None:
    """Показывает список подиков указанного игрока с кнопками «Забрать»."""
    try:
        raw: str = (message.text or "").strip().lstrip("@")
        if not raw.lstrip("-").isdigit():
            await message.answer("⚠️ Введи числовой Telegram ID, например <code>123456789</code>.",
                                 parse_mode="HTML")
            return
        target_user: int = int(raw)
        await state.clear()
        vapes = await vape_service.get_user_vapes(target_user)
        if not vapes:
            await message.answer(
                f"😶 У игрока <code>{target_user}</code> нет подиков в инвентаре.",
                parse_mode="HTML", reply_markup=admin_keyboard(),
            )
            return
        blocks: list[str] = [
            f"<b>#{v['id']}</b>\n" + format_stats_block(
                name=v["name"],
                multiplier=v["multiplier"],
                power=v["power"],
                max_puffs=v["max_puffs"],
                condition=v["condition"],
                current_puffs=v["current_puffs"],
                indent="   ",
            ) + (" <i>[экипирован]</i>" if v["is_equipped"] else "")
            + (" <i>(сломан)</i>" if v["is_broken"] else "")
            for v in vapes
        ]
        builder = InlineKeyboardBuilder()
        for v in vapes:
            builder.button(text=f"⛏ Забрать #{v['id']} {v['name'][:20]}",
                           callback_data=f"admin_del:{target_user}:{v['id']}")
        builder.button(text="⬅️ Назад в админ-панель", callback_data="back_admin")
        builder.adjust(1)
        kb: InlineKeyboardMarkup = builder.as_markup()
        text: str = (f"⛏ <b>Подики игрока</b> <code>{target_user}</code> "
                     f"(всего {len(vapes)}):\n\n" + "\n\n".join(blocks)
                     + "\n\nНажми кнопку под «Забрать», чтобы удалить подик.")
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        logger.exception("Ошибка шага изъятия (user)")
        await state.clear()
        await message.answer("⚠️ Ошибка, мастер прерван.", reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("admin_del:"))
async def cb_admin_del(callback: CallbackQuery, is_admin: bool) -> None:
    """Кнопка «Забрать #id»: удаляет конкретный подик у игрока и обновляет список."""
    if not is_admin:
        await callback.answer("⛔ Недостаточно прав.", show_alert=True)
        return
    try:
        raw: str = callback.data or ""
        parts: list[str] = raw.split(":")
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await callback.answer("Некорректные данные кнопки.", show_alert=True)
            return
        target_user: int = int(parts[1])
        vape_id: int = int(parts[2])
        ok, msg = await vape_service.admin_remove_vape(target_user, vape_id)
        if not ok:
            await callback.answer(msg, show_alert=True)
            return
        # Уведомляем игрока о том, что админ забрал его подик
        try:
            await callback.bot.send_message(chat_id=target_user, text=msg)
        except Exception:
            logger.warning("Не удалось уведомить игрока %s об изъятии", target_user)
        # Перерисовываем список оставшихся подиков
        vapes = await vape_service.get_user_vapes(target_user)
        if not vapes:
            await callback.message.edit_text(
                f"✅ {msg}\n\n😶 Инвентарь игрока <code>{target_user}</code> теперь пуст.",
                parse_mode="HTML", reply_markup=admin_keyboard(),
            )
        else:
            blocks: list[str] = [
                f"<b>#{v['id']}</b>\n" + format_stats_block(
                    name=v["name"],
                    multiplier=v["multiplier"],
                    power=v["power"],
                    max_puffs=v["max_puffs"],
                    condition=v["condition"],
                    current_puffs=v["current_puffs"],
                    indent="   ",
                ) + (" <i>[экипирован]</i>" if v["is_equipped"] else "")
                + (" <i>(сломан)</i>" if v["is_broken"] else "")
                for v in vapes
            ]
            builder = InlineKeyboardBuilder()
            for v in vapes:
                builder.button(text=f"⛏ Забрать #{v['id']} {v['name'][:20]}",
                               callback_data=f"admin_del:{target_user}:{v['id']}")
            builder.button(text="⬅️ Назад в админ-панель", callback_data="back_admin")
            builder.adjust(1)
            await callback.message.edit_text(
                f"✅ {msg}\n\n⛏ <b>Оставшиеся подики игрока</b> <code>{target_user}</code>:",
                parse_mode="HTML", reply_markup=builder.as_markup(),
            )
        await callback.answer()
    except Exception:
        logger.exception("Ошибка admin_del")
        await callback.answer("⚠️ Ошибка.", show_alert=True)


@router.callback_query(F.data == "back_admin")
async def cb_back_admin(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «Назад в админ-панель» — сброс FSM и возврат в главное меню админа."""
    await state.clear()
    await callback.message.edit_text(
        f"🛠 <b>Админ-панель Vape Tycoon</b>\n\n"
        f"Канал аукционов: {channel_mention()} (<code>{config.auction_channel_id}</code>)\n"
        f"Анонс → старт: {config.auction_announce_minutes} мин | "
        f"Таймер ставки: {config.bid_timer_seconds} сек | "
        f"Перерыв между лотами: {config.lot_break_minutes} мин",
        reply_markup=admin_keyboard(), parse_mode="HTML",
    )
    await callback.answer()
