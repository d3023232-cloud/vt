"""Точка входа Vape Tycoon Bot: инициализация БД, роутеров, middleware и запуск long polling."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from config import config
from database import init_db
from handlers import admin, auction, inventory, start
from middlewares.admin_check import AdminCheckMiddleware
from middlewares.logging_middleware import LoggingMiddleware
from services import scheduler_service

logger = logging.getLogger(__name__)


async def set_bot_commands(bot: Bot) -> None:
    """Регистрирует список команд в меню Telegram."""
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Главный экран"),
            BotCommand(command="inventory", description="Инвентарь подиков"),
            BotCommand(command="auction", description="Текущий аукцион"),
            BotCommand(command="help", description="Как играть"),
        ]
    )


async def main() -> None:
    """Создаёт бота и диспетчер, подключает все роутеры и запускает polling."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    # APScheduler спамит INFO-логами каждые 5 секунд (Running/executed _timer_job) —
    # оставляем только предупреждения и ошибки планировщика.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    # Инициализация базы данных (создание таблиц при первом запуске)
    await init_db()

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp["bot"] = bot
    # Middleware проверки прав администратора для всех входящих событий
    dp.message.middleware(AdminCheckMiddleware())
    dp.callback_query.middleware(AdminCheckMiddleware())

    # Логирование ID чатов (позволяет узнать ID канала: перешлите сообщение из него боту)
    dp.message.outer_middleware(LoggingMiddleware())
    dp.callback_query.outer_middleware(LoggingMiddleware())

    # Подключение роутеров. Порядок важен: роутер /start подключаем последним,
    # чтобы его широкие callback-фильтры не перехватывали чужие события раньше времени.
    dp.include_router(admin.router)
    dp.include_router(auction.router)
    dp.include_router(inventory.router)
    dp.include_router(start.router)

    await set_bot_commands(bot)
    # Запуск фоновых задач планировщика (добыча, регенерация, таймеры аукциона)
    scheduler_service.init_scheduler(bot)

    logger.info("Бот запущен, ожидаю обновления...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        scheduler_service.shutdown_scheduler()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
