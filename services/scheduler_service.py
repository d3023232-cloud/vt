"""Сервис планировщика APScheduler 3.x.

Фоновые задачи:
    * каждую минуту — генерация затяжек (power зат./мин) и регенерация баков;
    * каждые 5 секунд — обновление таймера в посте активного аукциона;
    * разовые date-задачи — старт торгов, закрытие лота, старт следующего лота;
    * уведомление игроков о поломке подиков.

Все задачи — асинхронные корутины, запускаются AsyncIOExecutor внутри
event-loop бота. Никакого time.sleep — только asyncio/APSchedular.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Coroutine

from aiogram import Bot
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import config
from services import auction_service, vape_service

logger = logging.getLogger(__name__)

# Планировщик APScheduler 3.x (AsyncIOScheduler работает в event-loop)
scheduler: AsyncIOScheduler = AsyncIOScheduler(
    jobstores={"default": MemoryJobStore()},
    executors={"default": AsyncIOExecutor()},
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
)

# Ссылка на Bot для отправки сообщений из фоновых задач
_bot: Bot | None = None

# Дедлайны закрытия текущих лотов: auction_id -> datetime (наивное локальное время,
# совпадающее с datetime.now(), который использует планировщик)
BID_DEADLINES: dict[int, datetime] = {}


def _job_id(prefix: str, auction_id: int) -> str:
    """Уникальный ID задачи планировщика для аукциона."""
    return f"{prefix}:{auction_id}"


async def _puff_job() -> None:
    """Задача раз в минуту: начисление затяжек + регенерация баков + уведомления о поломках."""
    assert _bot is not None
    try:
        await vape_service.generate_puffs()
        broken = await vape_service.refill_tanks()
        for vape in broken:
            try:
                await _bot.send_message(
                    chat_id=int(vape["owner_id"]),
                    text=(
                        f"💥 Твой подик «{vape['name']}» СЛОМАЛСЯ!\n"
                        f"Он больше не добывает Пары. Выброси его в /inventory "
                        f"и экипируй новый — или дождись редкого лота на аукционе "
                        f"@{config.auction_channel_username}."
                    ),
                )
            except Exception:  # noqa: BLE001 — доставка уведомления не должна ронять задачу
                logger.warning("Не удалось отправить уведомление о поломке игроку %s", vape["owner_id"])
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка задачи генерации затяжек")


async def _timer_job() -> None:
    """Задача каждые 5 секунд: обновляет таймер в посте активного аукциона."""
    assert _bot is not None
    try:
        await auction_service.refresh_timer(_bot)
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка задачи обновления таймера")


async def _announce_timer_job() -> None:
    """Задача раз в минуту: обновляет обратный отсчёт до старта торгов в посте-анонсе.

    Пока до старта больше минуты, пост обновляется раз в минуту (число минут
    меняется); посекундный отсчёт последних 60 секунд обеспечивает _timer_job.
    """
    assert _bot is not None
    try:
        await auction_service.update_announce_countdown(_bot)
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка задачи обновления анонса")


async def _start_bidding_job(auction_id: int) -> None:
    """Разовая задача: старт торгов по анонсированному аукциону."""
    assert _bot is not None
    try:
        await auction_service.start_bidding(_bot, auction_id)
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка старта торгов аукциона #%s", auction_id)


async def _close_lot_job(auction_id: int) -> None:
    """Разовая задача: закрытие текущего лота (продажа/повтор)."""
    assert _bot is not None
    try:
        await auction_service.close_lot(_bot, auction_id)
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка закрытия лота аукциона #%s", auction_id)


async def _next_lot_job(auction_id: int, same_lot: bool) -> None:
    """Разовая задача: старт следующего (или повтор того же) лота после перерыва."""
    assert _bot is not None
    try:
        await auction_service.start_next_lot(_bot, auction_id, same_lot)
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка старта следующего лота аукциона #%s", auction_id)


def init_scheduler(bot: Bot) -> None:
    """Сохраняет ссылку на бота, регистрирует периодические задачи, запускает планировщик."""
    global _bot
    _bot = bot
    # Каждую минуту — добыча/регенерация
    scheduler.add_job(_puff_job, "interval", minutes=1, id="puff_job", replace_existing=True)
    # Каждые 5 секунд — обновление таймера аукциона
    scheduler.add_job(_timer_job, "interval", seconds=5, id="timer_job", replace_existing=True)
    # Каждую минуту — обновление обратного отсчёта в посте-анонсе
    scheduler.add_job(_announce_timer_job, "interval", minutes=1, id="announce_timer_job",
                      replace_existing=True)
    if not scheduler.running:
        scheduler.start()
    logger.info("APScheduler запущен (добыча: 1 мин, таймер: 5 сек)")


def shutdown_scheduler() -> None:
    """Останавливает планировщик при завершении работы бота (без утечек задач)."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler остановлен")


def schedule_auction_start(auction_id: int, delay_seconds: int) -> None:
    """Планирует старт торгов через delay_seconds секунд после анонса."""
    run_at: datetime = datetime.now() + timedelta(seconds=delay_seconds)
    scheduler.add_job(
        _start_bidding_job, "date", run_date=run_at, args=[auction_id],
        id=_job_id("start", auction_id), replace_existing=True,
    )
    logger.info("Старт торгов аукциона #%s запланирован на %s", auction_id, run_at)


def schedule_lot_close(auction_id: int, delay_seconds: int) -> None:
    """Планирует закрытие текущего лота через delay_seconds секунд."""
    scheduler.add_job(
        _close_lot_job, "date",
        run_date=datetime.now() + timedelta(seconds=delay_seconds),
        args=[auction_id], id=_job_id("close", auction_id), replace_existing=True,
    )


def reschedule_lot_close(auction_id: int, delay_seconds: int) -> None:
    """Сбрасывает таймер лота (вызывается при каждой новой ставке)."""
    schedule_lot_close(auction_id, delay_seconds)


def schedule_next_lot(auction_id: int, delay_minutes: int, same_lot: bool) -> None:
    """Планирует старт следующего (или повтор того же) лота через перерыв LOT_BREAK_MINUTES."""
    scheduler.add_job(
        _next_lot_job, "date", run_date=datetime.now() + timedelta(minutes=delay_minutes),
        args=[auction_id, same_lot], id=_job_id("nextlot", auction_id), replace_existing=True,
    )
    logger.info("Аукцион #%s: следующий лот через %s мин (повтор=%s)",
                auction_id, delay_minutes, same_lot)


def reset_bid_deadline(auction_id: int, seconds: int) -> None:
    """Устанавливает дедлайн окончания лота (для отображения остатка времени в посте)."""
    BID_DEADLINES[auction_id] = datetime.now() + timedelta(seconds=seconds)


def clear_bid_deadline(auction_id: int) -> None:
    """Убирает дедлайн лота (лот закрыт) — предотвращает утечку памяти словаря."""
    BID_DEADLINES.pop(auction_id, None)


def cancel_auction_jobs(auction_id: int) -> None:
    """Отменяет все запланированные задачи APScheduler для конкретного аукциона."""
    clear_bid_deadline(auction_id)
    for prefix in ("start", "close", "nextlot"):
        job_id: str = _job_id(prefix, auction_id)
        try:
            scheduler.remove_job(job_id)
        except Exception:  # noqa: BLE001 — JobLookupError, если задачи уже нет
            pass
    logger.info("Задачи аукциона #%s отменены", auction_id)
