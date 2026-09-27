import asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Update, Message, Chat, User, CallbackQuery

class FakeSession(BaseSession):
    async def close(self): pass
    async def stream_content(self, url, headers=None, timeout=30, chunk_size=4096, raise_for_status=True):
        yield b""
    async def make_request(self, bot, method, timeout=None, **kwargs):
        chat_id = getattr(method, "chat_id", 1) or 1
        return {"ok": True, "result": {
            "message_id": 1, "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 1, "is_bot": False, "first_name": "A"},
            "text": getattr(method, "text", "x")}}

async def main():
    from config import config
    from database import init_db
    await init_db()
    from handlers import admin as adm
    dp = Dispatcher()
    dp.include_router(adm.router)
    bot = Bot(config.bot_token, session=FakeSession())
    u = User(id=config.admin_id, is_bot=False, first_name="Admin", username="admin")
    c = Chat(id=config.admin_id, type="private")

    async def state_now():
        key = StorageKey(bot_id=bot.id, chat_id=config.admin_id, user_id=config.admin_id)
        raw = await dp.storage.get_state(key)
        return raw

    async def msg(text, mid):
        await dp.feed_update(bot, Update(update_id=mid, message=Message(
            message_id=mid, date=0, chat=c, from_user=u, text=text)))

    async def cb(data, mid):
        await dp.feed_update(bot, Update(update_id=mid, callback_query=CallbackQuery(
            id=f"q{mid}", from_user=u, message=Message(message_id=mid, date=0, chat=c,
            from_user=u, text="m"), data=data, chat_instance="")))

    await msg("/admin", 1)
    await cb("admin_create", 2)
    s = await state_now()
    print("state after admin_create:", s)
    assert s and "waiting_name" in s

    # Команда во время мастера: FSM сбрасывается, команда обрабатывается (ошибки нет)
    await msg("/auction", 3)
    s = await state_now()
    print("state after /auction command:", s)
    assert not (s and "waiting_name" in s), "команда не прервала мастер!"

    # Мастер заново + кнопка Отмена
    await cb("admin_create", 4)
    await cb("admin_cancel_wizard", 5)
    s = await state_now()
    print("state after cancel button:", s)
    assert s is None, "кнопка Отмена не сбросила FSM!"

    # Выдача/изъятие + отмена командой и кнопкой
    await cb("admin_give", 6)
    s = await state_now(); print("state after admin_give:", s)
    assert s and "waiting_user" in s
    await msg("/start", 7)
    s = await state_now(); print("state after /start during give:", s)
    assert not (s and "VapeGiveStates" in s)

    await cb("admin_remove", 8)
    s = await state_now(); print("state after admin_remove:", s)
    assert s and "VapeRemoveStates" in s
    await cb("admin_cancel_wizard", 9)
    s = await state_now(); print("state after cancel remove:", s)
    assert s is None
    print("ALL OK")

asyncio.run(main())
