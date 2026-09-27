"""Логирование входящих апдейтов.

Позволяет, в частности, узнать ID любого чата/канала: достаточно
переслать любое сообщение из канала боту (или добавить его в канал),
и в логах появится строка вида:

    Update from chat id=-1001234567890 type=channel ...
"""

import logging

from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, TelegramObject

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        try:
            if isinstance(event, Message):
                chat = event.chat
                logger.info(
                    "Message in chat id=%s type=%s title=%r from user id=%s",
                    chat.id, chat.type, getattr(chat, "title", None),
                    event.from_user.id if event.from_user else None,
                )
            elif isinstance(event, CallbackQuery) and event.message:
                chat = event.message.chat
                logger.info(
                    "Callback in chat id=%s type=%s title=%r from user id=%s",
                    chat.id, chat.type, getattr(chat, "title", None),
                    event.from_user.id if event.from_user else None,
                )
        except Exception:  # логгер не должен ронять бота
            logger.exception("Failed to log update")
        return await handler(event, data)
