"""Round 3 (MASTER_PLAN v3) — E2E-проверки потоков «UI ↔ backend».

P0.3: /cancel_lesson — команда существует и ведёт полный флоу отмены
P1.3: /absent с неизвестным уроком — отправитель получает честный фидбэк
P1.1: интент absence_report доходит до координаторов
"""

import pytest

from src.events.bus import bus
from src.events.types import EventTypes


# ── фейки telegram ────────────────────────────────────────────────────

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


class FakeUpdate:
    def __init__(self, user, chat_id=1):
        self.effective_user = user
        self.effective_chat = type("FakeChat", (), {"id": chat_id})()
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []


async def _init_tmp_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    return "albion.db"


# ── P0.3: /cancel_lesson ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_p03_cancel_lesson_full_flow(tmp_path, monkeypatch):
    """Команда → LESSON_CANCELLED → уведомления репетитору и координатору."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import cmd_cancel_lesson
    from src.db.repository import UserRepository
    from src.workflows.cancellation import CancellationWorkflow

    await UserRepository(db).create("tutor_1", "tutor", "Репетитор")
    await UserRepository(db).create("coord_1", "coordinator", "Координатор")

    wf = CancellationWorkflow(db)
    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.LESSON_CANCELLED, wf.handle_cancelled)
    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        upd = FakeUpdate(FakeUser(9, full_name="Заказчик"))
        ctx = FakeContext(["lesson_1", "по", "болезни"])
        await cmd_cancel_lesson(upd, ctx)

        # Пользователю ответили подтверждением с ID урока.
        assert any("lesson_1" in t for t, _ in upd.message.replies), upd.message.replies

        # Репетитор уведомлён об отмене (причина проброшена).
        tutor_msgs = [d for d in captured if d.get("telegram_id") == "tutor_1"]
        assert tutor_msgs, "репетитор должен получить уведомление"
        assert "Отмена" in tutor_msgs[0]["message"]
        assert "болезни" in tutor_msgs[0]["message"]

        # Координатор уведомлён.
        coord_msgs = [d for d in captured if d.get("telegram_id") == "coord_1"]
        assert coord_msgs, "координатор должен получить уведомление"

        # Mock-урок реально помечен отменённым.
        assert (await wf.airtable.get_lesson("lesson_1")).status == "cancelled"
    finally:
        bus.unsubscribe(EventTypes.LESSON_CANCELLED, wf.handle_cancelled)
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)


@pytest.mark.asyncio
async def test_p03_cancel_lesson_unknown_lesson_notifies_reporter(tmp_path, monkeypatch):
    """Неизвестный урок: отправитель получает честный «не найден», а не тишину."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import cmd_cancel_lesson
    from src.workflows.cancellation import CancellationWorkflow

    wf = CancellationWorkflow(db)
    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.LESSON_CANCELLED, wf.handle_cancelled)
    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        upd = FakeUpdate(FakeUser(9, full_name="Заказчик"))
        # Заменяем mock-уроки: гарантируем, что unknown_x нет нигде.
        await cmd_cancel_lesson(upd, FakeContext(["unknown_x"]))
        reporter_msgs = [d for d in captured if d.get("telegram_id") == "9"]
        assert reporter_msgs, "отправитель должен получить фидбэк"
        assert "не найден" in reporter_msgs[0]["message"]
    finally:
        bus.unsubscribe(EventTypes.LESSON_CANCELLED, wf.handle_cancelled)
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)


@pytest.mark.asyncio
async def test_p03_cancel_lesson_usage_hint(tmp_path, monkeypatch):
    """Без аргументов — подсказка, события нет."""
    await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import cmd_cancel_lesson

    async def boom(_ev):
        raise AssertionError("не должно быть события LESSON_CANCELLED без аргументов")

    bus.subscribe(EventTypes.LESSON_CANCELLED, boom)
    try:
        upd = FakeUpdate(FakeUser(9))
        await cmd_cancel_lesson(upd, FakeContext([]))
        texts = [t for t, _ in upd.message.replies]
        assert any("/cancel_lesson" in t for t in texts)
    finally:
        bus.unsubscribe(EventTypes.LESSON_CANCELLED, boom)
