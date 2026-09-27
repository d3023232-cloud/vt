import asyncio
from datetime import datetime
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Update, Message
from aiogram.fsm.context import FSMContext

from database import init_db
from middlewares.admin_check import AdminCheckMiddleware
from handlers import admin, auction, inventory, start
from handlers.admin import AuctionCreateStates

sent = []

class DummySession(BaseSession):
    async def close(self): pass
    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendMessage):
            sent.append((method.chat_id, method.text or ""))
        return None
    async def stream_content(self, url, headers=None, timeout=30, chunk_size=8192):
        yield b""

def mkmsg(mid, uid, text):
    return Message(message_id=mid, date=datetime(2026, 9, 28),
                   chat={"id": uid, "type": "private", "from": {"id": uid, "is_bot": False, "first_name": "U"}},
                   from_user={"id": uid, "is_bot": False, "first_name": "U"}, text=text)

async def main():
    await init_db()
    bot = Bot(token="123456:TEST", session=DummySession(), parse_mode=None)
    dp = Dispatcher()
    dp.message.middleware(AdminCheckMiddleware())
    dp.callback_query.middleware(AdminCheckMiddleware())
    dp.include_router(admin.router)
    dp.include_router(auction.router)
    dp.include_router(inventory.router)
    dp.include_router(start.router)

    ADMIN, USER = 1, 2
    from aiogram.fsm.key_builder import DefaultKeyBuilder
    key = DefaultKeyBuilder.with_event_type(update_type="message", chat_id=ADMIN, user_id=ADMIN)

    # 1. /start от юзера без состояния
    await dp.feed_update(bot, Update(update_id=1, message=mkmsg(1, USER, "/start")))
    assert sent and "Vape Tycoon" in sent[-1][1], f"START BROKEN: {sent}"
    print("OK 1: /start отвечает главному экрану")

    st = FSMContext(dp.fsm.storage, key)
    await st.set_state(AuctionCreateStates.waiting_price)
    await st.update_data({"name": "Ghost", "params_dict": {"multiplier": 7, "power": 35, "tank": 700, "condition": 100, "quantity": 1}})
    n = len(sent)
    await dp.feed_update(bot, Update(update_id=2, message=mkmsg(2, ADMIN, "/start")))
    texts = " ".join(t for _, t in sent[n:])
    assert "Мастер прерван" in texts, f"wizard_break не сработал: {texts!r}"
    assert "Vape Tycoon" in texts, f"/start после break не выполнен: {texts!r}"
    assert await st.get_state() is None, "FSM не очищен!"
    print("OK 2: команда во время мастера сбрасывает FSM и выполняется /start")

    # 3. /help во время мастера
    await st.set_state(AuctionCreateStates.waiting_name)
    n = len(sent)
    await dp.feed_update(bot, Update(update_id=3, message=mkmsg(3, ADMIN, "/help")))
    texts = " ".join(t for _, t in sent[n:])
    assert "Мастер прерван" in texts and await st.get_state() is None
    print("OK 3: /help во время мастера прерывает мастер и выполняется")

    # 4. Обычный текст во время мастера проходит в шаг (не перехватывается)
    await st.set_state(AuctionCreateStates.waiting_name)
    n = len(sent)
    await dp.feed_update(bot, Update(update_id=4, message=mkmsg(4, ADMIN, "Ghost Mod X")))
    cur = await st.get_state()
    assert cur and "waiting_params" in cur, f"шаг не перешёл дальше: {cur}"
    print("OK 4: обычный текст обработан шагом мастера ->", cur)
    await st.clear()

    # 5. Команды юзера работают
    for cmd in ("/inventory", "/auction", "/help"):
        n = len(sent)
        await dp.feed_update(bot, Update(update_id=10, message=mkmsg(10, USER, cmd)))
        assert sent[n:], f"{cmd} молчит"
    print("OK 5: /inventory, /auction, /help отвечают")
    print("ALL_E2E_OK")

asyncio.run(main())
