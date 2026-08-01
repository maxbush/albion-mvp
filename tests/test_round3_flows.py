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
    """Неизвестный урок: отправитель получает честный «не найден» НЕМЕДЛЕННО —
    и никакого противоречивого «🔄 передана координаторам» следом.

    Баг, найденный сухим прогоном (scripts/demo_dry_run.py): команда раньше
    всегда отвечала «Отмена передана», даже когда workflow не нашёл урок.
    Теперь проверка урока — ДО публикации события (workflow-фидбэк остаётся
    как защита от гонки «урок исчез между проверкой и обработкой»)."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import cmd_cancel_lesson

    captured = []

    async def cap_cancelled(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.LESSON_CANCELLED, cap_cancelled)
    try:
        upd = FakeUpdate(FakeUser(9, full_name="Заказчик"))
        await cmd_cancel_lesson(upd, FakeContext(["unknown_x"]))
        replies = [t for t, _ in upd.message.replies]
        assert any("не найден" in t for t in replies), replies
        assert not any("передана" in t for t in replies), replies
        assert not captured, "событие LESSON_CANCELLED не должно публиковаться для неизвестного урока"
    finally:
        bus.unsubscribe(EventTypes.LESSON_CANCELLED, cap_cancelled)


@pytest.mark.asyncio
async def test_cancel_lesson_workflow_race_fallback_notifies_reporter(tmp_path, monkeypatch):
    """Защита от гонки: если урок исчез УЖЕ после проверки командой,
    workflow всё ещё шлёт отправителю «не найден» через шину."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.workflows.cancellation import CancellationWorkflow
    from src.events.types import Event

    wf = CancellationWorkflow(db)
    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.LESSON_CANCELLED, wf.handle_cancelled)
    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        # Публикуем событие напрямую — как будто команда проверила урок,
        # а потом он пропал из интеграции.
        await bus.publish(Event(EventTypes.LESSON_CANCELLED, {
            "lesson_id": "ghost_lesson", "reason": "тест", "reported_by": "9",
        }))
        reporter_msgs = [d for d in captured if d.get("telegram_id") == "9"]
        assert reporter_msgs, "отправитель должен получить фидбэк"
        assert "не найден" in reporter_msgs[0]["message"]
    finally:
        bus.unsubscribe(EventTypes.LESSON_CANCELLED, wf.handle_cancelled)
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)


@pytest.mark.asyncio
async def test_p03_cancel_lesson_usage_hint(tmp_path, monkeypatch):
    """Без аргументов и личных занятий — честное пустое состояние, события нет.

    (Round 6: вместо техподсказки «<ID>» — персональный список кнопками;
    если занятий нет — просим написать координатору.)"""
    await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import cmd_cancel_lesson

    async def boom(_ev):
        raise AssertionError("не должно быть события LESSON_CANCELLED без аргументов")

    bus.subscribe(EventTypes.LESSON_CANCELLED, boom)
    try:
        upd = FakeUpdate(FakeUser(9))
        await cmd_cancel_lesson(upd, FakeContext([]))
        texts = [t for t, _ in upd.message.replies]
        assert any("координатору" in t for t in texts)
    finally:
        bus.unsubscribe(EventTypes.LESSON_CANCELLED, boom)


# ── P0.4: parse_mode + экранирование динамики в Markdown-ответах ─────

@pytest.mark.asyncio
async def test_p04_mh_students_has_parse_mode_and_escapes(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.config import settings
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.bot.pilot import cmd_mh_students
    from src.db.repository import MeritHubStudentRepository

    # Имя с underscore — раньше ломало бы Markdown-отправку без экранирования.
    await MeritHubStudentRepository(db).upsert(
        "s01", merithub_user_id="mh_s01", name="Anna_Maria Jones",
        parent_telegram_id="555", timezone="Europe/London", role="student")

    upd = FakeUpdate(FakeUser(100))
    await cmd_mh_students(upd, FakeContext([]))
    assert upd.message.replies, "ожидался ответ со списком учеников"
    text, kw = upd.message.replies[0]
    assert kw.get("parse_mode") == "Markdown", "Markdown-разметка требует parse_mode"
    assert "*Anna\\_Maria Jones*" in text  # жирный сохранён, underscore экранирован


@pytest.mark.asyncio
async def test_p04_mh_contacts_has_parse_mode(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.config import settings
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.bot.pilot import cmd_mh_contacts
    from src.db.repository import MeritHubContactRepository

    await MeritHubContactRepository(db).upsert(
        "s01", telegram_id="555", role="parent", name="Parent_One", phone="+44_123")

    upd = FakeUpdate(FakeUser(100))
    await cmd_mh_contacts(upd, FakeContext([]))
    text, kw = upd.message.replies[0]
    assert kw.get("parse_mode") == "Markdown"
    assert "Parent\\_One" in text
    assert "+44\\_123" in text


@pytest.mark.asyncio
async def test_p04_seed10_has_parse_mode(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.config import settings
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.bot.pilot import cmd_seed10

    upd = FakeUpdate(FakeUser(100))
    await cmd_seed10(upd, FakeContext(["555"]))
    text, kw = upd.message.replies[0]
    assert kw.get("parse_mode") == "Markdown"
    # backticks вокруг команд должны интерпретироваться Telegram, т.к. parse_mode есть
    assert "`/mh_schedule" in text


# ── P1.1: absence_report intent → координаторы ───────────────────────

@pytest.mark.asyncio
async def test_p11_absence_report_reaches_coordinators(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import UserRepository
    from src.events.types import Event
    from src.workflows.absence import AbsenceWorkflow

    await UserRepository(db).create("coord_1", "coordinator", "Координатор")
    wf = AbsenceWorkflow(db)
    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await wf.handle_classified(Event(EventTypes.MESSAGE_CLASSIFIED, {
            "intent": "absence_report",
            "text": "мой сын сегодня не смог прийти на урок",
            "telegram_id": "555",
        }))
        msgs = [d for d in captured if d.get("telegram_id") == "coord_1"]
        assert msgs, "координатор должен получить репорт о неявке"
        assert "не смог прийти" in msgs[0]["message"]
        # R7-4: сырой TG не печатаем — он в url-кнопке «Написать пользователю».
        assert "TG 555" not in msgs[0]["message"]
        btns = msgs[0].get("buttons") or []
        assert any(b.get("url") == "tg://user?id=555" for b in btns)
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)


@pytest.mark.asyncio
async def test_p11_other_intents_ignored(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import UserRepository
    from src.events.types import Event
    from src.workflows.absence import AbsenceWorkflow

    await UserRepository(db).create("coord_1", "coordinator", "Координатор")
    wf = AbsenceWorkflow(db)
    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await wf.handle_classified(Event(EventTypes.MESSAGE_CLASSIFIED, {
            "intent": "question", "text": "а когда урок?", "telegram_id": "555",
        }))
        assert not captured, "на другие интенты absence-обработчик не реагирует"
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)


# ── P1.2: протухшие check-in workflow'ы ─────────────────────────────

@pytest.mark.asyncio
async def test_p12_stale_checkin_expired_and_ignored(tmp_path, monkeypatch):
    """Check-in от занятия 5 часов назад НЕ перехватывает новые сообщения,
    а сам добивается до completed/expired (с отменой отложенных действий)."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from datetime import datetime, timedelta, timezone
    from src.db.repository import ScheduledActionRepository, WorkflowRepository
    from src.workflows.lesson_ops import LessonOpsWorkflow

    repo = WorkflowRepository(db)
    sched = ScheduledActionRepository(db)
    start_past = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    wid = await repo.create("prelesson_parent", "running", {
        "actor_type": "parent", "actor_telegram_id": "555",
        "nonce": "abc123", "start_time": start_past, "class_id": "C_STALE",
    })
    aid = await sched.create(
        wid, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "parent_prelesson_no_reply", {"workflow_id": wid})

    ops = LessonOpsWorkflow(db)
    assert await ops.find_active_checkin("555", ("parent",)) is None

    wf = await repo.get(wid)
    assert wf["state"] == "completed"
    import json as _json
    assert _json.loads(wf["data"]).get("response_status") == "expired"
    act = await sched._fetchone("SELECT * FROM scheduled_actions WHERE id=?", (aid,))
    assert act["status"] == "cancelled"


@pytest.mark.asyncio
async def test_p12_fresh_checkin_still_found(tmp_path, monkeypatch):
    """Свежий check-in (занятие через час) по-прежнему находится."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from datetime import datetime, timedelta, timezone
    from src.db.repository import WorkflowRepository
    from src.workflows.lesson_ops import LessonOpsWorkflow

    repo = WorkflowRepository(db)
    start_future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    wid = await repo.create("prelesson_parent", "running", {
        "actor_type": "parent", "actor_telegram_id": "555",
        "nonce": "abc123", "start_time": start_future, "class_id": "C_FRESH",
    })
    ops = LessonOpsWorkflow(db)
    found = await ops.find_active_checkin("555", ("parent",))
    assert found is not None
    assert found[0] == wid
    # И не должен помечаться expired
    wf = await repo.get(wid)
    assert wf["state"] == "running"


# ── P1.3: /absent — неизвестный урок → фидбэк отправителю ────────────

@pytest.mark.asyncio
async def test_p13_absent_unknown_lesson_notifies_reporter(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.events.types import Event
    from src.workflows.absence import AbsenceWorkflow

    wf = AbsenceWorkflow(db)
    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await wf.handle_lesson_absent(Event(EventTypes.LESSON_ABSENT, {
            "lesson_id": "unknown_x", "reported_by": "777",
        }))
        msgs = [d for d in captured if d.get("telegram_id") == "777"]
        assert msgs, "отправитель /absent должен узнать, что урок не найден"
        assert "не найден" in msgs[0]["message"]
        assert "unknown_x" in msgs[0]["message"]
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)


@pytest.mark.asyncio
async def test_p13_absent_known_lesson_still_works(tmp_path, monkeypatch):
    """Регрессия: известный mock-урок → инцидент + workflow создаются как раньше."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.workflows.engine import engine
    from src.db.repository import (
        IncidentRepository, ScheduledActionRepository, WorkflowRepository,
    )
    from src.events.types import Event
    from src.workflows.absence import AbsenceWorkflow

    engine.repo = WorkflowRepository(db)
    engine.scheduler = ScheduledActionRepository(db)
    await _seed_lesson_student_user(db)

    wf = AbsenceWorkflow(db)
    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await wf.handle_lesson_absent(Event(EventTypes.LESSON_ABSENT, {
            "lesson_id": "lesson_1", "reported_by": "111111",
        }))
        # Инцидент создан и pending — «урок не найден» НЕ отправлен.
        rows = await IncidentRepository(db)._fetchall("SELECT * FROM incidents")
        assert len(rows) == 1 and rows[0]["type"] == "absence"
        assert not any("не найден" in (d.get("message") or "") for d in captured)
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)


async def _seed_lesson_student_user(db):
    """Родитель из mock Airtable (student_1 → parent_1) должен быть в users."""
    from src.db.repository import UserRepository
    await UserRepository(db).create("parent_1", "parent", "Родитель Миши")


# ── P1.4: scheduler zombie reaper ────────────────────────────────────

@pytest.mark.asyncio
async def test_p14_zombie_running_action_marked_failed(tmp_path, monkeypatch):
    """running + attempts>=3 + просроченный lock → failed, а не вечный зомби."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from datetime import datetime, timedelta, timezone
    from src.db.repository import ScheduledActionRepository, WorkflowRepository

    repo = ScheduledActionRepository(db)
    wid = await WorkflowRepository(db).create("test", "running", {})
    aid = await repo.create(
        wid, (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        "notify_parent", {"incident_id": 1})
    # Симулируем краш после 3-й попытки: running, lock истёк, attempts=3.
    await repo._execute(
        "UPDATE scheduled_actions SET status='running', attempts=3, locked_until=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"), aid))

    claimed = await repo.claim_pending(limit=10)
    assert all(t["id"] != aid for t in claimed), "зомби не должен выполняться снова"
    row = await repo._fetchone("SELECT * FROM scheduled_actions WHERE id=?", (aid,))
    assert row["status"] == "failed"
    assert "lock expired" in row["last_error"]


@pytest.mark.asyncio
async def test_p14_expired_lock_with_retries_left_still_reaped(tmp_path, monkeypatch):
    """Регрессия: running + attempts<3 + истёкший lock → back to pending и claim."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from datetime import datetime, timedelta, timezone
    from src.db.repository import ScheduledActionRepository, WorkflowRepository

    repo = ScheduledActionRepository(db)
    wid = await WorkflowRepository(db).create("test", "running", {})
    aid = await repo.create(
        wid, (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        "notify_parent", {"incident_id": 1})
    await repo._execute(
        "UPDATE scheduled_actions SET status='running', attempts=2, locked_until=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"), aid))

    claimed = await repo.claim_pending(limit=10)
    assert [t["id"] for t in claimed] == [aid]


# ── P1.5: эскалация с полным контекстом ──────────────────────────────

@pytest.mark.asyncio
async def test_p15_escalation_message_has_context(tmp_path, monkeypatch):
    """Координаторская эскалация содержит ученика, занятие и способ связаться."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import (
        IncidentRepository, MeritHubClassRepository, UserRepository,
        WorkflowRepository, ScheduledActionRepository,
    )
    from src.workflows.engine import engine
    from src.workflows.absence import AbsenceWorkflow

    engine.repo = WorkflowRepository(db)
    engine.scheduler = ScheduledActionRepository(db)
    await UserRepository(db).create("coord_1", "coordinator", "Координатор")
    await MeritHubClassRepository(db).upsert(
        "C9", title="Math", start_time="2026-07-31T15:00:00+00:00")

    inc_id = await IncidentRepository(db).create(
        lesson_ref="C9", type="absence", status="pending")
    wid = await engine.start_workflow("absence_notification", {
        "incident_id": inc_id, "student_name": "Миша Иванов",
        "parent_telegram_id": "555", "lesson_ref": "C9"})

    wf = AbsenceWorkflow(db)
    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await wf._escalate(wid, inc_id, reason="no response")
        msgs = [d for d in captured if d.get("telegram_id") == "coord_1"]
        assert msgs, "эскалация должна уйти координатору"
        m = msgs[0]["message"]
        assert f"#{inc_id}" in m
        assert "no response" in m
        assert "Миша Иванов" in m          # ученик
        assert "C9" in m                   # занятие
        assert "31.07" in m                # человекочитаемая дата из метаданных
        # R7-4: TG родителя — в tg://-кнопке, а не сырой строкой в тексте.
        assert "555" not in m
        btns = msgs[0].get("buttons") or []
        assert any(b.get("url") == "tg://user?id=555" for b in btns)
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)


@pytest.mark.asyncio
async def test_p15_escalation_minimal_when_no_context(tmp_path, monkeypatch):
    """Эскалация по «неизвестному» поводу не падает даже без workflow-данных."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.db.repository import (
        IncidentRepository, UserRepository, WorkflowRepository, ScheduledActionRepository,
    )
    from src.workflows.engine import engine
    from src.workflows.absence import AbsenceWorkflow

    engine.repo = WorkflowRepository(db)
    engine.scheduler = ScheduledActionRepository(db)
    await UserRepository(db).create("coord_1", "coordinator", "Координатор")
    inc_id = await IncidentRepository(db).create(type="absence", status="pending")
    wid = await engine.start_workflow("absence_notification", {"incident_id": inc_id})

    wf = AbsenceWorkflow(db)
    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await wf._escalate(wid, inc_id, reason="parent not registered")
        msgs = [d for d in captured if d.get("telegram_id") == "coord_1"]
        assert msgs
        assert "parent not registered" in msgs[0]["message"]
        assert f"#{inc_id}" in msgs[0]["message"]
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)


# ── P2.4: единый текст помощи координатора в обоих путях ────────────

class _FakeChatRich:
    def __init__(self, id=1):
        self.id = id
        self.sent = []

    async def send_message(self, text, **kw):
        self.sent.append((text, kw))


class _FakeQuery:
    def __init__(self, data, user):
        self.data = data
        self.from_user = user
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        pass

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))

    async def edit_message_reply_markup(self, markup):
        pass


@pytest.mark.asyncio
async def test_p24_coordinator_help_is_unified(tmp_path, monkeypatch):
    """Помощь координатора едина: /start и регистрация ведут на одну кнопку «📋 Команды»,
    которая показывает один и тот же текст (P2.4 + U4)."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.bot.handlers import cmd_start, handle_callback, _coordinator_help_text
    from src.db.repository import UserRepository

    await UserRepository(db).create("321", "coordinator", "Координатор")

    # Путь 1: /start → одно сообщение с кнопкой «Команды» → help по тапу.
    upd1 = FakeUpdate(FakeUser(321))
    await cmd_start(upd1, FakeContext([]))
    assert len(upd1.message.replies) == 1, "U4: /start координатора — одно сообщение"

    upd1.callback_query = _FakeQuery("help_commands", FakeUser(321))
    await handle_callback(upd1, FakeContext([]))
    help_from_start = upd1.callback_query.edits[-1][0]

    # Путь 2: регистрация → тот же тап «Команды» → тот же текст.
    upd2 = FakeUpdate(FakeUser(654))
    upd2.callback_query = _FakeQuery("register_coordinator", upd2.effective_user)
    await handle_callback(upd2, FakeContext([]))
    upd2.callback_query = _FakeQuery("help_commands", FakeUser(654))
    await handle_callback(upd2, FakeContext([]))
    help_from_button = upd2.callback_query.edits[-1][0]

    assert help_from_start == help_from_button == _coordinator_help_text()
    # Underscore в командах экранирован (Markdown V1), иначе текст ломается.
    assert "/pilot\\_absent" in help_from_start
    assert "/cancel\\_lesson <ID>" in help_from_start
    assert "/mh\\_schedule" in help_from_start
    for line in help_from_start.splitlines():
        if line.strip().startswith("/"):
            assert "\\_" in line or "_" not in line, line
