import asyncio, logging
from datetime import datetime
logging.basicConfig(level=logging.DEBUG)

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Update, Message, Chat, User

from database import init_db
from handlers import admin, auction, inventory, start
from middlewares.admin_check import AdminCheckMiddleware
from middlewares.logging_middleware import LoggingMiddleware

TOKEN = "123456:TEST"

class MockSession(BaseSession):
    def __init__(self): super().__init__()
    async def close(self): pass
    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendMessage):
            print(f"\n>>> BOT ANSWER:\n{method.text[:600]}\n<<<")
        return None
    async def stream_content(self, *a, **k): pass

async def main():
    await init_db()
    bot = Bot(token=TOKEN, session=MockSession())
    dp = Dispatcher()
    dp["bot"] = bot
    dp.message.middleware(AdminCheckMiddleware())
    dp.callback_query.middleware(AdminCheckMiddleware())
    dp.message.outer_middleware(LoggingMiddleware())
    dp.callback_query.outer_middleware(LoggingMiddleware())
    dp.include_router(admin.router)
    dp.include_router(auction.router)
    dp.include_router(inventory.router)
    dp.include_router(start.router)

    chat = Chat(id=8319217707, type="private")
    usr = User(id=8319217707, is_bot=False, first_name="Test")
    msg = Message(message_id=1, date=datetime(2026,9,28), chat=chat, from_user=usr, text="/start",
                  is_command=True, entities=None)
    upd = Update(update_id=1, message=msg)
    handled = await dp.feed_update(bot, upd)
    print("feed_update result:", handled)

    # второй /start (уже зарегистрирован)
    msg2 = Message(message_id=2, date=datetime(2026,9,28), chat=chat, from_user=usr, text="/start",
                   is_command=True, entities=None)
    await dp.feed_update(bot, Update(update_id=2, message=msg2))

    # /help
    msg3 = Message(message_id=3, date=datetime(2026,9,28), chat=chat, from_user=usr, text="/help",
                   is_command=True, entities=None)
    await dp.feed_update(bot, Update(update_id=3, message=msg3))

asyncio.run(main())
