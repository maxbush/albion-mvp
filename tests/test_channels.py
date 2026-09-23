"""PR1 — каналы доставки: user_channels, router, TelegramSender."""

import pytest

from src.channels.base import ChannelButton, ChannelSender, SendResult
from src.channels import router
from src.channels.router import get_sender, register_sender, resolve
from src.channels.telegram import TelegramSender
from src.db.repository import UserChannelRepository, UserRepository


class FakeSender(ChannelSender):
    name = "whatsapp"

    def __init__(self):
        self.sent = []

    async def send(self, address, text, buttons=None):
        self.sent.append((address, text, buttons))
        return SendResult(ok=True, channel=self.name, message_id="wamid.1")


class FakeBotMessage:
    def __init__(self, message_id=777):
        self.message_id = message_id


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return FakeBotMessage()


@pytest.fixture(autouse=True)
def clean_registry():
    saved = dict(router._senders)
    router._senders.clear()
    yield
    router._senders.clear()
    router._senders.update(saved)


# ── UserChannelRepository ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_channel_set_and_preferred(db):
    repo = UserChannelRepository(db)
    uid = await UserRepository(db).create("100", "parent", "Мама")

    await repo.set(uid, "telegram", "100")
    await repo.set(uid, "whatsapp", "+79990001122")

    pref = await repo.preferred_for_user(uid)
    assert pref["channel"] == "whatsapp"
    assert pref["address"] == "+79990001122"
    # is_preferred=1 ровно у одного канала
    chans = await repo.list_for_user(uid)
    assert sorted(c["channel"] for c in chans) == ["telegram", "whatsapp"]
    assert sum(c["is_preferred"] for c in chans) == 1


@pytest.mark.asyncio
async def test_user_channel_preferred_switch(db):
    repo = UserChannelRepository(db)
    uid = await UserRepository(db).create("100", "parent", "Мама")
    await repo.set(uid, "telegram", "100")
    await repo.set(uid, "whatsapp", "+79990001122")
    await repo.set(uid, "telegram", "100", preferred=True)

    pref = await repo.preferred_for_user(uid)
    assert pref["channel"] == "telegram"


@pytest.mark.asyncio
async def test_find_by_address(db):
    repo = UserChannelRepository(db)
    uid = await UserRepository(db).create("100", "parent", "Мама")
    await repo.set(uid, "whatsapp", "+79990001122")

    row = await repo.find_by_address("whatsapp", "+79990001122")
    assert row["user_id"] == uid
    assert await repo.find_by_address("whatsapp", "+70000000000") is None


# ── Router.resolve ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_fallback_telegram(db):
    """Нет user_channels → telegram по telegram_id."""
    uid = await UserRepository(db).create("100", "parent", "Мама")
    assert uid
    assert await resolve(telegram_id="100", db_path=db) == ("telegram", "100")


@pytest.mark.asyncio
async def test_resolve_unknown_user(db):
    """Пользователя вообще нет в users → всё равно telegram."""
    assert await resolve(telegram_id="999", db_path=db) == ("telegram", "999")


@pytest.mark.asyncio
async def test_resolve_preferred_without_sender_falls_back(db):
    """Preferred whatsapp, но отправителя нет → telegram, а не в пустоту."""
    repo = UserChannelRepository(db)
    uid = await UserRepository(db).create("100", "parent", "Мама")
    await repo.set(uid, "whatsapp", "+79990001122")
    await repo.set(uid, "telegram", "100", preferred=False)

    assert await resolve(telegram_id="100", db_path=db) == ("telegram", "100")


@pytest.mark.asyncio
async def test_resolve_preferred_with_sender(db):
    """Preferred whatsapp + зарегистрированный sender → whatsapp."""
    repo = UserChannelRepository(db)
    uid = await UserRepository(db).create("100", "parent", "Мама")
    await repo.set(uid, "whatsapp", "+79990001122")
    await repo.set(uid, "telegram", "100", preferred=False)
    register_sender(FakeSender())

    assert await resolve(telegram_id="100", db_path=db) == ("whatsapp", "+79990001122")


@pytest.mark.asyncio
async def test_resolve_by_user_id(db):
    repo = UserChannelRepository(db)
    uid = await UserRepository(db).create("100", "parent", "Мама")
    await repo.set(uid, "whatsapp", "+79990001122")
    register_sender(FakeSender())

    assert await resolve(user_id=uid, db_path=db) == ("whatsapp", "+79990001122")


# ── TelegramSender ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_sender_plain(db):
    bot = FakeBot()
    sender = TelegramSender(bot)
    res = await sender.send("42", "Привет")
    assert res.ok and res.channel == "telegram" and res.message_id == "777"
    assert bot.sent[0]["reply_markup"] is None


@pytest.mark.asyncio
async def test_telegram_sender_buttons(db):
    bot = FakeBot()
    sender = TelegramSender(bot)
    buttons = [
        ChannelButton(label="✅ Ок", callback_id="resolve:1:abc"),
        ChannelButton(label="Написать", url="tg://user?id=5"),
    ]
    res = await sender.send("42", "Текст", buttons)
    assert res.ok
    markup = bot.sent[0]["reply_markup"]
    b1, b2 = markup.inline_keyboard[0][0], markup.inline_keyboard[1][0]
    assert b1.callback_data == "resolve:1:abc" and b1.url is None
    assert b2.url == "tg://user?id=5" and b2.callback_data is None


# ── Registry ─────────────────────────────────────────────────────────

def test_registry(db):
    s = FakeSender()
    register_sender(s)
    assert get_sender("whatsapp") is s
    assert get_sender("telegram") is None
    assert router.registered_channels() == ["whatsapp"]
