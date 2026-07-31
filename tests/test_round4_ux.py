"""Round 4 — UX-работы U1–U7 (только существующие механики).

U1: меню команд «/» по роли (set_my_commands + BotCommandScopeChat).
"""

import pytest

from src.config import settings


class FakeUser:
    def __init__(self, id, username=None, full_name="T"):
        self.id = id
        self.username = username
        self.full_name = full_name


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))


class FakeChatRich:
    def __init__(self, id=1):
        self.id = id
        self.sent = []

    async def send_message(self, text, **kw):
        self.sent.append((text, kw))


class FakeQuery:
    def __init__(self, data, user):
        self.data = data
        self.from_user = user
        self.edits = []
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))

    async def edit_message_reply_markup(self, markup):
        pass


class FakeBot:
    """Ловит вызовы bot-API, которые нам важны в UX-тестах."""
    def __init__(self):
        self.menus = []          # (chat_id, [(cmd, desc), ...])
        self.sent = []

    async def set_my_commands(self, commands, scope=None):
        chat_id = getattr(scope, "chat_id", None)
        self.menus.append((chat_id, [(c.command, c.description) for c in commands]))

    async def send_message(self, chat_id=None, text=None, reply_markup=None, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kw):
        self.sent.append({"chat_id": chat_id, "message_id": message_id, "text": text})


class FakeUpdate:
    def __init__(self, user, chat_id=1):
        self.effective_user = user
        self.effective_chat = FakeChatRich(chat_id)
        self.message = FakeMessage()
        self.callback_query = None


class FakeContext:
    def __init__(self, args=None, bot=None):
        self.args = args or []
        self.bot = bot or FakeBot()


async def _init_tmp_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    return "albion.db"


# ── U1: меню команд по роли ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_u1_menu_applied_on_registration(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback

    upd = FakeUpdate(FakeUser(501))
    upd.callback_query = FakeQuery("register_coordinator", upd.effective_user)
    ctx = FakeContext(bot=FakeBot())
    await handle_callback(upd, ctx)

    assert ctx.bot.menus, "меню должно быть выставлено при регистрации"
    chat_id, items = ctx.bot.menus[-1]
    assert chat_id == 501
    names = {c for c, _ in items}
    # Координаторское меню содержит операционные команды.
    assert {"today", "incidents", "morning", "ok"} <= names


@pytest.mark.asyncio
async def test_u1_menu_parent_is_minimal(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback

    upd = FakeUpdate(FakeUser(502))
    upd.callback_query = FakeQuery("register_parent", upd.effective_user)
    ctx = FakeContext(bot=FakeBot())
    await handle_callback(upd, ctx)

    chat_id, items = ctx.bot.menus[-1]
    assert chat_id == 502
    names = {c for c, _ in items}
    # Родителю — минимум (Hick's Law), без координаторских команд.
    assert "today" not in names and "incidents" not in names
    assert {"start", "status", "cancel_lesson", "whoami"} == names


@pytest.mark.asyncio
async def test_u1_role_command_updates_target_menu(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "900")
    from src.bot.roles import cmd_role

    upd = FakeUpdate(FakeUser(900))
    ctx = FakeContext(["601", "tutor"], bot=FakeBot())
    await cmd_role(upd, ctx)

    chat_id, items = ctx.bot.menus[-1]
    assert chat_id == 601  # меню выставлено ЦЕЛЕВОМУ пользователю
    names = {c for c, _ in items}
    assert "cancel_lesson" in names and "today" not in names


@pytest.mark.asyncio
async def test_u1_menu_survives_bot_api_failure(tmp_path, monkeypatch):
    """Ошибка Telegram API при set_my_commands не должна ломать регистрацию."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_callback

    class BadBot(FakeBot):
        async def set_my_commands(self, commands, scope=None):
            raise RuntimeError("telegram down")

    upd = FakeUpdate(FakeUser(503))
    upd.callback_query = FakeQuery("register_parent", upd.effective_user)
    ctx = FakeContext(bot=BadBot())
    await handle_callback(upd, ctx)  # не должно упасть
    # Пользователь всё равно зарегистрирован.
    from src.db.repository import UserRepository
    assert (await UserRepository(db).get_by_telegram_id("503"))["role"] == "parent"
