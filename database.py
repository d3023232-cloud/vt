"""Модуль инициализации базы данных SQLite через асинхронную библиотеку aiosqlite.

Схема БД:
    users    — игроки и их баланс Паров;
    vapes    — подики (устройства добычи);
    auctions — аукционы (лоты, партии);
    bids     — ставки игроков.

Все соединения открываются через контекстный менеджер aiosqlite.connect,
что гарантирует отсутствие утечек и незакрытых соединений.
"""

import logging
from typing import Any

import aiosqlite

from config import config

logger = logging.getLogger(__name__)

# Путь к файлу базы данных берётся из .env
DATABASE_PATH: str = config.database_path

# SQL-схема всех четырёх таблиц
SCHEMA_SQL: str = """
CREATE TABLE IF NOT EXISTS users (
    user_id           INTEGER PRIMARY KEY,                -- Telegram ID пользователя
    username          TEXT,                               -- @username игрока
    balance           INTEGER DEFAULT 0,                  -- количество Паров
    last_collect_time TIMESTAMP,                          -- время последнего сбора
    equipped_vape_id  INTEGER,                            -- ID экипированного подика
    registered_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (equipped_vape_id) REFERENCES vapes(id)
);

CREATE TABLE IF NOT EXISTS vapes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER,                              -- FOREIGN KEY на users.user_id
    name            TEXT NOT NULL,                        -- название подика
    multiplier      INTEGER DEFAULT 1,                    -- множитель к добыче
    power           INTEGER NOT NULL,                     -- Паров (затяжек) в минуту
    max_puffs       INTEGER NOT NULL,                     -- максимальное количество затяжек
    current_puffs   INTEGER DEFAULT 0,                    -- текущие затяжки
    condition       INTEGER DEFAULT 100,                  -- состояние в процентах 0..100
    puffs_per_vapor INTEGER DEFAULT 5,                    -- Паров за одну затяжку
    is_equipped     BOOLEAN DEFAULT 0,                    -- экипирован ли
    is_broken       BOOLEAN DEFAULT 0,                    -- сломан ли
    regen_at        TIMESTAMP,                            -- время следующей регенерации
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (owner_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS auctions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,  -- номер аукциона (уникален, не повторяется)
    lot_name          TEXT NOT NULL,                      -- название лота
    multiplier        INTEGER NOT NULL,
    power             INTEGER NOT NULL,
    max_puffs         INTEGER NOT NULL,
    condition         INTEGER NOT NULL,
    quantity          INTEGER NOT NULL,                   -- количество штук в партии
    lots_sold         INTEGER DEFAULT 0,                  -- сколько уже продано
    start_price       INTEGER NOT NULL,                   -- стартовая цена
    status            TEXT DEFAULT 'announced',           -- announced | active | finished | cancelled
    channel_id        INTEGER NOT NULL,                   -- ID канала
    message_id        INTEGER,                            -- ID поста в канале
    current_lot_number INTEGER DEFAULT 1,                 -- текущий лот из партии
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    starts_at         TIMESTAMP,                          -- момент старта торгов (для обратного отсчёта в анонсе)
    started_at        TIMESTAMP,
    finished_at       TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bids (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    auction_id  INTEGER NOT NULL,                         -- FOREIGN KEY на auctions.id
    lot_number  INTEGER NOT NULL,                         -- номер лота из партии
    user_id     INTEGER NOT NULL,                         -- FOREIGN KEY на users.user_id
    amount      INTEGER NOT NULL,                         -- сумма ставки
    is_winning  BOOLEAN DEFAULT 0,                        -- является ли текущей ведущей ставкой
    is_refunded BOOLEAN DEFAULT 0,                        -- возвращены ли Пары
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (auction_id) REFERENCES auctions(id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_vapes_owner ON vapes(owner_id);
CREATE INDEX IF NOT EXISTS idx_bids_auction_lot ON bids(auction_id, lot_number);
CREATE INDEX IF NOT EXISTS idx_auctions_status ON auctions(status);
"""


async def init_db() -> None:
    """Создаёт все таблицы при первом запуске бота и выполняет мягкие миграции."""
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.executescript(SCHEMA_SQL)
            # Мягкая миграция: добавляем starts_at, если колонки ещё нет (старые БД)
            cursor = await db.execute("PRAGMA table_info(auctions)")
            columns = [row[1] for row in await cursor.fetchall()]
            if "starts_at" not in columns:
                await db.execute("ALTER TABLE auctions ADD COLUMN starts_at TIMESTAMP")
                logger.info("Миграция: добавлена колонка auctions.starts_at")
            await db.commit()
        logger.info("База данных инициализирована: %s", DATABASE_PATH)
    except aiosqlite.Error:
        logger.exception("Ошибка инициализации базы данных")
        raise


async def fetch_one(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    """Выполняет запрос и возвращает одну строку в виде словаря (или None)."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
        return dict(row) if row is not None else None


async def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Выполняет запрос и возвращает список строк-словарей."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def execute_query(query: str, params: tuple[Any, ...] = ()) -> int:
    """Выполняет INSERT/UPDATE/DELETE, фиксирует транзакцию, возвращает lastrowid."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        try:
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor.lastrowid or 0
        except aiosqlite.Error:
            await db.rollback()
            logger.exception("Ошибка выполнения запроса: %s", query)
            raise
        finally:
            await db.close()
