"""Middleware проверки прав администратора.

Доступ к /admin и всем админ-действиям разрешён только пользователю,
чьё Telegram ID совпадает с ADMIN_ID из файла .env. Остальные команды
игнорируются (сообщение не обрабатывается дальше).
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from config import config

logger = logging.getLogger(__name__)

# Префиксы колбэков, которые относятся исключительно к админ-панели
ADMIN_CALLBACK_PREFIXES: tuple[str, ...] = ("admin_", "confirm:")


def is_admin(user_id: int) -> bool:
    """Проверяет, является ли пользователь администратором (ADMIN_ID из .env)."""
    return user_id == config.admin_id


class AdminCheckMiddleware(BaseMiddleware):
    """Блокирует админ-команды и админ-колбэки для всех, кроме ADMIN_ID."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id: int | None = None
        text: str | None = None

        if isinstance(event, Message):
            user_id = event.from_user.id if event.from_user else None
            text = event.text or ""
        elif isinstance(event, CallbackQuery):
            user_id = event.from_user.id if event.from_user else None
            text = event.data or ""

        # Передаём флаг админа дальше в хендлеры
        data["is_admin"] = bool(user_id is not None and is_admin(user_id))

        if user_id is None:
            return None

        # Команда /admin — строго для ADMIN_ID, остальных игнорируем молча
        if isinstance(event, Message) and text:
            command: str = text.split()[0].split("@")[0].lower()
            if command in ("/admin", "/helpadmin") and not is_admin(user_id):
                logger.warning("Попытка доступа к /admin от неадмина: %s", user_id)
                return None

        # Админские колбэки — тоже строго для ADMIN_ID
        if isinstance(event, CallbackQuery) and text:
            if any(text.startswith(p) for p in ADMIN_CALLBACK_PREFIXES) and not is_admin(user_id):
                logger.warning("Попытка админ-колбэка от неадмина: %s (%s)", user_id, text)
                await event.answer("⛔ Недостаточно прав.", show_alert=True)
                return None

        return await handler(event, data)
