"""Модуль загрузки конфигурации из файла .env.

Все константы бота загружаются исключительно из переменных окружения,
хардкод значений в коде запрещён.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Загружаем переменные из файла .env в корне проекта
load_dotenv()


def _get_int(name: str, default: int | None = None) -> int:
    """Безопасно читает целочисленную переменную окружения.

    Args:
        name: имя переменной окружения.
        default: значение по умолчанию (если оно разрешено).

    Returns:
        Целое число из переменой окружения.

    Raises:
        ValueError: если переменная не задана или содержит нечисловое значение.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        if default is not None:
            return default
        raise ValueError(f"Переменная окружения {name} не задана")
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Переменная окружения {name} должна быть числом, получено: {raw!r}") from exc


@dataclass(frozen=True)
class Config:
    """Неизменяемая конфигурация приложения."""

    bot_token: str
    admin_id: int
    database_path: str
    auction_channel_id: int
    auction_channel_username: str
    puffs_per_vapor: int
    regen_time_minutes: int
    wear_min_percent: int
    wear_max_percent: int
    max_vapes_per_user: int
    auction_announce_minutes: int
    bid_timer_seconds: int
    lot_break_minutes: int


def load_config() -> Config:
    """Читает и валидирует все переменные окружения.

    Returns:
        Объект Config со всеми параметрами бота.

    Raises:
        ValueError: если обязательная переменная отсутствует либо
            параметры находятся в противоречивых диапазонах.
    """
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("BOT_TOKEN не задан в файле .env")

    cfg = Config(
        bot_token=token,
        admin_id=_get_int("ADMIN_ID"),
        database_path=os.getenv("DATABASE_PATH", "vape_tycoon.db").strip() or "vape_tycoon.db",
        auction_channel_id=_get_int("AUCTION_CHANNEL_ID"),
        auction_channel_username=os.getenv("AUCTION_CHANNEL_USERNAME", "").strip(),
        puffs_per_vapor=_get_int("PUFFS_PER_VAPOR", 5),
        regen_time_minutes=_get_int("REGEN_TIME_MINUTES", 30),
        wear_min_percent=_get_int("WEAR_MIN_PERCENT", 5),
        wear_max_percent=_get_int("WEAR_MAX_PERCENT", 30),
        max_vapes_per_user=_get_int("MAX_VAPES_PER_USER", 3),
        auction_announce_minutes=_get_int("AUCTION_ANNOUNCE_MINUTES", 15),
        bid_timer_seconds=_get_int("BID_TIMER_SECONDS", 30),
        lot_break_minutes=_get_int("LOT_BREAK_MINUTES", 15),
    )

    # Валидация диапазонов — защита от некорректного .env
    if cfg.puffs_per_vapor <= 0:
        raise ValueError("PUFFS_PER_VAPOR должен быть больше 0")
    if cfg.regen_time_minutes <= 0:
        raise ValueError("REGEN_TIME_MINUTES должен быть больше 0")
    if not (0 < cfg.wear_min_percent <= cfg.wear_max_percent <= 100):
        raise ValueError("Требование: 0 < WEAR_MIN_PERCENT <= WEAR_MAX_PERCENT <= 100")
    if cfg.max_vapes_per_user <= 0:
        raise ValueError("MAX_VAPES_PER_USER должен быть больше 0")
    if cfg.auction_announce_minutes <= 0:
        raise ValueError("AUCTION_ANNOUNCE_MINUTES должен быть больше 0")
    if cfg.bid_timer_seconds <= 0:
        raise ValueError("BID_TIMER_SECONDS должен быть больше 0")
    if cfg.lot_break_minutes <= 0:
        raise ValueError("LOT_BREAK_MINUTES должен быть больше 0")
    if cfg.auction_channel_id == 0:
        raise ValueError("AUCTION_CHANNEL_ID не задан корректно (формат -100xxxxxxxxxx)")

    return cfg


# Глобальный экземпляр конфигурации (создаётся один раз при импорте)
config: Config = load_config()
