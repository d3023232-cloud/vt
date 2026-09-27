"""Сервис аукционов: анонс, старт торгов, ставки, таймер, продажа лотов, реролл и отмена.

Все запросы к БД — асинхронные (aiosqlite). Операции со ставками выполняются
в транзакциях с условиями на баланс, что исключает отрицательный баланс,
двойное списание и дублирование Паров при возвратах.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

import aiosqlite
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import config
from database import DATABASE_PATH, execute_query, fetch_all, fetch_one
from services import scheduler_service
from services.vape_service import give_auction_vape
from utils.helpers import fmt_dt, format_stats_block, now_utc, parse_dt

logger = logging.getLogger(__name__)

# Допустимые шаги ставки (сопоставлены с текстом кнопок в канале)
BID_STEPS: dict[str, int] = {"100": 100, "250": 250, "500": 500, "1000": 1000}

# Диапазоны валидации параметров лота на шаге 2 мастера создания аукциона
LOT_LIMITS: dict[str, tuple[int, int]] = {
    "multiplier": (1, 100),
    "power": (1, 1000),
    "max_puffs": (10, 100000),
    "condition": (1, 100),
    "quantity": (1, 10),
}

# Активные аукционы не запускаются параллельно; статусы для проверки
ACTIVE_STATUSES: tuple[str, ...] = ("announced", "active")


async def has_active_auction() -> bool:
    """Проверяет, есть ли уже незавершённый аукцион (запрет нескольких одновременно)."""
    row = await fetch_one(
        "SELECT id FROM auctions WHERE status IN (?, ?) LIMIT 1", ACTIVE_STATUSES
    )
    return row is not None


async def get_active_auction() -> dict[str, Any] | None:
    """Возвращает текущий активный/анонсированный аукцион или None."""
    return await fetch_one(
        "SELECT * FROM auctions WHERE status IN (?, ?) ORDER BY id DESC LIMIT 1",
        ACTIVE_STATUSES,
    )


async def get_auction(auction_id: int) -> dict[str, Any] | None:
    """Возвращает аукцион по ID."""
    return await fetch_one("SELECT * FROM auctions WHERE id = ?", (auction_id,))


async def create_auction(
    lot_name: str,
    multiplier: int,
    power: int,
    max_puffs: int,
    condition: int,
    quantity: int,
    start_price: int,
    delay_minutes: int | None = None,
) -> int:
    """Создаёт запись аукциона со статусом 'announced'.

    Валидирует входные данные, запрещает запуск второго аукциона одновременно.

    Args:
        delay_minutes: через сколько минут начнутся торги (None — из конфига).

    Returns:
        ID созданного аукциона (его номер).

    Raises:
        ValueError: некорректные параметры или уже есть активный аукцион.
    """
    if not lot_name.strip():
        raise ValueError("Название лота не может быть пустым")
    if start_price <= 0:
        raise ValueError("Стартовая цена должна быть больше нуля")
    if delay_minutes is None:
        delay_minutes = config.auction_announce_minutes
    if delay_minutes < 1 or delay_minutes > 1440:
        raise ValueError("Задержка старта должна быть от 1 минуты до 24 часов")
    for key, value in (
        ("multiplier", multiplier), ("power", power), ("max_puffs", max_puffs),
        ("condition", condition), ("quantity", quantity),
    ):
        low, high = LOT_LIMITS[key]
        if not low <= value <= high:
            raise ValueError(f"Параметр {key}={value} вне диапазона {low}..{high}")

    if await has_active_auction():
        raise ValueError("Уже есть активный аукцион. Нельзя запускать несколько одновременно.")

    auction_id = await execute_query(
        """
        INSERT INTO auctions (lot_name, multiplier, power, max_puffs, condition,
                              quantity, start_price, status, channel_id, current_lot_number,
                              starts_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'announced', ?, 1, ?)
        """,
        (lot_name.strip(), multiplier, power, max_puffs, condition,
         quantity, start_price, config.auction_channel_id,
         fmt_dt(now_utc() + timedelta(minutes=delay_minutes))),
    )
    logger.info("Создан аукцион #%s: %s (x%d, партия %d шт)", auction_id, lot_name, multiplier, quantity)
    return auction_id


def _bid_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура ставок для поста в канале: [+100], [+250], [+500], [+1000]."""
    builder = InlineKeyboardBuilder()
    builder.button(text="+100 💨", callback_data="bid:100")
    builder.button(text="+250 💨", callback_data="bid:250")
    builder.button(text="+500 💨", callback_data="bid:500")
    builder.button(text="+1000 💨", callback_data="bid:1000")
    builder.adjust(4)
    return builder.as_markup()


def format_countdown(remaining_seconds: int) -> str:
    """Форматирует обратный отсчёт до события (старт торгов / конец лота).

    Правила: меньше минуты — посекундный отсчёт ('N сек');
    минуту и больше — отсчёт в минутах и часах, который меняется раз в минуту.

    Args:
        remaining_seconds: сколько секунд осталось (значения <= 0 дают 'меньше минуты').

    Returns:
        Строка вида '5 сек', '3 мин', '1 ч 5 мин', '2 ч'.
    """
    if remaining_seconds < 60:
        return f"{max(0, remaining_seconds)} сек"
    minutes: int = remaining_seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours, mins = divmod(minutes, 60)
    return f"{hours} ч {mins} мин" if mins else f"{hours} ч"


def _lot_caption(auction: dict[str, Any], bid_row: dict[str, Any] | None, ends_at: Any = None) -> str:
    """Формирует текст поста-лота в канале (анонс / торги / финал)."""
    lines: list[str] = [
        f"🔨 <b>АУКЦИОН #{auction['id']}</b>",
        f"📦 Лот: <b>{auction['lot_name']}</b>",
        format_stats_block(
            multiplier=auction["multiplier"],
            power=auction["power"],
            max_puffs=auction["max_puffs"],
            condition=auction["condition"],
            quantity=auction["quantity"],
            lots_sold=auction["lots_sold"],
            indent="   ",
        ),
    ]
    status: str = auction["status"]
    if status == "announced":
        # Отсчёт до старта торгов: <1 мин — посекундно, иначе — поминутно
        starts_at = parse_dt(auction.get("starts_at"))
        if starts_at is not None:
            remaining: int = max(0, int((starts_at - datetime.now()).total_seconds()))
            lines.append(f"⏳ Старт торгов через {format_countdown(remaining)}.")
        else:
            lines.append(f"⏳ Старт торгов через {config.auction_announce_minutes} мин.")
        lines.append(f"🏁 Стартовая цена: {auction['start_price']} Паров")
    elif status == "active":
        lot_no: int = int(auction["current_lot_number"])
        lines.append(f"🔥 ЛОТ {lot_no}/{auction['quantity']} — ТОРГИ ИДУТ!")
        if bid_row is not None:
            lines.append(f"💰 Текущая ставка: <b>{bid_row['amount']} Паров</b>")
            lines.append(f"👑 Лидер: <code>{bid_row['user_id']}</code>")
        else:
            lines.append(f"💰 Текущая ставка: <b>{auction['start_price']} Паров</b> (нет ставок)")
        if ends_at is not None:
            # ends_at хранится в локальном времени (как и планировщик APScheduler)
            remaining: int = max(0, int((ends_at - datetime.now()).total_seconds()))
            lines.append(f"⏱ До конца лота: {remaining} сек")
    elif status == "finished":
        lines.append("✅ Аукцион завершён. Все лоты проданы!")
    elif status == "cancelled":
        lines.append("❌ Аукцион отменён администратором.")
    return "\n".join(lines)


async def check_channel_admin(bot: Bot) -> bool:
    """Проверяет, что бот является админом канала из AUCTION_CHANNEL_ID.

    Публиковать пост можно только после успешной проверки.
    """
    try:
        member = await bot.get_chat_member(config.auction_channel_id, (await bot.get_me()).id)
        return member.status in ("administrator", "creator")
    except TelegramAPIError:
        logger.exception("Не удалось проверить права бота в канале %s", config.auction_channel_id)
        return False


async def publish_announce(bot: Bot, auction_id: int) -> int | None:
    """Публикует пост-анонс в канал и сохраняет message_id в БД.

    Returns:
        ID сообщения в канале либо None при ошибке (аукцион помечается cancelled).
    """
    auction = await get_auction(auction_id)
    if auction is None:
        return None
    if not await check_channel_admin(bot):
        logger.error("Бот не является админом канала %s — публикация запрещена", config.auction_channel_id)
        await cancel_auction(bot, auction_id, notify_admin=False)
        return None
    try:
        sent = await bot.send_message(
            chat_id=config.auction_channel_id,
            text=_lot_caption(auction, None),
            reply_markup=_bid_keyboard(),
        )
        await execute_query("UPDATE auctions SET message_id = ? WHERE id = ?", (sent.message_id, auction_id))
        logger.info("Анонс аукциона #%s опубликован (message_id=%s)", auction_id, sent.message_id)
        return sent.message_id
    except TelegramAPIError:
        logger.exception("Ошибка публикации анонса аукциона #%s", auction_id)
        await cancel_auction(bot, auction_id, notify_admin=False)
        return None


async def update_channel_post(bot: Bot, auction: dict[str, Any], bid_row: dict[str, Any] | None,
                              ends_at: Any = None) -> None:
    """Редактирует пост в канале с актуальным состоянием торгов (ставка/лидер/таймер)."""
    message_id = auction.get("message_id")
    if not message_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=auction["channel_id"],
            message_id=message_id,
            text=_lot_caption(auction, bid_row, ends_at),
            reply_markup=_bid_keyboard(),
        )
    except TelegramAPIError as exc:
        # «message is not modified» — нормальная ситуация при частых обновлениях
        if "not modified" not in str(exc).lower():
            logger.warning("Не удалось обновить пост аукциона #%s: %s", auction["id"], exc)


async def start_bidding(bot: Bot, auction_id: int) -> None:
    """Стартует торги: статус 'active', started_at, обнуление current_lot_number, обновление поста."""
    auction = await get_auction(auction_id)
    if auction is None or auction["status"] != "announced":
        logger.warning("Старт торгов пропущен: аукцион #%s не найден/не в статусе announced", auction_id)
        return
    await execute_query(
        "UPDATE auctions SET status = 'active', started_at = ?, current_lot_number = 1 WHERE id = ?",
        (fmt_dt(now_utc()), auction_id),
    )
    auction = await get_auction(auction_id)
    assert auction is not None
    scheduler_service.reset_bid_deadline(auction_id, config.bid_timer_seconds)
    scheduler_service.schedule_lot_close(auction_id, config.bid_timer_seconds)
    await update_channel_post(bot, auction, None, _get_deadline(auction_id))
    logger.info("Торги по аукциону #%s начаты, лот 1/%s", auction_id, auction["quantity"])


def _get_deadline(auction_id: int) -> Any:
    """Возвращает datetime дедлайна текущего лота (или None)."""
    return scheduler_service.BID_DEADLINES.get(auction_id)


async def place_bid(bot: Bot, user_id: int, step_key: str) -> tuple[bool, str]:
    """Обрабатывает ставку игрока кнопкой +N из поста в канале.

    Логика транзакции:
      1. Проверка регистрации и баланса (balance >= текущая ставка + шаг).
      2. Списание суммы новой ставки с баланса (условие balance >= сумма).
      3. Новая ставка is_winning = 1.
      4. Возврат Паров предыдущей ставки ЭТОГО ЖЕ игрока (is_winning=0, is_refunded=1).
      5. Возврат Паров предыдущего лидера (проигравшим возвращаются, победителю — никогда).

    Returns:
        (успех, сообщение для alert над кнопкой).
    """
    if step_key not in BID_STEPS:
        return False, "Некорректный шаг ставки."
    step: int = BID_STEPS[step_key]

    auction = await get_active_auction()
    if auction is None or auction["status"] != "active":
        return False, "Сейчас нет активных торгов."

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            user_row = await cur.fetchone()
            if user_row is None:
                await db.rollback()
                return False, "Ты не зарегистрирован. Используй /start"

            cur = await db.execute(
                "SELECT b.*, u.username FROM bids b "
                "LEFT JOIN users u ON u.user_id = b.user_id "
                "WHERE b.auction_id = ? AND b.lot_number = ? AND b.is_winning = 1",
                (auction["id"], auction["current_lot_number"]),
            )
            leading = await cur.fetchone()
            current_bid: int = int(leading["amount"]) if leading else int(auction["start_price"])
            new_amount: int = current_bid + step

            # Проверка баланса ДО списания: balance >= текущая ставка + шаг
            if int(user_row["balance"]) < new_amount:
                await db.rollback()
                return False, f"❌ Недостаточно Паров. Нужно {new_amount}, у тебя {user_row['balance']}."

            # Списание новой суммы ставки (условие balance >= сумма — защита от ухода в минус)
            cur = await db.execute(
                "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
                (new_amount, user_id, new_amount),
            )
            if cur.rowcount == 0:
                await db.rollback()
                return False, f"❌ Недостаточно Паров. Нужно {new_amount}, у тебя {user_row['balance']}."

            # Возврат предыдущей ставки этого же игрока (если он был лидером):
            # помечаем её неведущей и возвращаем Пары — затем спишется новая сумма.
            # Победителю на момент ставки Пары не возвращаются дважды: старая его
            # ставка возвращается одной транзакцией и той же заменой на новую.
            if leading and int(leading["user_id"]) == user_id:
                await db.execute(
                    "UPDATE bids SET is_winning = 0, is_refunded = 1 WHERE id = ?", (leading["id"],)
                )
                await db.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (int(leading["amount"]), user_id),
                )

            # Возврат предыдущему лидеру-проигравшему (победителю Пары не возвращаются!)
            if leading and int(leading["user_id"]) != user_id:
                await db.execute(
                    "UPDATE bids SET is_winning = 0, is_refunded = 1 WHERE id = ?", (leading["id"],)
                )
                await db.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (int(leading["amount"]), int(leading["user_id"])),
                )

            # Новая ведущая ставка
            cur = await db.execute(
                "INSERT INTO bids (auction_id, lot_number, user_id, amount, is_winning) "
                "VALUES (?, ?, ?, ?, 1)",
                (auction["id"], auction["current_lot_number"], user_id, new_amount),
            )
            bid_id = cur.lastrowid
            await db.commit()
        except aiosqlite.Error:
            await db.rollback()
            logger.exception("Ошибка ставки игрока %s на аукционе #%s", user_id, auction["id"])
            return False, "Ошибка проведения ставки, попробуй ещё раз."
        finally:
            await db.close()

    # Сбрасываем таймер лота и обновляем пост в канале
    scheduler_service.reset_bid_deadline(int(auction["id"]), config.bid_timer_seconds)
    scheduler_service.reschedule_lot_close(int(auction["id"]), config.bid_timer_seconds)

    fresh = await get_auction(int(auction["id"]))
    assert fresh is not None
    cur_lead = await fetch_one(
        "SELECT * FROM bids WHERE id = ?", (bid_id,)
    )
    await update_channel_post(bot, fresh, cur_lead, _get_deadline(int(auction["id"])))
    logger.info("Игрок %s поставил %s Паров на аукцион #%s (лот %s)",
                user_id, new_amount, auction["id"], auction["current_lot_number"])
    return True, f"✅ Ставка {new_amount} Паров принята! Ты лидируешь 🏆"


async def refresh_timer(bot: Bot) -> None:
    """Фоновая задача каждые 5 секунд: обновляет таймер в посте активного аукциона.

    Если дедлайн истёк — закрытие лота выполняет отдельная задача APScheduler,
    здесь только перерисовка секунд. Дополнительно, если сейчас есть аукцион в
    статусе 'announced' и до его старта меньше минуты — посекундно обновляется
    обратный отсчёт анонса (поминутно его обновляет _announce_timer_job).
    """
    auction = await fetch_one("SELECT * FROM auctions WHERE status = 'active' ORDER BY id LIMIT 1")
    if auction is not None:
        deadline = scheduler_service.BID_DEADLINES.get(int(auction["id"]))
        if deadline is not None:
            leading = await fetch_one(
                "SELECT b.*, u.username FROM bids b LEFT JOIN users u ON u.user_id = b.user_id "
                "WHERE b.auction_id = ? AND b.lot_number = ? AND b.is_winning = 1",
                (auction["id"], auction["current_lot_number"]),
            )
            await update_channel_post(bot, auction, dict(leading) if leading else None, deadline)

    # Посекундный отсчёт последних <60 сек перед стартом торгов анонса
    announced = await get_announced_auction()
    if announced is not None:
        starts_at = parse_dt(announced.get("starts_at"))
        if starts_at is not None:
            remaining: int = int((starts_at - datetime.now()).total_seconds())
            if 0 <= remaining < 60:
                await update_channel_post(bot, announced, None)


async def get_announced_auction() -> dict[str, Any] | None:
    """Возвращает последний аукцион в статусе 'announced' (или None)."""
    return await fetch_one(
        "SELECT * FROM auctions WHERE status = 'announced' ORDER BY id DESC LIMIT 1"
    )


async def update_announce_countdown(bot: Bot) -> bool:
    """Обновляет пост-анонс в канале (строка с обратным отсчётом до старта торгов).

    Returns:
        True, если анонс найден и пост обновлён; False — нечего обновлять.
    """
    auction = await get_announced_auction()
    if auction is None or not auction.get("message_id"):
        return False
    await update_channel_post(bot, auction, None)
    return True


async def close_lot(bot: Bot, auction_id: int) -> None:
    """Завершает текущий лот: продаёт победителю либо оставляет без ставок.

    Правила:
      - Победитель получает подик; если инвентарь полон — Пары возвращаются
        победителю и лот выставляется повторно (тот же номер лота).
      - При отсутствии ставок лот уходит без продажи (повторно).
      - Проигравшие получили возврат Паров ещё на этапе ставок.
      - Если lots_sold < quantity — через LOT_BREAK_MINUTES стартует следующий лот.
      - Если lots_sold == quantity — аукцион завершается, публикуется финальный пост.
    """
    auction = await get_auction(auction_id)
    if auction is None or auction["status"] != "active":
        return
    lot_number: int = int(auction["current_lot_number"])
    leading = await fetch_one(
        "SELECT * FROM bids WHERE auction_id = ? AND lot_number = ? AND is_winning = 1",
        (auction_id, lot_number),
    )

    # Лот закрывается — убираем дедлайн (профилактика утечек памяти словаря)
    scheduler_service.clear_bid_deadline(auction_id)

    if leading is None:
        # Лот без ставок — повторяем этот же лот через перерыв
        logger.info("Аукцион #%s: лот %s без ставок, повтор", auction_id, lot_number)
        await _post_lot_notice(bot, auction, "😶 Лот прошёл без ставок — он будет выставлен повторно.")
        scheduler_service.schedule_next_lot(auction_id, config.lot_break_minutes, same_lot=True)
        return

    winner_id: int = int(leading["user_id"])
    amount: int = int(leading["amount"])
    granted: bool = await give_auction_vape(
        winner_id, auction["lot_name"], int(auction["multiplier"]),
        int(auction["power"]), int(auction["max_puffs"]), int(auction["condition"]),
    )
    if not granted:
        # Инвентарь победителя полон: выдавать подик запрещено. Пары возвращаются
        # (это единственный допустимый случай возврата победителю — сделка
        # считается несостоявшейся), ставка гасится, лот перевыставляется.
        async with aiosqlite.connect(DATABASE_PATH) as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cur = await db.execute(
                    "UPDATE bids SET is_winning = 0, is_refunded = 1 "
                    "WHERE id = ? AND is_refunded = 0",
                    (leading["id"],),
                )
                if cur.rowcount == 1:
                    await db.execute(
                        "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                        (amount, winner_id),
                    )
                await db.commit()
            except aiosqlite.Error:
                await db.rollback()
                logger.exception("Ошибка возврата Паров победителю %s", winner_id)
            finally:
                await db.close()
        logger.info("Аукцион #%s: инвентарь победителя %s полон, лот %s повторен",
                    auction_id, winner_id, lot_number)
        await _notify_user(bot, winner_id,
                           f"🎉 Ты выиграл лот «{auction['lot_name']}», но твой инвентарь полон "
                           f"(максимум {config.max_vapes_per_user}).\n"
                           f"💸 Тебе возвращено {amount} Паров, лот будет выставлен повторно.")
        await _post_lot_notice(bot, auction, "🔄 Инвентарь победителя полон — лот выставлен повторно.")
        scheduler_service.schedule_next_lot(auction_id, config.lot_break_minutes, same_lot=True)
        return

    # Продажа засчитана
    sold: int = int(auction["lots_sold"]) + 1
    await execute_query(
        "UPDATE auctions SET lots_sold = ? WHERE id = ?", (sold, auction_id)
    )
    await _notify_user(bot, winner_id,
                       f"🎉 Ты выиграл лот «{auction['lot_name']}» с аукциона #{auction_id} "
                       f"за {amount} Паров!\n💨 Подик добавлен в инвентарь.")
    await _post_lot_notice(bot, auction,
                           f"🔨 Лот {lot_number}/{auction['quantity']} продан за {amount} Паров! "
                           f"Покупатель: <code>{winner_id}</code>")
    auction = await get_auction(auction_id)
    assert auction is not None

    quantity: int = int(auction["quantity"])
    if sold < quantity:
        # Следующий лот партии через LOT_BREAK_MINUTES минут
        await execute_query(
            "UPDATE auctions SET current_lot_number = ? WHERE id = ?", (lot_number + 1, auction_id)
        )
        scheduler_service.schedule_next_lot(auction_id, config.lot_break_minutes, same_lot=False)
    else:
        await finish_auction(bot, auction_id)


async def _post_lot_notice(bot: Bot, auction: dict[str, Any], notice: str) -> None:
    """Дополняет пост в канале строкой-уведомлением о результате лота."""
    try:
        await bot.send_message(chat_id=auction["channel_id"], text=f"ℹ️ {notice}")
    except TelegramAPIError:
        logger.exception("Ошибка публикации уведомления по аукциону #%s", auction["id"])


async def _notify_user(bot: Bot, user_id: int, text: str) -> None:
    """Отправляет личное уведомление игроку (ошибки доставки не критичны)."""
    try:
        await bot.send_message(chat_id=user_id, text=text)
    except TelegramAPIError:
        logger.warning("Не удалось отправить уведомление игроку %s", user_id)


async def start_next_lot(bot: Bot, auction_id: int, same_lot: bool) -> None:
    """Открывает торги текущего лота после перерыва партии.

    Номер лота в БД уже установлен вызвавшей стороной (close_lot):
    при продаже — следующий, при несостоявшихся торгах — тот же самый.

    Args:
        same_lot: True — повтор этого же лота, False — новый лот партии.
    """
    auction = await get_auction(auction_id)
    if auction is None or auction["status"] != "active":
        return
    current_no: int = int(auction["current_lot_number"])
    scheduler_service.reset_bid_deadline(auction_id, config.bid_timer_seconds)
    scheduler_service.schedule_lot_close(auction_id, config.bid_timer_seconds)
    await update_channel_post(bot, auction, None, _get_deadline(auction_id))
    logger.info("Аукцион #%s: открыт лот %s/%s (повтор=%s)",
                auction_id, current_no, auction["quantity"], same_lot)


async def finish_auction(bot: Bot, auction_id: int) -> None:
    """Завершает аукцион: статус 'finished', финальный пост с итогами."""
    auction = await get_auction(auction_id)
    if auction is None:
        return
    await execute_query(
        "UPDATE auctions SET status = 'finished', finished_at = ? WHERE id = ?",
        (fmt_dt(now_utc()), auction_id),
    )
    auction = await get_auction(auction_id)
    assert auction is not None
    total_bids = await fetch_one(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total FROM bids WHERE auction_id = ?",
        (auction_id,),
    )
    final_text: str = (
        f"🏁 <b>Аукцион #{auction_id} завершён!</b>\n"
        f"📦 Лот: {auction['lot_name']} | Продано: {auction['lots_sold']}/{auction['quantity']}\n"
        f"💰 Ставок сделано: {total_bids['cnt'] if total_bids else 0}, "
        f"оборот: {total_bids['total'] if total_bids else 0} Паров.\n"
        f"Следующие аукционы — в канале @{config.auction_channel_username}"
    )
    try:
        if auction.get("message_id"):
            await bot.edit_message_text(
                chat_id=auction["channel_id"], message_id=auction["message_id"],
                text=final_text,
            )
        else:
            await bot.send_message(chat_id=auction["channel_id"], text=final_text)
    except TelegramAPIError:
        logger.exception("Ошибка финального поста аукциона #%s", auction_id)
    scheduler_service.cancel_auction_jobs(auction_id)
    logger.info("Аукцион #%s завершён", auction_id)


async def cancel_auction(bot: Bot, auction_id: int, notify_admin: bool = True) -> tuple[bool, str]:
    """Отменяет аукцион. Запрещено, если хотя бы один лот уже продан (lots_sold > 0).

    При отмене: все ведущие ставки помечаются возвращёнными, Пары возвращаются
    игрокам, статус 'cancelled', задачи планировщика отменяются, в канале
    публикуется пост об отмене.

    Returns:
        (успех, сообщение).
    """
    auction = await get_auction(auction_id)
    if auction is None:
        return False, "Аукцион не найден."
    if auction["status"] not in ACTIVE_STATUSES:
        return False, f"Аукцион #{auction_id} уже в статусе «{auction['status']}», отмена невозможна."
    if int(auction["lots_sold"]) > 0:
        return False, f"❌ Нельзя отменить: уже продано {auction['lots_sold']} лотов."

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "SELECT id, user_id, amount FROM bids WHERE auction_id = ? AND is_winning = 1",
                (auction_id,),
            )
            winning_bids = await cur.fetchall()
            for bid in winning_bids:
                # Возврат Паров всем действующим лидерам (они стали проигравшими)
                await db.execute(
                    "UPDATE bids SET is_winning = 0, is_refunded = 1 WHERE id = ?", (bid["id"],)
                )
                await db.execute(
                    "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                    (int(bid["amount"]), int(bid["user_id"])),
                )
            await db.execute(
                "UPDATE auctions SET status = 'cancelled', finished_at = ? WHERE id = ?",
                (fmt_dt(now_utc()), auction_id),
            )
            await db.commit()
        except aiosqlite.Error:
            await db.rollback()
            logger.exception("Ошибка отмены аукциона #%s", auction_id)
            return False, "Ошибка отмены, попробуй позже."
        finally:
            await db.close()

    from services import scheduler_service
    scheduler_service.cancel_auction_jobs(auction_id)

    try:
        await bot.send_message(
            chat_id=config.auction_channel_id,
            text=f"❌ Аукцион #{auction_id} ({auction['lot_name']}) отменён администратором. "
                 f"Все ставки возвращены игрокам.",
        )
    except TelegramAPIError:
        logger.exception("Ошибка поста об отмене аукциона #%s", auction_id)

    logger.info("Аукцион #%s отменён, возвращено ставок: %s", auction_id, len(winning_bids))
    return True, f"✅ Аукцион #{auction_id} отменён, ставки возвращены."


async def get_stats() -> dict[str, Any]:
    """Собирает статистику для админ-панели."""
    users_cnt = await fetch_one("SELECT COUNT(*) AS c FROM users")
    vapes_cnt = await fetch_one("SELECT COUNT(*) AS c FROM vapes")
    broken_cnt = await fetch_one("SELECT COUNT(*) AS c FROM vapes WHERE is_broken = 1")
    auctions_cnt = await fetch_one("SELECT COUNT(*) AS c FROM auctions")
    bids_cnt = await fetch_one("SELECT COUNT(*) AS c FROM bids")
    vapor_pool = await fetch_one("SELECT COALESCE(SUM(balance), 0) AS s FROM users")
    top = await fetch_all("SELECT user_id, username, balance FROM users ORDER BY balance DESC LIMIT 5")
    return {
        "users": int(users_cnt["c"]) if users_cnt else 0,
        "vapes": int(vapes_cnt["c"]) if vapes_cnt else 0,
        "broken": int(broken_cnt["c"]) if broken_cnt else 0,
        "auctions": int(auctions_cnt["c"]) if auctions_cnt else 0,
        "bids": int(bids_cnt["c"]) if bids_cnt else 0,
        "pool": int(vapor_pool["s"]) if vapor_pool else 0,
        "top": top,
    }
