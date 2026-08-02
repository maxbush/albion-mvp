"""Round 9 — total audit fixes (MASTER_PLAN v6).

Проверяем:
1) R9-1: LIKE-коллизия incident_id — resolve_absence(5) НЕ должен отменять
   workflow инцидента 55; точные поиски по parent_telegram_id.
"""

import json
import sqlite3

import pytest

from src.db.repository import (
    IncidentRepository,
    WorkflowRepository,
    ScheduledActionRepository,
    UserRepository,
)
from src.workflows.engine import engine
from src.workflows.absence import AbsenceWorkflow
from src.events.bus import bus
from src.events.types import Event, EventTypes


@pytest.mark.asyncio
async def test_r9_1_resolve_absence_does_not_cancel_other_incident_workflow(tmp_path, monkeypatch):
    """R9-1: инциденты 5 и 55 — resolve_absence(5) отменяет ТОЛЬКО свой workflow."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    con = sqlite3.connect("albion.db")
    con.execute("INSERT INTO incidents (id, lesson_ref, type, status) VALUES (5, 'L5', 'absence', 'pending')")
    con.execute("INSERT INTO incidents (id, lesson_ref, type, status) VALUES (55, 'L55', 'absence', 'pending')")
    con.commit()
    con.close()

    w5 = await engine.start_workflow("absence_notification", {
        "incident_id": 5, "parent_telegram_id": "777",
        "student_name": "Миша", "lesson_ref": "L5"})
    w55 = await engine.start_workflow("absence_notification", {
        "incident_id": 55, "parent_telegram_id": "999",
        "student_name": "Катя", "lesson_ref": "L55"})

    wf = AbsenceWorkflow("albion.db")
    await wf.resolve_absence(5, "777", resolution="parent_ok")

    repo = WorkflowRepository("albion.db")
    assert (await repo.get(w5))["state"] == "cancelled"
    # КРИТИЧНО: чужой workflow не тронут (раньше LIKE-подстрока матчила 55)
    assert (await repo.get(w55))["state"] == "running"
    assert (await IncidentRepository("albion.db").get(55))["status"] == "pending"


@pytest.mark.asyncio
async def test_r9_1_parent_reply_uses_correct_workflow(tmp_path, monkeypatch):
    """R9-1: notify_coordinators_parent_reply берёт данные СВОЕГО workflow (не 55)."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    con = sqlite3.connect("albion.db")
    con.execute("INSERT INTO incidents (id, lesson_ref, type, status) VALUES (5, 'L5', 'absence', 'pending')")
    con.execute("INSERT INTO incidents (id, lesson_ref, type, status) VALUES (55, 'L55', 'absence', 'pending')")
    con.commit()
    con.close()
    await UserRepository("albion.db").create("999", "coordinator", "Координатор")

    await engine.start_workflow("absence_notification", {
        "incident_id": 5, "parent_telegram_id": "777",
        "student_name": "Миша", "lesson_ref": "L5"})
    await engine.start_workflow("absence_notification", {
        "incident_id": 55, "parent_telegram_id": "999",
        "student_name": "Катя", "lesson_ref": "L55"})

    captured = []
    async def cap(ev):
        captured.append(ev.data)
    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        wf = AbsenceWorkflow("albion.db")
        await wf.notify_coordinators_parent_reply(5, "ok", parent_telegram_id="777")
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    coord_msgs = [d["message"] for d in captured if d.get("telegram_id") == "999"]
    assert coord_msgs, "координатор должен получить уведомление"
    text = coord_msgs[0]
    assert "Миша" in text and "Инцидент #5" in text
    assert "Катя" not in text and "#55" not in text  # данные чужого workflow не утекли


@pytest.mark.asyncio
async def test_r9_1_find_active_incident_exact_tg(tmp_path, monkeypatch):
    """R9-1: поиск по parent_telegram_id точен (777 не находит workflow с 7777)."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    await IncidentRepository("albion.db").create(lesson_ref="A", type="absence", status="pending")
    i2 = await IncidentRepository("albion.db").create(lesson_ref="B", type="absence", status="pending")
    await engine.start_workflow("absence_notification", {
        "incident_id": 1, "parent_telegram_id": "777", "student_name": "А"})
    await engine.start_workflow("absence_notification", {
        "incident_id": i2, "parent_telegram_id": "7777", "student_name": "Б"})

    wf = AbsenceWorkflow("albion.db")
    hit = await wf.find_active_incident_for_parent("777")
    assert hit is not None
    inc_id, data = hit
    assert inc_id == 1 and data["student_name"] == "А"
    assert data["parent_telegram_id"] == "777"


@pytest.mark.asyncio
async def test_r9_1_find_by_json_works_for_strings_and_ints(tmp_path, monkeypatch):
    """R9-1: хелпер find_by_json корректен для int и str полей."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    repo = WorkflowRepository("albion.db")

    w1 = await repo.create("t", "running", {"incident_id": 5, "class_id": "C9"})
    w2 = await repo.create("t", "running", {"incident_id": 55, "class_id": "C10"})

    assert [r["id"] for r in await repo.find_by_json("incident_id", 5)] == [w1]
    assert [r["id"] for r in await repo.find_by_json("incident_id", 55)] == [w2]
    assert [r["id"] for r in await repo.find_by_json("class_id", "C9")] == [w1]
    assert [r["id"] for r in await repo.find_by_json("class_id", "C1")] == []
    assert [r["id"] for r in await repo.find_by_json(
        "incident_id", 5, state="cancelled")] == []
    assert [r["id"] for r in await repo.find_by_json(
        "incident_id", 5, state="running")] == [w1]


# ── R9-13: notify_late_detail — actor-aware ────────────────────────────

@pytest.mark.asyncio
async def test_r9_13_late_detail_parent_says_student_late(tmp_path, monkeypatch):
    """R9-13: родитель жмёт «Опоздаю» → координатору «Ученик опоздает на N мин»,
    а НЕ «Репетитор задержится» (прежняя ложь)."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    await UserRepository("albion.db").create("999", "coordinator", "Координатор")

    from src.workflows.lesson_ops import LessonOpsWorkflow
    wid = await WorkflowRepository("albion.db").create("prelesson_parent", "running", {
        "class_id": "C9",
        "actor_type": "parent",
        "actor_telegram_id": "777",
        "student_name": "Миша",
        "tutor_name": "Анна",
        "start_time": "2099-08-02T15:00:00+00:00",
    })

    captured = []
    async def cap(ev):
        captured.append(ev.data)
    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await LessonOpsWorkflow("albion.db").notify_late_detail(wid, "15")
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    msgs = [d["message"] for d in captured if d.get("telegram_id") == "999"]
    assert msgs, "координатор должен получить уведомление"
    text = msgs[0]
    assert "Уточнение по опозданию ученика" in text
    assert "Ученик: Миша опоздает на 15 мин" in text
    assert "задержится" not in text


@pytest.mark.asyncio
async def test_r9_13_late_detail_tutor_says_tutor_late(tmp_path, monkeypatch):
    """R9-13: репетитор жмёт «Опоздаю» → координатору «Репетитор задержится на N мин»."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    await UserRepository("albion.db").create("999", "coordinator", "Координатор")

    from src.workflows.lesson_ops import LessonOpsWorkflow
    wid = await WorkflowRepository("albion.db").create("prelesson_tutor", "running", {
        "class_id": "C9",
        "actor_type": "tutor",
        "actor_telegram_id": "555",
        "tutor_name": "Анна",
        "student_names": ["Миша"],
        "start_time": "2099-08-02T15:00:00+00:00",
    })

    captured = []
    async def cap(ev):
        captured.append(ev.data)
    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await LessonOpsWorkflow("albion.db").notify_late_detail(wid, "30+")
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    msgs = [d["message"] for d in captured if d.get("telegram_id") == "999"]
    text = msgs[0]
    assert "Уточнение по опозданию репетитора" in text
    assert "Репетитор: Анна задержится на 30+ мин" in text
    assert "опоздает" not in text


# ── R9-2: мёртвая кнопка «➕ Ещё занятие» (wz:sched:again) ────────────

class _FakeUser:
    def __init__(self, id):
        self.id = id
        self.full_name = "Координатор"
        self.username = None


class _FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.replies = []
        self.message_id = 1

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))

    async def delete(self):
        pass


class _FakeChat:
    def __init__(self, id=42):
        self.id = id
        self.type = "private"


class _FakeQuery:
    def __init__(self, data, user):
        self.data = data
        self.from_user = user
        self.message = None
        self.edits = []
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))

    async def edit_message_reply_markup(self, *a, **k):
        pass


class _FakeBot:
    async def send_message(self, chat_id=None, text=None, reply_markup=None, **kw):
        return _FakeMessage(text)

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kw):
        pass


class _FakeCtx:
    def __init__(self):
        self.args = []
        self.bot = _FakeBot()


class _FakeUpd:
    def __init__(self, user, chat_id=42, text=None):
        self.effective_user = user
        self.effective_chat = _FakeChat(chat_id)
        self.message = _FakeMessage(text)
        self.callback_query = None


async def _wz_click(upd, ctx, data):
    from src.bot.wizard import handle_wz_callback
    upd.callback_query = _FakeQuery(data, upd.effective_user)
    await handle_wz_callback(upd, ctx)
    q = upd.callback_query
    upd.callback_query = None
    return q


async def _wz_seed(db):
    from src.db.repository import MeritHubStudentRepository
    srepo = MeritHubStudentRepository(db)
    await srepo.upsert("t01", merithub_user_id="mh_t01", name="Daniel John", role="tutor")
    await srepo.upsert("s01", merithub_user_id="mh_s01", name="Sofia",
                       parent_telegram_id="555", role="student")


@pytest.mark.asyncio
async def test_r9_2_again_button_restarts_schedule_wizard(tmp_path, monkeypatch):
    """R9-2: после успешного создания клик «➕ Ещё занятие» открывает новый
    визард (шаг выбора репетитора), а не «Этот сценарий уже закрыт»."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    from src.config import settings
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "999")
    from src.integrations.merithub_mock import MockMeritHubService
    monkeypatch.setattr("src.bot.wizard.get_merithub_service", lambda: MockMeritHubService())
    await _wz_seed("albion.db")

    from src.bot import wizard as wz
    upd = _FakeUpd(_FakeUser(999))
    ctx = _FakeCtx()

    # Полный happy path до создания
    await wz.cmd_schedule(upd, ctx)
    await _wz_click(upd, ctx, "wz:sched:tutor:t01")
    await _wz_click(upd, ctx, "wz:sched:student:s01")
    await _wz_click(upd, ctx, "wz:sched:sdone")
    await _wz_click(upd, ctx, "wz:sched:type:perma")
    await _wz_click(upd, ctx, "wz:sched:day:1")
    await _wz_click(upd, ctx, "wz:sched:ddone")
    await _wz_click(upd, ctx, "wz:sched:hour:13")
    await _wz_click(upd, ctx, "wz:sched:min:30")
    await _wz_click(upd, ctx, "wz:sched:dur:60")
    q = await _wz_click(upd, ctx, "wz:sched:confirm")
    texts = [t for t, _ in q.edits]
    assert any("Занятие создано" in t for t in texts)

    # Состояние удалено — как в проде
    from src.db.repository import WizardStateRepository
    assert await WizardStateRepository("albion.db").get("42") is None

    # Кнопка «➕ Ещё занятие» — ДОЛЖНА открыть новый визард
    q = await _wz_click(upd, ctx, "wz:sched:again")
    assert q.edits, "кнопка должна отредактировать сообщение"
    last = q.edits[-1][0]
    assert "Репетитор" in last and "Daniel John" in str(q.edits[-1][1].get("reply_markup"))
    assert "закрыт" not in last and "прерван" not in last
    # Новое состояние визарда создано (шаг tutor)
    st = await WizardStateRepository("albion.db").get("42")
    assert st is not None and st["step"] == "tutor" and st["flow"] == "schedule"
