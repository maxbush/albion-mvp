"""Round 7 — total audit fixes (MASTER_PLAN v4).

R7-1: непокрытые интенты (question/other) → честный ответ пользователю +
алерт координаторам с кнопкой ответа. Больше никакого «Обрабатываю...» в пустоту.
"""

import pytest


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


class FakeChat:
    def __init__(self, id=1):
        self.id = id


class FakeUpdate:
    def __init__(self, user, chat_id=1):
        self.effective_user = user
        self.effective_chat = FakeChat(chat_id)
        self.message = FakeMessage()
        self.callback_query = None


class FakeContext:
    def __init__(self, args=None, bot=None):
        self.args = args or []
        self.bot = bot


async def _init_tmp_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    return "albion.db"


def _flat_buttons(buttons):
    return buttons or []


# ── R7-1: fallback для question/other ────────────────────────────────

@pytest.mark.asyncio
async def test_r7_1_question_answers_user_and_alerts_coordinators(tmp_path, monkeypatch):
    """«а когда урок?» → пользователь получает ack, координаторы — текст + кнопку ответа."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import UserRepository
    from src.events.bus import bus
    from src.events.types import Event, EventTypes
    from src.workflows.fallback import FallbackWorkflow

    await UserRepository(db).create("555", "parent", "Анна")
    await UserRepository(db).create("coord_1", "coordinator", "Координатор")

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await FallbackWorkflow(db).handle_classified(Event(EventTypes.MESSAGE_CLASSIFIED, {
            "intent": "question", "telegram_id": "555", "text": "а когда урок у Сони?",
        }))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    user_msgs = [d for d in captured if d.get("telegram_id") == "555"]
    assert user_msgs, "пользователь должен получить честный ack"
    assert "координатору" in user_msgs[0]["message"]

    coord_msgs = [d for d in captured if d.get("telegram_id") == "coord_1"]
    assert coord_msgs, "координаторы должны получить текст обращения"
    alert = coord_msgs[0]
    assert "а когда урок у Сони?" in alert["message"]
    assert "Анна" in alert["message"] and "parent" in alert["message"]
    assert "TG 555" not in alert["message"]             # П9: без сырого TG в тексте
    btns = _flat_buttons(alert.get("buttons"))
    assert any(b.get("url") == "tg://user?id=555" for b in btns)


@pytest.mark.asyncio
async def test_r7_1_other_intent_tutor_gets_en_ack(tmp_path, monkeypatch):
    """Тьютор (en) → ack на английском; covered-интенты fallback не трогает."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import UserRepository
    from src.events.bus import bus
    from src.events.types import Event, EventTypes
    from src.workflows.fallback import FallbackWorkflow

    await UserRepository(db).create("42", "tutor", "Daniel", language="en")
    await UserRepository(db).create("coord_1", "coordinator", "Координатор")

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    wf = FallbackWorkflow(db)
    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await wf.handle_classified(Event(EventTypes.MESSAGE_CLASSIFIED, {
            "intent": "other", "telegram_id": "42", "text": "just checking in",
        }))
        # чужие интенты — игнор (их обрабатывают свои workflow)
        await wf.handle_classified(Event(EventTypes.MESSAGE_CLASSIFIED, {
            "intent": "cancellation", "telegram_id": "42", "text": "cancel",
        }))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    user_msgs = [d for d in captured if d.get("telegram_id") == "42"]
    assert len(user_msgs) == 1                                    # ровно один ack (other)
    assert "coordinator" in user_msgs[0]["message"].lower()       # EN-версия
    assert "Передал" not in user_msgs[0]["message"]


@pytest.mark.asyncio
async def test_r7_1_free_text_full_chain_via_bus(tmp_path, monkeypatch):
    """E2E-цепочка: free-text → classifier → fallback → ответ (без «Обрабатываю...»)."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import handle_message
    from src.db.repository import UserRepository
    from src.events.bus import bus
    from src.events.types import EventTypes
    from src.workflows.fallback import FallbackWorkflow

    await UserRepository(db).create("777", "parent", "Анна")

    # Mock-классификатор неизвестный текст всегда метит "lead" — для
    # детерминированной проверки fallback-ветки классификацию фиксируем.
    from src.ai.client import llm_client

    async def fake_classify(text):
        return {"intent": "question", "confidence": 0.9}

    monkeypatch.setattr(llm_client, "classify_intent", fake_classify)

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    # Цепочка как в main.register_all: classifier → fallback
    from src.ai.classifier import handle_message_incoming
    wf = FallbackWorkflow(db)
    bus.subscribe(EventTypes.MESSAGE_INCOMING, handle_message_incoming)
    bus.subscribe(EventTypes.MESSAGE_CLASSIFIED, wf.handle_classified)
    try:
        upd = FakeUpdate(FakeUser(777))
        upd.message.text = "обычный вопрос без ключевых слов"
        await handle_message(upd, FakeContext())
    finally:
        bus.unsubscribe(EventTypes.MESSAGE_INCOMING, handle_message_incoming)
        bus.unsubscribe(EventTypes.MESSAGE_CLASSIFIED, wf.handle_classified)
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    # «Обрабатываю...» больше нет
    assert not any("Обрабатываю" in (t or "") for t, _ in upd.message.replies)
    # пользователь получил видимый ответ через шину (fallback по question)
    assert any("координатору" in d.get("message", "") for d in captured
               if d.get("telegram_id") == "777")


# ── R7-2: ack отправителю на lead / absence_report ───────────────────

@pytest.mark.asyncio
async def test_r7_2_lead_sender_gets_ack(tmp_path, monkeypatch):
    """Заявка: координаторы уведомлены (как раньше) + отправитель получает ack."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import LeadRepository, UserRepository
    from src.events.bus import bus
    from src.events.types import Event, EventTypes
    from src.workflows.lead_capture import LeadCaptureWorkflow

    await UserRepository(db).create("911", "parent", "Новый клиент")
    await UserRepository(db).create("coord_1", "coordinator", "Координатор")

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await LeadCaptureWorkflow(db).handle_lead_new(Event(EventTypes.LEAD_NEW, {
            "raw_message": "Нужен репетитор по математике для сына",
            "telegram_id": "911",
            "extracted_data": {"subject": "math", "is_lead": True},
        }))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    sender = [d for d in captured if d.get("telegram_id") == "911"]
    coord = [d for d in captured if d.get("telegram_id") == "coord_1"]
    assert sender, "отправитель заявки должен получить подтверждение"
    assert "Заявка принята" in sender[0]["message"]
    assert coord and "Новая заявка" in coord[0]["message"]  # прежний алерт на месте
    assert await LeadRepository(db).get(1)                  # заявка сохранена


@pytest.mark.asyncio
async def test_r7_2_absence_report_sender_gets_ack(tmp_path, monkeypatch):
    """Репорт о неявке текстом: координаторы получают алерт + отправителю ack."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import UserRepository
    from src.events.bus import bus
    from src.events.types import Event, EventTypes
    from src.workflows.absence import AbsenceWorkflow

    await UserRepository(db).create("912", "parent", "Мама")
    await UserRepository(db).create("coord_1", "coordinator", "Координатор")

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await AbsenceWorkflow(db).handle_classified(Event(EventTypes.MESSAGE_CLASSIFIED, {
            "intent": "absence_report", "telegram_id": "912", "text": "сын сегодня не придёт",
        }))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    sender = [d for d in captured if d.get("telegram_id") == "912"]
    coord = [d for d in captured if d.get("telegram_id") == "coord_1"]
    assert sender and "передали координатору" in sender[0]["message"]
    assert coord and "неявке" in coord[0]["message"]


# ── R7-3: /incidents человеком ───────────────────────────────────────

@pytest.mark.asyncio
async def test_r7_3_incidents_human_readable(tmp_path, monkeypatch):
    """Статус/тип словом, ученик по имени, время в org-зоне; счётчики прежние."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.config import settings
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.db.repository import IncidentRepository, UserRepository, WorkflowRepository
    from src.db.repository import MeritHubClassRepository

    await UserRepository(db).create("100", "coordinator", "Admin")
    irepo = IncidentRepository(db)
    inc_a = await irepo.create(lesson_ref="C9", type="absence", status="escalated",
                               resolution="no response")
    inc_r = await irepo.create(lesson_ref=None, type="absence", status="resolved",
                               resolution="parent_late")
    await MeritHubClassRepository(db).upsert(
        "C9", title="Math", start_time="2099-07-31T15:00:00+00:00")
    wrepo = WorkflowRepository(db)
    await wrepo.create("absence_notification", "running",
                       {"incident_id": inc_a, "student_name": "Sofia"})
    await wrepo.create("absence_notification", "completed",
                       {"incident_id": inc_r, "student_name": "Max"})

    from src.bot.pilot import cmd_incidents
    upd = FakeUpdate(FakeUser(100))
    await cmd_incidents(upd, FakeContext([]))

    text = upd.message.replies[-1][0]
    # счётчики — как раньше (регрессионная защита)
    assert "Ожидают: 0" in text and "Эскалации: 1" in text and "Закрыто: 1" in text
    # живой вид активного
    assert "неявка" in text and "Sofia" in text
    assert "ЭСКАЛАЦИЯ" in text
    assert "C9" in text and "31.07, 15:00" in text          # человекочитаемый label
    # ни сырых статусов, ни обрезанных ISO-после́довательностей
    assert "статус:" not in text and "[absence]" not in text
    # закрытый: резолюция словом, имя ученика
    assert "Max" in text and "опоздали" in text
    # время — с подписью зоны, а не голый UTC-naive обрубок
    assert "(London)" in text
