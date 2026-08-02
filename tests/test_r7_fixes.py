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


# ── R7-4: хвосты эскалации (П9) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_r7_4_escalation_no_raw_tg_no_utc_tail(tmp_path, monkeypatch):
    """Эскалация: нет «Parent TG:» и «UTC» текста; есть tg://-кнопка и время London."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import IncidentRepository, UserRepository, WorkflowRepository
    from src.events.bus import bus
    from src.events.types import EventTypes
    from src.workflows.absence import AbsenceWorkflow

    await UserRepository(db).create("555", "parent", "Анна")
    await UserRepository(db).create("coord_1", "coordinator", "Координатор")
    irepo = IncidentRepository(db)
    inc_id = await irepo.create(lesson_ref="C9", type="absence", status="pending")
    wid = await WorkflowRepository(db).create("absence_notification", "running", {
        "incident_id": inc_id, "student_name": "Sofia", "parent_telegram_id": "555",
    })

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await AbsenceWorkflow(db)._escalate(wid, inc_id, reason="no response")
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    coord = [d for d in captured if d.get("telegram_id") == "coord_1"]
    assert coord, "эскалация должна дойти до координатора"
    text = coord[0]["message"]
    assert "Эскалация" in text and "Sofia" in text
    assert "Parent TG" not in text and "555" not in text   # сырого TG нет
    assert "UTC" not in text                                # никакого теххвоста
    assert "(London)" in text and "Создан:" in text         # время в org-зоне
    btns = coord[0].get("buttons") or []
    assert any(b.get("url") == "tg://user?id=555" for b in btns)  # действие — кнопкой


@pytest.mark.asyncio
async def test_r7_4_parent_reply_notification_has_button_not_raw_tg(tmp_path, monkeypatch):
    """notify_coordinators_parent_reply: «Parent TG: 555» → url-кнопка."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import IncidentRepository, UserRepository, WorkflowRepository
    from src.events.bus import bus
    from src.events.types import EventTypes
    from src.workflows.absence import AbsenceWorkflow

    await UserRepository(db).create("coord_1", "coordinator", "Координатор")
    irepo = IncidentRepository(db)
    inc_id = await irepo.create(lesson_ref="C9", type="absence", status="pending")
    await WorkflowRepository(db).create("absence_notification", "running", {
        "incident_id": inc_id, "student_name": "Sofia", "parent_telegram_id": "555",
    })

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await AbsenceWorkflow(db).notify_coordinators_parent_reply(
            inc_id, "late", parent_text="опоздаем", parent_telegram_id="555")
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    coord = [d for d in captured if d.get("telegram_id") == "coord_1"][0]
    assert "Parent TG" not in coord["message"]
    assert "опоздаем" in coord["message"]
    btns = coord.get("buttons") or []
    assert any(b.get("url") == "tg://user?id=555" for b in btns)


# ── R7-5: no-reply алерт без «Actor:/Telegram:» ──────────────────────

@pytest.mark.asyncio
async def test_r7_5_no_reply_alert_human_with_contact_button(tmp_path, monkeypatch):
    """«Репетитор не подтвердил готовность»: контекст словами + кнопка связи."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import UserRepository, WorkflowRepository
    from src.events.bus import bus
    from src.events.types import Event, EventTypes
    from src.workflows.lesson_ops import LessonOpsWorkflow

    await UserRepository(db).create("coord_1", "coordinator", "Координатор")
    wid = await WorkflowRepository(db).create("prelesson_tutor", "running", {
        "class_id": "C9", "actor_type": "tutor", "actor_telegram_id": "42",
        "tutor_name": "Daniel John", "student_names": ["Sofia"],
        "start_time": "2099-07-31T15:00:00+00:00"})

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await LessonOpsWorkflow(db).handle_scheduler_tick(Event(EventTypes.SCHEDULER_TICK, {
            "action": "tutor_prelesson_no_reply",
            "workflow_id": wid,
            "data": {"workflow_id": wid},
        }))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    coord = [d for d in captured if d.get("telegram_id") == "coord_1"]
    assert coord, "координатор должен получить алерт о молчании"
    text = coord[0]["message"]
    assert "не подтвердил готовность" in text
    assert "Daniel John" in text and "Sofia" in text
    assert "Actor:" not in text and "Telegram:" not in text   # теххвостов нет
    btns = coord[0].get("buttons") or []
    assert any(b.get("url") == "tg://user?id=42" for b in btns)
    assert any("репетитору" in (b.get("text") or "") for b in btns)


# ── R7-6: /lessons — ветка координатора + пункт меню ─────────────────

@pytest.mark.asyncio
async def test_r7_6_coordinator_menu_includes_lessons():
    """UI/Backend синк: help обещает /lessons — меню «/» обязано его иметь."""
    from src.bot.roles import ROLE_COMMAND_MENUS
    coord_cmds = {c for c, _ in ROLE_COMMAND_MENUS["coordinator"]}
    assert "lessons" in coord_cmds
    parent_cmds = {c for c, _ in ROLE_COMMAND_MENUS["parent"]}
    tutor_cmds = {c for c, _ in ROLE_COMMAND_MENUS["tutor"]}
    assert "lessons" in parent_cmds and "lessons" in tutor_cmds


@pytest.mark.asyncio
async def test_r7_6_coordinator_lessons_org_wide_view(tmp_path, monkeypatch):
    """Координатор видит занятия ВСЕЙ организации со ссылками (не «вас не добавили»)."""
    import json
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import cmd_lessons
    from src.db.repository import (
        MeritHubClassRepository, MeritHubEnrollmentRepository, UserRepository,
    )
    from src.utils.recurrence import mh_weekday, org_now
    from datetime import timedelta

    await UserRepository(db).create("100", "coordinator", "Босс")
    tomorrow = org_now().date() + timedelta(days=1)
    crepo = MeritHubClassRepository(db)
    await crepo.upsert("C60", title="Sofia — Physics", class_type="perma",
                       schedule_days=json.dumps([mh_weekday(tomorrow)]),
                       start_time=f"{tomorrow.isoformat()}T15:00:00+00:00",
                       participant_link="pl60")
    await crepo.upsert("C61", title="Max — Maths", class_type="perma",
                       schedule_days=json.dumps([mh_weekday(tomorrow)]),
                       start_time=f"{tomorrow.isoformat()}T17:00:00+00:00",
                       participant_link="pl61")
    await MeritHubEnrollmentRepository(db).add(
        "C60", "mh_s01", client_user_id="s01", student_name="Sofia")

    upd = FakeUpdate(FakeUser(100))
    await cmd_lessons(upd, FakeContext([]))

    text, kw = upd.message.replies[-1]
    assert "организации" in text
    assert "Sofia — Physics" in text and "Max — Maths" in text
    assert "не вижу" not in text                      # нет ложного empty-state
    btns = [b for row in (kw["reply_markup"].inline_keyboard or []) for b in row]
    assert any("pl60" in (b.url or "") for b in btns)
    # пустая база → честное пустое состояние с вектором на /schedule
    await MeritHubClassRepository(db)._execute("DELETE FROM merithub_classes", ())
    upd2 = FakeUpdate(FakeUser(100))
    await cmd_lessons(upd2, FakeContext([]))
    assert "/schedule" in upd2.message.replies[-1][0]


# ── R7-7: help-карточка покрывает команды владельца ──────────────────

def test_r7_7_help_card_covers_owner_commands():
    """R9-4 (правка): help админа покрывает команды владельца; help координатора —
    только его команды (раньше не-админ видел /kill_switch, /roles, /mh_* и получал
    «⛔ Только владелец/админ» — UI/Backend миссматч)."""
    from src.bot.handlers import _coordinator_help_text
    text_admin = _coordinator_help_text(admin=True)
    text = _coordinator_help_text(admin=False)
    # команды аварийного дня и обзора — находимы только у админа (R7-7 + R9-4)
    assert "/kill\\_switch" in text_admin and "/roles" in text_admin
    assert "/kill\\_switch" not in text and "/roles" not in text
    assert "/mh\\_schedule" not in text
    # ежедневные визарды — в первой секции (Hick: частое — наверху)
    assert text.index("/schedule") < text.index("/ok")
    for cmd in ("/add\\_student", "/add\\_tutor", "/today", "/morning",
                "/incidents", "/lessons", "/status", "/ok"):
        assert cmd in text, cmd


# ── R7-8: /mh_schedule — ссылка тьютору на его языке ─────────────────

class _StubMeritHub:
    """Та же заглушка, что в test_pilot (не переизобретаем конвенцию)."""

    def __init__(self):
        self.last_add = None

    async def schedule_class(self, instructor, **kw):
        return {"classId": "C9", "hostLink": "HL",
                "commonLinks": {"commonHostLink": "HL", "commonParticipantLink": "PL"}}

    async def add_users_to_class(self, class_id, users):
        self.last_add = (class_id, users)
        return {"users": [{"userId": u["userId"], "userLink": "u_" + u["userId"]} for u in users]}

    from src.integrations.merithub_client import MeritHubClient as _MC
    parse_schedule = staticmethod(_MC.parse_schedule)
    parse_user_links = staticmethod(_MC.parse_user_links)

    def room_url(self, link, device_test=False):
        return f"ROOM/{link}"


@pytest.mark.asyncio
async def test_r7_8_mh_schedule_tutor_link_localized(tmp_path, monkeypatch):
    """Тьютор с language=en получает EN-ссылку (как из визарда), а не RU хардкод."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.config import settings
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    monkeypatch.setattr("src.integrations.factory.get_merithub_service", lambda: _StubMeritHub())
    from src.db.repository import (
        MeritHubContactRepository, MeritHubStudentRepository, UserRepository,
    )
    from src.events.bus import bus
    from src.events.types import EventTypes

    await UserRepository(db).create("100", "coordinator", "Админ")
    await UserRepository(db).create("555", "tutor", "Anna", language="en")
    srepo = MeritHubStudentRepository(db)
    await srepo.upsert("t1", merithub_user_id="mh_t1", name="Anna", role="tutor")
    await MeritHubContactRepository(db).upsert("t1", "555", "tutor", name="Anna")
    await srepo.upsert("s1", merithub_user_id="mh_s1", name="Миша",
                       parent_telegram_id="777", role="student")

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        from src.bot.pilot import cmd_mh_schedule
        upd = FakeUpdate(FakeUser(100))
        await cmd_mh_schedule(upd, FakeContext(["t1", "2099-07-20T15:00:00+01:00", "60", "s1"]))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    tutor_msgs = [d for d in captured if d.get("telegram_id") == "555"]
    assert tutor_msgs, "тьютор должен получить ссылку"
    msg = tutor_msgs[0]["message"]
    assert "Lesson link" in msg and "Students" in msg      # EN (язык тьютора)
    assert "Ссылка на урок" not in msg                      # не RU хардкод
    assert "ROOM/u_mh_t1" in msg


# ── R7-9: EN-слова в эвристиках tutor-reply + mock сканирует текст, не промпт ──

@pytest.mark.asyncio
async def test_r7_9_english_tutor_replies_understood():
    """EN-формулировки тьютора распознаются (и эвристика, и mock-ветка)."""
    from src.ai.client import LLMClient, llm_client

    cases = {
        "I'm ready, all set": "ready",
        "Confirmed, on track": "ready",
        "Sorry, running late, stuck in traffic": "late",
        "I can't make it today": "no_show",
        "Cannot conduct the lesson, sick": "no_show",
        "WiFi connection issues": "tech",
        "My laptop camera is broken": "tech",
    }
    for text, expected in cases.items():
        # эвристика (fallback-путь)
        assert LLMClient()._heuristic_tutor_reply(text)["status"] == expected, text
        # mock-ветка chat_cheap (как в демо без API-ключа)
        got = await llm_client.interpret_tutor_reply(text)
        assert got["status"] == expected, f"{text} -> {got}"


@pytest.mark.asyncio
async def test_r7_9_mock_scans_user_text_not_prompt_template():
    """Регрессия латентного бага: шаблон промпта не должен определять статус."""
    from src.ai.client import llm_client

    # В шаблоне есть «late»/«ready»/«cannot» — если сканировать весь промпт,
    # любые слова схлопнутся в no_show/late. Русский «всё ок» бывало «late».
    got = await llm_client.interpret_parent_reply("всё в порядке, спасибо")
    assert got["status"] == "ok", got
    got = await llm_client.interpret_parent_reply("сын не придёт сегодня")
    assert got["status"] == "no_show", got


# ── R7-13: webhook classStatus публикует продуктовые события ─────────

@pytest.mark.asyncio
async def test_r7_13_classstatus_publishes_lesson_events(tmp_path, monkeypatch):
    """lv → LESSON_STARTED, cp → LESSON_COMPLETED; статус сохраняется как раньше."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.api.webhook import _dispatch_class_status
    from src.db.repository import MeritHubClassStatusRepository
    from src.events.bus import bus
    from src.events.types import EventTypes

    started, completed = [], []

    async def cap_started(ev): started.append(ev.data)
    async def cap_completed(ev): completed.append(ev.data)

    bus.subscribe(EventTypes.LESSON_STARTED, cap_started)
    bus.subscribe(EventTypes.LESSON_COMPLETED, cap_completed)
    try:
        await _dispatch_class_status({"classId": "C9", "subClassId": "SC1",
                                      "status": "lv", "startTime": "2099-07-31T15:00:00Z"})
        await _dispatch_class_status({"classId": "C9", "subClassId": "SC1", "status": "cp"})
        await _dispatch_class_status({"classId": "C10", "status": "up"})   # другой класс, не публикуется
    finally:
        bus.unsubscribe(EventTypes.LESSON_STARTED, cap_started)
        bus.unsubscribe(EventTypes.LESSON_COMPLETED, cap_completed)

    assert len(started) == 1 and started[0]["class_id"] == "C9"
    assert started[0]["sub_class_id"] == "SC1"
    assert len(completed) == 1
    row = await MeritHubClassStatusRepository(db).get("C9")
    assert row["last_status"] == "cp"                              # прежняя запись статуса


def test_r7_13_phantom_event_types_removed():
    """Фантомные типы (0 publish за всю историю) удалены из EventTypes."""
    from src.events.types import EventTypes
    assert not hasattr(EventTypes, "PAYMENT_RECEIVED")
    assert not hasattr(EventTypes, "PAYMENT_LOW_BALANCE")
    assert not hasattr(EventTypes, "LESSON_RESCHEDULED")


# ── R7-14: complete_workflow — мёртвый SELECT убран ──────────────────

@pytest.mark.asyncio
async def test_r7_14_complete_workflow_contract(tmp_path, monkeypatch):
    """Контракт после удаления мёртвого repo.get(): state=completed + событие на шине."""
    await _init_tmp_db(tmp_path, monkeypatch)
    from src.events.bus import bus
    from src.events.types import EventTypes
    from src.workflows.engine import engine

    done = []

    async def cap(ev): done.append(ev.data)

    bus.subscribe(EventTypes.WORKFLOW_COMPLETED, cap)
    try:
        wid = await engine.start_workflow("zzz_test", {"x": 1})
        await engine.complete_workflow(wid, {"ok": True})
    finally:
        bus.unsubscribe(EventTypes.WORKFLOW_COMPLETED, cap)

    assert done and done[0]["workflow_id"] == wid and done[0]["ok"] is True
    wf = await engine.repo.get(wid)
    assert wf["state"] == "completed"


# ── R7-15: батч-запросы ≡ одиночные (анти-N+1) ───────────────────────

@pytest.mark.asyncio
async def test_r7_15_batch_queries_match_singles(tmp_path, monkeypatch):
    """get_many/list_by_classes/get_map/get_by_client_ids дают идентичный
    результат куче одиночных запросов — иначе оптимизация меняет поведение."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import (
        MeritHubClassRepository,
        MeritHubClassStatusRepository,
        MeritHubEnrollmentRepository,
        MeritHubStudentRepository,
    )

    crepo, erepo = MeritHubClassRepository(db), MeritHubEnrollmentRepository(db)
    srepo, strepo = MeritHubStudentRepository(db), MeritHubClassStatusRepository(db)

    # Два класса, зачисления и студент-строки (минимум для селектов).
    await crepo._execute(
        "INSERT INTO merithub_classes (class_id, title, start_time, class_type) VALUES ('B1','B1','2099-08-03T15:00:00','oneTime')")
    await crepo._execute(
        "INSERT INTO merithub_classes (class_id, title, start_time, class_type) VALUES ('B2','B2','2099-08-04T16:00:00','oneTime')")
    await erepo.add("B1", "mh1", client_user_id="cu1", student_name="Ann")
    await erepo.add("B2", "mh2", client_user_id="cu2", student_name="Bob")
    await srepo.upsert("cu1", name="Ann S")
    await srepo.upsert("cu2", name="Bob T")
    await strepo.upsert("B1", "lv")

    many = await crepo.get_many(["B1", "B2"])
    assert set(many) == {"B1", "B2"}
    assert many["B1"]["title"] == (await crepo.get("B1"))["title"]

    by_cls = await erepo.list_by_classes(["B1", "B2"])
    assert [e["student_name"] for e in by_cls["B1"]] == ["Ann"]
    assert [e["student_name"] for e in by_cls["B2"]] == ["Bob"]

    st = await strepo.get_map(["B1", "B2"])
    assert st["B1"]["last_status"] == "lv" and "B2" not in st

    sm = await srepo.get_by_client_ids(["cu1", "cu2"])
    assert sm["cu1"]["name"] == "Ann S" and sm["cu2"]["name"] == "Bob T"

    # Пустые/дублирующиеся входы не падают.
    assert await crepo.get_many([]) == {}
    assert set(await crepo.get_many(["B1", "B1", None])) == {"B1"}


# ── R7-16: idempotent notify_parent (requeue ≠ дубль) ────────────────

@pytest.mark.asyncio
async def test_r7_16_notify_parent_idempotent_on_requeue(tmp_path, monkeypatch):
    """Повторное выполнение того же scheduled action (requeue после падения
    воркера/гонки) НЕ дублирует сообщение родителю и НЕ планирует вторую
    эскалацию. Флаг notify_parent_sent в данных workflow."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import (
        IncidentRepository, ScheduledActionRepository, UserRepository,
        WorkflowRepository,
    )
    from src.events.bus import bus
    from src.events.types import Event, EventTypes
    from src.workflows.absence import AbsenceWorkflow
    from src.workflows.engine import engine

    engine.repo = WorkflowRepository(db)
    engine.scheduler = ScheduledActionRepository(db)
    # Родитель mock-ученика student_1 (Airtable mock: parent_telegram_id=parent_1).
    await UserRepository(db).create("parent_1", "parent", "Родитель Миши")

    wf = AbsenceWorkflow(db)
    captured = []

    async def cap(ev): captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await wf.handle_lesson_absent(Event(EventTypes.LESSON_ABSENT, {
            "lesson_id": "lesson_1", "reported_by": "111111"}))
        inc = (await IncidentRepository(db)._fetchall("SELECT * FROM incidents"))[0]
        wrow = (await WorkflowRepository(db)._fetchall(
            "SELECT * FROM workflow_instances WHERE workflow_type='absence_notification'"))[0]
        wid = wrow["id"]

        tick = lambda: wf.handle_scheduler_tick(Event("scheduler.tick", {
            "action": "notify_parent", "workflow_id": wid,
            "data": {"incident_id": inc["id"]}}))
        await tick()          # первое выполнение — отправляем
        await tick()          # requeue-дубль — должен молча выйти
        await tick()          # и ещё раз для надёжности
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    parent_msgs = [d for d in captured
                   if d.get("workflow_id") == wid and d.get("incident_id") == inc["id"]]
    assert len(parent_msgs) == 1, f"родителю ушло {len(parent_msgs)} сообщений вместо 1"

    escalates = await ScheduledActionRepository(db)._fetchall(
        "SELECT * FROM scheduled_actions WHERE workflow_id=? AND action='escalate'", (wid,))
    assert len(escalates) == 1, f"эскалаций запланировано {len(escalates)} вместо 1"

    import json as _json
    data = _json.loads((await WorkflowRepository(db).get(wid))["data"])
    assert data.get("notify_parent_sent") is True


# ── R7-17: .env.example покрывает ВСЕ поля Settings ──────────────────

def test_r7_17_env_example_covers_all_settings():
    """Документация = запускаемость: каждое поле Settings должно иметь
    ключ в .env.example, иначе оператор не узнает о ручке настройки."""
    from pathlib import Path
    from src.config import Settings
    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(
        encoding="utf-8")
    missing = [name.upper() for name in Settings.model_fields
               if f"\n{name.upper()}=" not in f"\n{example}"]
    assert not missing, f"в .env.example нет ключей: {missing}"


# ── R7-18: /leads — поверхность просмотра лидов ──────────────────────

@pytest.mark.asyncio
async def test_r7_18_cmd_leads_lists_recent_with_status_and_counter(tmp_path, monkeypatch):
    """Admin: последние 10 со статусом словом + счётчик; не-admin — отказ."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.config import settings
    from src.db.repository import LeadRepository

    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    leads = LeadRepository(db)
    for i in range(12):
        await leads.create(f"Хочу пробное занятие #{i + 1}",
                           {"name": f"Ученик {i + 1}"}, source="telegram")
    await leads._execute("UPDATE leads SET status='contacted' WHERE id=12")

    from src.bot.pilot import cmd_leads

    # не-admin
    upd = FakeUpdate(FakeUser(777, "stranger"))
    await cmd_leads(upd, FakeContext([]))
    assert any("⛔" in t for t, _ in upd.message.replies)

    # admin
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_leads(upd, FakeContext([]))
    text = upd.message.replies[0][0]
    assert "Лиды" in text and "Всего: 12" in text
    assert "последние 10" in text                      # лимит отражён в выдаче
    assert "#12 " in text and "#3 " in text            # свежие сверху, 11+1... ждём 12..3
    assert "#2 " not in text                           # 11-я с конца не влезла
    assert "в работе" in text and "новый" in text      # статусы словами
    assert "🎯" in text


@pytest.mark.asyncio
async def test_r7_18_cmd_leads_empty_hint(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.config import settings
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.bot.pilot import cmd_leads
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_leads(upd, FakeContext([]))
    text = upd.message.replies[0][0]
    assert "Всего: 0" in text and "Пока пусто" in text
