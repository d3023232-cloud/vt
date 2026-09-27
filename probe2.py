import asyncio, logging
from datetime import datetime
logging.basicConfig(level=logging.INFO)

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Update, Message, Chat, User

from database import init_db
from handlers import admin, auction, inventory, start
from middlewares.admin_check import AdminCheckMiddleware
from middlewares.logging_middleware import LoggingMiddleware

class MockSession(BaseSession):
    def __init__(self): super().__init__()
    async def close(self): pass
    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendMessage):
            print(f">>> BOT ANSWER:\n{method.text[:300]}")
        return None
    async def stream_content(self, *a, **k): pass

async def probe(routers):
    await init_db()
    bot = Bot(token="123456:TEST", session=MockSession())
    dp = Dispatcher()
    dp["bot"] = bot
    dp.message.middleware(AdminCheckMiddleware())
    dp.callback_query.middleware(AdminCheckMiddleware())
    dp.message.outer_middleware(LoggingMiddleware())
    for r in routers:
        dp.include_router(r)
    chat = Chat(id=8319217707, type="private")
    usr = User(id=8319217707, is_bot=False, first_name="Test")
    msg = Message(message_id=1, date=datetime(2026,9,28), chat=chat, from_user=usr, text="/start")
    handled = await dp.feed_update(bot, Update(update_id=1, message=msg))
    names = [getattr(r, '__name__', '?') for r in routers]
    print(f"routers={names} -> result={handled}")
    await bot.session.close()

async def main():
    await probe([admin.router])
    await probe([start.router])
    await probe([admin.router, auction.router, inventory.router, start.router])

asyncio.run(main())
