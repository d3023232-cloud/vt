"""Сервис механики подиков: регистрация, добыча (затяжки), сбор Паров, регенерация и износ.

Все операции с базой данных выполняются асинхронно через aiosqlite.
Критичные по балансу операции выполняются в одной транзакции, что исключает
отрицательный баланс и дублирование Паров при сбоях.
"""

import logging
import random
from typing import Any

import aiosqlite

from config import config
from database import DATABASE_PATH, execute_query, fetch_all, fetch_one
from utils.helpers import add_minutes, calc_vapors, fmt_dt, now_utc

logger = logging.getLogger(__name__)

# Параметры стартового подика (выдаётся при /start)
STARTER_VAPE_NAME: str = "Basic Vape"
STARTER_MULTIPLIER: int = 1
STARTER_POWER: int = 5
STARTER_MAX_PUFFS: int = 100

# Диапазоны допустимых значений параметров при админ-выдаче подиков
LOT_PARAM_LIMITS: dict[str, tuple[int, int]] = {
    "multiplier": (1, 100),
    "power": (1, 1000),
    "max_puffs": (10, 100000),
    "condition": (1, 100),
    "quantity": (1, 10),
}


async def register_user(user_id: int, username: str | None) -> dict[str, Any]:
    """Регистрирует нового игрока и выдаёт стартовый подик.

    Если пользователь уже зарегистрирован — запись НЕ удаляется и не
    перезаписывается, возвращаются существующие данные.

    Args:
        user_id: Telegram ID игрока.
        username: @username игрока (может быть None).

    Returns:
        Словарь-запись пользователя из таблицы users.
    """
    existing = await get_user(user_id)
    if existing is not None:
        # Пользователь уже зарегистрирован — обновляем только username
        await execute_query(
            "UPDATE users SET username = ? WHERE user_id = ?",
            (username, user_id),
        )
        return existing

    now: str = fmt_dt(now_utc())
    # Транзакция: создание пользователя + стартового подика + экипировка
    async with aiosqlite.connect(DATABASE_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO users (user_id, username, balance, last_collect_time) VALUES (?, ?, 0, ?)",
                (user_id, username, now),
            )
            cursor = await db.execute(
                """
                INSERT INTO vapes (owner_id, name, multiplier, power, max_puffs,
                                   current_puffs, condition, puffs_per_vapor,
                                   is_equipped, is_broken, regen_at)
                VALUES (?, ?, ?, ?, ?, 0, 100, ?, 1, 0, NULL)
                """,
                (
                    user_id,
                    STARTER_VAPE_NAME,
                    STARTER_MULTIPLIER,
                    STARTER_POWER,
                    STARTER_MAX_PUFFS,
                    config.puffs_per_vapor,
                ),
            )
            vape_id = cursor.lastrowid
            await db.execute(
                "UPDATE users SET equipped_vape_id = ? WHERE user_id = ?",
                (vape_id, user_id),
            )
            await db.commit()
            logger.info("Зарегистрирован игрок %s, стартовый подик #%s", user_id, vape_id)
        except aiosqlite.Error:
            await db.rollback()
            logger.exception("Ошибка регистрации пользователя %s", user_id)
            raise
        finally:
            await db.close()

    user = await get_user(user_id)
    assert user is not None
    return user


async def get_user(user_id: int) -> dict[str, Any] | None:
    """Возвращает запись пользователя из таблицы users (или None)."""
    return await fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))


async def get_equipped_vape(user_id: int) -> dict[str, Any] | None:
    """Возвращает экипированный рабочий подик пользователя (или None)."""
    return await fetch_one(
        "SELECT v.* FROM vapes v "
        "JOIN users u ON u.equipped_vape_id = v.id "
        "WHERE u.user_id = ? AND v.is_broken = 0",
        (user_id,),
    )


async def get_user_vapes(user_id: int) -> list[dict[str, Any]]:
    """Возвращает все подики пользователя (не более MAX_VAPES_PER_USER штук)."""
    return await fetch_all(
        "SELECT * FROM vapes WHERE owner_id = ? ORDER BY id LIMIT ?",
        (user_id, config.max_vapes_per_user),
    )


async def count_user_vapes(user_id: int) -> int:
    """Считает количество подиков в инвентаре пользователя."""
    row = await fetch_one("SELECT COUNT(*) AS cnt FROM vapes WHERE owner_id = ?", (user_id,))
    return int(row["cnt"]) if row else 0


async def generate_puffs() -> int:
    """Фоновая задача (каждую минуту): начисляет затяжки экипированным подикам.

    Скорость: power затяжек в минуту; добыча останавливается при достижении
    max_puffs (MIN(max_puffs, ...) гарантирует, что current_puffs никогда не
    превысит max_puffs).

    Returns:
        Количество обновлённых подиков.
    """
    updated: int = await execute_query(
        """
        UPDATE vapes
        SET current_puffs = MIN(max_puffs, current_puffs + power)
        WHERE is_equipped = 1 AND is_broken = 0 AND current_puffs < max_puffs
        """
    )
    logger.debug("Генерация затяжек: обновлено подиков: %s", updated)
    return updated


async def collect_vapors(user_id: int) -> tuple[str, str]:
    """Собирает Пары с экипированного подика.

    Формула: vapors = current_puffs * puffs_per_vapor * multiplier.
    После сбора: current_puffs = 0, last_collect_time = now,
    regen_at = now + REGEN_TIME_MINUTES минут.

    Args:
        user_id: Telegram ID игрока.

    Returns:
        Кортеж (status, message), где status один из:
        'ok' — Пары собраны; 'empty' — бак пуст;
        'no_vape' — нет экипированного подика; 'unregistered' — игрок не найден.
    """
    user = await get_user(user_id)
    if user is None:
        return "unregistered", "Ты не зарегистрирован. Используй /start"

    vape = await get_equipped_vape(user_id)
    if vape is None:
        return "no_vape", "У тебя нет экипированного подика. Загляни в /inventory или на аукцион."

    current_puffs: int = int(vape["current_puffs"])
    if current_puffs <= 0:
        return "empty", "Бак пуст! Дождитесь регенерации"

    vapors: int = calc_vapors(current_puffs, int(vape["puffs_per_vapor"]), int(vape["multiplier"]))
    now_dt = now_utc()
    regen_at: str = fmt_dt(add_minutes(config.regen_time_minutes))

    # Транзакция: обнуление бака + зачисление Паров.
    # Условие current_puffs > 0 в UPDATE гарантирует идемпотентность:
    # при повторном вызове (сбой/двойное нажатие) Пары не начислятся дважды.
    async with aiosqlite.connect(DATABASE_PATH) as db:
        try:
            cursor = await db.execute(
                """
                UPDATE vapes
                SET current_puffs = 0, regen_at = ?
                WHERE id = ? AND owner_id = ? AND current_puffs > 0 AND is_broken = 0
                """,
                (regen_at, vape["id"], user_id),
            )
            if cursor.rowcount == 0:
                # Бак уже опустошен параллельным запросом — ничего не начисляем
                await db.rollback()
                return "empty", "Бак пуст! Дождитесь регенерации"
            await db.execute(
                """
                UPDATE users
                SET balance = balance + ?, last_collect_time = ?
                WHERE user_id = ?
                """,
                (vapors, fmt_dt(now_dt), user_id),
            )
            await db.commit()
            logger.info("Игрок %s собрал %s Паров с подика #%s", user_id, vapors, vape["id"])
        except aiosqlite.Error:
            await db.rollback()
            logger.exception("Ошибка сбора Паров у игрока %s", user_id)
            raise
        finally:
            await db.close()

    new_balance: int = int((await get_user(user_id) or {"balance": 0})["balance"])
    message: str = (
        f"💨 Ты собрал {vapors} Паров ({current_puffs} затяжек × "
        f"{vape['puffs_per_vapor']} Паров, множитель x{vape['multiplier']}).\n"
        f"⚖️ Баланс: {new_balance} Паров.\n"
        f"♻️ Бак восстановится через {config.regen_time_minutes} мин."
    )
    return "ok", message


async def refill_tanks() -> list[dict[str, Any]]:
    """Фоновая задача: заполняет баки и применяет износ по истечении REGEN_TIME_MINUTES.

    Для каждого экипированного подика с current_puffs = 0 и наступившим
    regen_at: бак заполняется до max_puffs, condition уменьшается на
    случайное число от WEAR_MIN_PERCENT до WEAR_MAX_PERCENT. Если
    condition <= 0 — подик ломается (is_broken = 1, condition = 0),
    equipped_vape_id владельца обнуляется.

    Returns:
        Список словарей «сломанных в этом цикле» подиков для уведомлений.
    """
    candidates = await fetch_all(
        """
        SELECT * FROM vapes
        WHERE is_equipped = 1 AND is_broken = 0
          AND current_puffs = 0
          AND regen_at IS NOT NULL
          AND regen_at <= strftime('%Y-%m-%d %H:%M:%S', 'now')
        """
    )
    broken: list[dict[str, Any]] = []
    for vape in candidates:
        wear: int = random.randint(config.wear_min_percent, config.wear_max_percent)
        new_condition: int = max(0, int(vape["condition"]) - wear)
        is_now_broken: bool = new_condition <= 0
        async with aiosqlite.connect(DATABASE_PATH) as db:
            try:
                cursor = await db.execute(
                    """
                    UPDATE vapes
                    SET current_puffs = max_puffs,
                        condition = ?,
                        is_broken = ?,
                        regen_at = NULL
                    WHERE id = ? AND is_broken = 0 AND current_puffs = 0
                    """,
                    (new_condition, 1 if is_now_broken else 0, vape["id"]),
                )
                if cursor.rowcount == 0:
                    # Подик уже обработан параллельно — пропускаем
                    await db.rollback()
                    continue
                if is_now_broken:
                    # Сбрасываем экипировку у владельца сломанного подика
                    await db.execute(
                        "UPDATE users SET equipped_vape_id = NULL "
                        "WHERE user_id = ? AND equipped_vape_id = ?",
                        (vape["owner_id"], vape["id"]),
                    )
                    broken.append({**vape, "condition": 0})
                await db.commit()
            except aiosqlite.Error:
                await db.rollback()
                logger.exception("Ошибка регенерации подика #%s", vape["id"])
            finally:
                await db.close()
    if broken:
        logger.info("Сломано подиков за цикл регенерации: %s", len(broken))
    return broken


async def equip_vape(user_id: int, vape_id: int) -> tuple[bool, str]:
    """Экипирует подик. Запрещено экипировать сломанный или чужой подик.

    Returns:
        (успех, сообщение).
    """
    vape = await fetch_one(
        "SELECT * FROM vapes WHERE id = ? AND owner_id = ?", (vape_id, user_id)
    )
    if vape is None:
        return False, "Подик не найден в твоём инвентаре."
    if vape["is_broken"]:
        return False, "Нельзя экипировать сломанный подик! 🗑 Его можно только выбросить."

    # Транзакция: снять экипировку со всех подиков, надеть на выбранный
    async with aiosqlite.connect(DATABASE_PATH) as db:
        try:
            await db.execute("UPDATE vapes SET is_equipped = 0 WHERE owner_id = ?", (user_id,))
            await db.execute("UPDATE vapes SET is_equipped = 1 WHERE id = ?", (vape_id,))
            await db.execute(
                "UPDATE users SET equipped_vape_id = ? WHERE user_id = ?", (vape_id, user_id)
            )
            await db.commit()
        except aiosqlite.Error:
            await db.rollback()
            logger.exception("Ошибка экипировки подика #%s игроком %s", vape_id, user_id)
            return False, "Ошибка при экипировке, попробуй позже."
        finally:
            await db.close()
    return True, f"💨 Подик «{vape['name']}» экипирован!"


async def discard_vape(user_id: int, vape_id: int) -> tuple[bool, str]:
    """Выбрасывает (удаляет) подик. Разрешено только для сломанных подиков.

    Returns:
        (успех, сообщение).
    """
    vape = await fetch_one(
        "SELECT * FROM vapes WHERE id = ? AND owner_id = ?", (vape_id, user_id)
    )
    if vape is None:
        return False, "Подик не найден в твоём инвентаре."
    if not vape["is_broken"]:
        return False, "Нельзя выбросить рабочий подик! 🚫 Удаляются только сломанные."

    await execute_query(
        "DELETE FROM vapes WHERE id = ? AND owner_id = ? AND is_broken = 1",
        (vape_id, user_id),
    )
    return True, f"🗑 Подик «{vape['name']}» выброшен на свалку истории."


async def give_auction_vape(
    user_id: int,
    name: str,
    multiplier: int,
    power: int,
    max_puffs: int,
    condition: int,
) -> bool:
    """Выдаёт победителю аукциона подик в инвентарь.

    Если инвентарь полон (MAX_VAPES_PER_USER штук) — выдача запрещена,
    возвращается False (возврат Паров и реролл лота решает auction_service).

    Returns:
        True, если подик создан; False, если мест нет.
    """
    count: int = await count_user_vapes(user_id)
    if count >= config.max_vapes_per_user:
        logger.info(
            "Инвентарь игрока %s полон (%s/%s), выдача невозможна",
            user_id, count, config.max_vapes_per_user,
        )
        return False

    # Валидация: состояние не может быть ниже 0 и выше 100
    safe_condition: int = min(100, max(0, int(condition)))
    vape_id = await execute_query(
        """
        INSERT INTO vapes (owner_id, name, multiplier, power, max_puffs,
                           current_puffs, condition, puffs_per_vapor, is_equipped, is_broken)
        VALUES (?, ?, ?, ?, ?, 0, ?, ?, 0, 0)
        """,
        (user_id, name, multiplier, power, max_puffs, safe_condition, config.puffs_per_vapor),
    )
    logger.info("Игрок %s получил подик #%s (%s) с аукциона", user_id, vape_id, name)
    return True


async def admin_give_vape(
    user_id: int,
    name: str,
    multiplier: int,
    power: int,
    max_puffs: int,
    condition: int,
    quantity: int = 1,
) -> tuple[bool, str]:
    """Выдаёт подик(и) игроку по указанию администратора (админ-панель).

    В отличие от auction-выдачи, игрок НЕ обязан быть зарегистрирован —
    при необходимости он регистрируется автоматически (без стартового подика,
    чтобы выдача была честной: ровно то, что выдал админ).

    Args:
        user_id: Telegram ID получателя.
        name: название подика.
        multiplier: множитель дохода.
        power: мощность (затяжек/мин).
        max_puffs: объём бака.
        condition: состояние в %.
        quantity: сколько копий выдать (1..10).

    Returns:
        (успех, сообщение для админа).
    """
    if not name.strip():
        return False, "Название подика не может быть пустым."
    for label, value, low, high in (
        ("множитель", multiplier, *LOT_PARAM_LIMITS["multiplier"]),
        ("мощность", power, *LOT_PARAM_LIMITS["power"]),
        ("бак", max_puffs, *LOT_PARAM_LIMITS["max_puffs"]),
        ("состояние", condition, *LOT_PARAM_LIMITS["condition"]),
        ("количество", quantity, *LOT_PARAM_LIMITS["quantity"]),
    ):
        if not low <= int(value) <= high:
            return False, f"Параметр «{label}»={value} вне диапазона {low}..{high}."

    user = await get_user(user_id)
    if user is None:
        # Регистрируем игрока без стартового подика: создаём только запись users
        await execute_query(
            "INSERT INTO users (user_id, username, balance, last_collect_time) "
            "VALUES (?, NULL, 0, ?)",
            (user_id, fmt_dt(now_utc())),
        )
        logger.info("Админ-выдача: зарегистрирован игрок %s без стартового подика", user_id)

    count: int = await count_user_vapes(user_id)
    free: int = config.max_vapes_per_user - count
    if free <= 0:
        return False, (f"Инвентарь игрока {user_id} полон ({count}/{config.max_vapes_per_user}). "
                       f"Сначала удали часть подиков (⛏ Забрать подики).")
    to_give: int = min(int(quantity), free)

    safe_condition: int = min(100, max(0, int(condition)))
    given_ids: list[int] = []
    for _ in range(to_give):
        vape_id = await execute_query(
            """
            INSERT INTO vapes (owner_id, name, multiplier, power, max_puffs,
                               current_puffs, condition, puffs_per_vapor, is_equipped, is_broken)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, 0, 0)
            """,
            (user_id, name.strip(), int(multiplier), int(power), int(max_puffs),
             safe_condition, config.puffs_per_vapor),
        )
        given_ids.append(int(vape_id))

    # Если у игрока нет экипированного подика — экипируем первый выданный
    equipped_now: bool = False
    if await get_equipped_vape(user_id) is None and given_ids:
        ok_equip, _ = await equip_vape(user_id, given_ids[0])
        equipped_now = ok_equip

    skipped: int = int(quantity) - to_give
    msg: str = (f"✅ Выдано {to_give} шт «{name.strip()}» игроку {user_id}"
                + ("" if skipped == 0 else f" (пропущено {skipped} — нет места в инвентаре)"))
    if equipped_now:
        msg += "\n💨 Первый подик экипирован автоматически."
    logger.info("Админ-выдача подика игроку %s: %s", user_id, msg)
    return True, msg


async def admin_remove_vape(user_id: int, vape_id: int) -> tuple[bool, str]:
    """Изымает (удаляет) конкретный подик игрока по указанию администратора.

    В отличие от discard_vape, ограничение «только сломанные» не действует —
    админ может забрать любой подик. Если подик был экипирован — экипировка
    владельца сбрасывается.

    Returns:
        (успех, сообщение для админа).
    """
    vape = await fetch_one("SELECT * FROM vapes WHERE id = ?", (vape_id,))
    if vape is None:
        return False, f"Подик #{vape_id} не найден."
    if int(vape["owner_id"]) != user_id:
        return False, (f"Подик #{vape_id} принадлежит игроку {vape['owner_id']}, "
                       f"а не {user_id}. Проверь ID.")

    async with aiosqlite.connect(DATABASE_PATH) as db:
        try:
            await db.execute("DELETE FROM vapes WHERE id = ? AND owner_id = ?", (vape_id, user_id))
            await db.execute(
                "UPDATE users SET equipped_vape_id = NULL WHERE user_id = ? AND equipped_vape_id = ?",
                (user_id, vape_id),
            )
            await db.commit()
        except aiosqlite.Error:
            await db.rollback()
            logger.exception("Ошибка изъятия подика #%s у игрока %s", vape_id, user_id)
            return False, "Ошибка при изъятии, попробуй позже."
        finally:
            await db.close()
    logger.info("Админ изъял подик #%s («%s») у игрока %s", vape_id, vape["name"], user_id)
    return True, f"🗑 Изят подик #{vape_id} «{vape['name']}» у игрока {user_id}."
