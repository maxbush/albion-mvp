"""Тесты edge-case сценариев и новых демо-команд.

P2.1: post-escalation button press
P2.2: duplicate button press (different button after resolve)
P2.3: self-registration flow
P2.4: find_escalated_incident_for_parent
P2.5: cmd_seed10
P2.6: cmd_demo_reset
P2.7: cmd_incidents, cmd_today, cmd_morning
P2.8: cmd_mh_contact + phone/email
"""

import json
from datetime import datetime, timezone, timedelta

import pytest

from src.config import settings
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.workflows.engine import engine
from src.workflows.absence import AbsenceWorkflow
from src.db.repository import (
    IncidentRepository, WorkflowRepository, ScheduledActionRepository,
    UserRepository, MeritHubStudentRepository, MeritHubContactRepository,
    MeritHubClassRepository,
)


# ── фейки telegram ────────────────────────────────────────────────────
class FakeUser:
    def __init__(self, id, username=None, full_name="T"):
        self.id = id
        self.username = username
        self.full_name = full_name


class FakeMessage:
    def __init__(self):
        self.replies = []
        self.text = ""

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class FakeChat:
    def __init__(self, id=1):
        self.id = id


class FakeUpdate:
    def __init__(self, user, chat_id=1):
        self.effective_user = user
        self.effective_chat = FakeChat(chat_id)
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []


# ── P2.1: post-escalation button press ────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_after_escalation_marks_resolved_and_notifies(tmp_path, monkeypatch):
    """Инцидент escalated → parent нажимает кнопку → inc resolved + coord notified."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    await UserRepository("albion.db").create("777", "parent", "Parent")
    await UserRepository("albion.db").create("999", "coordinator", "Coord")

    # Создаём инцидент и workflow
    inc_id = await IncidentRepository("albion.db").create(
        lesson_ref="l1", type="absence", status="pending")
    wid = await engine.start_workflow("absence_notification", {
        "incident_id": inc_id, "parent_telegram_id": "777",
        "student_name": "Миша", "lesson_ref": "l1"})

    # Эскалируем
    wf = AbsenceWorkflow("albion.db")
    await wf._escalate(wid, inc_id)
    inc = await IncidentRepository("albion.db").get(inc_id)
    assert inc["status"] == "escalated"

    # Parent нажимает кнопку → resolve_absence
    captured = []
    async def capture(event):
        captured.append(event.data)
    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, capture)
    try:
        await wf.resolve_absence(inc_id, "777", resolution="parent_ok")
        await wf.notify_coordinators_parent_reply(inc_id, "ok", parent_telegram_id="777")
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, capture)

    inc = await IncidentRepository("albion.db").get(inc_id)
    assert inc["status"] == "resolved"
    assert inc["resolution"] == "parent_ok"
    # Coordinator получил уведомление об ответе
    assert any(d.get("telegram_id") == "999" for d in captured)


# ── P2.2: duplicate button press ─────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_absence_idempotent(tmp_path, monkeypatch):
    """Повторный resolve_absence на уже закрытый инцидент — no-op."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    inc_id = await IncidentRepository("albion.db").create(
        lesson_ref="l1", type="absence", status="pending")
    wid = await engine.start_workflow("absence_notification", {
        "incident_id": inc_id, "parent_telegram_id": "777"})

    wf = AbsenceWorkflow("albion.db")
    await wf.resolve_absence(inc_id, "777", "parent_ok")
    inc = await IncidentRepository("albion.db").get(inc_id)
    assert inc["status"] == "resolved"
    assert inc["resolution"] == "parent_ok"

    # Повторный вызов — no-op, resolution не меняется
    await wf.resolve_absence(inc_id, "777", "parent_late")
    inc = await IncidentRepository("albion.db").get(inc_id)
    assert inc["resolution"] == "parent_ok"  # не изменилось


# ── P2.3: self-registration flow ─────────────────────────────────────

@pytest.mark.asyncio
async def test_self_registration_creates_user_with_role(tmp_path, monkeypatch):
    """Новый юзер → register_parent → создана запись с ролью parent."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")

    repo = UserRepository("albion.db")
    uid, created = await repo.set_role_by_telegram("555", "parent", name="New Parent")
    assert created is True
    user = await repo.get(uid)
    assert user["role"] == "parent"
    assert user["telegram_id"] == "555"
    assert user["name"] == "New Parent"

    # Повторный вызов — обновляет, не создаёт
    uid2, created2 = await repo.set_role_by_telegram("555", "tutor")
    assert created2 is False
    assert uid2 == uid
    user = await repo.get(uid)
    assert user["role"] == "tutor"


# ── P2.4: find_escalated_incident_for_parent ─────────────────────────

@pytest.mark.asyncio
async def test_find_escalated_incident_for_parent(tmp_path, monkeypatch):
    """Эскалированный инцидент находится через find_escalated."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    inc_id = await IncidentRepository("albion.db").create(
        lesson_ref="l1", type="absence", status="pending")
    wid = await engine.start_workflow("absence_notification", {
        "incident_id": inc_id, "parent_telegram_id": "777",
        "student_name": "Миша", "lesson_ref": "l1"})

    wf = AbsenceWorkflow("albion.db")

    # До эскалации — не находит
    result = await wf.find_escalated_incident_for_parent("777")
    assert result is None

    # Эскалируем
    await wf._escalate(wid, inc_id)

    # Теперь находит
    result = await wf.find_escalated_incident_for_parent("777")
    assert result is not None
    found_inc_id, data = result
    assert found_inc_id == inc_id
    assert data.get("student_name") == "Миша"


# ── P2.5: cmd_seed10 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_seed10_creates_students_and_tutors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    await UserRepository("albion.db").create("100", "coordinator", "Admin")

    from src.bot.pilot import cmd_seed10
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_seed10(upd, FakeContext(["333", "444"]))

    srepo = MeritHubStudentRepository("albion.db")
    students = await srepo.list_all()
    assert len(students) == 13  # 10 students + 3 tutors

    # s01-s05 → parent 333
    s01 = await srepo.get_by_client_id("s01")
    assert s01 and s01["parent_telegram_id"] == "333"

    # s06-s10 → parent 444
    s06 = await srepo.get_by_client_id("s06")
    assert s06 and s06["parent_telegram_id"] == "444"

    # Tutors
    t01 = await srepo.get_by_client_id("t01")
    assert t01 and t01["role"] == "tutor"

    assert any("Создано 10 учеников" in r for r in upd.message.replies)


# ── P2.6: cmd_demo_reset ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_demo_reset_clears_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    await UserRepository("albion.db").create("100", "coordinator", "Admin")

    # Создаём инцидент
    await IncidentRepository("albion.db").create(lesson_ref="l1", type="absence", status="pending")
    incs = await IncidentRepository("albion.db")._fetchall("SELECT * FROM incidents")
    assert len(incs) == 1

    from src.bot.pilot import cmd_demo_reset
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_demo_reset(upd, FakeContext([]))

    incs = await IncidentRepository("albion.db")._fetchall("SELECT * FROM incidents")
    assert len(incs) == 0
    assert any("Демо-сброс" in r for r in upd.message.replies)


# ── P2.7: cmd_incidents ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_incidents_shows_stats(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    await UserRepository("albion.db").create("100", "coordinator", "Admin")

    await IncidentRepository("albion.db").create(lesson_ref="l1", type="absence", status="pending")
    await IncidentRepository("albion.db").create(lesson_ref="l2", type="absence", status="resolved")

    from src.bot.pilot import cmd_incidents
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_incidents(upd, FakeContext([]))

    assert len(upd.message.replies) == 1
    text = upd.message.replies[0]
    assert "Ожидают: 1" in text
    assert "Закрыто: 1" in text


# ── P2.7b: cmd_today ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_today_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    await UserRepository("albion.db").create("100", "coordinator", "Admin")

    from src.bot.pilot import cmd_today
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_today(upd, FakeContext([]))
    assert len(upd.message.replies) == 1
    assert "Обзор" in upd.message.replies[0]


# ── P2.7c: cmd_morning ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_morning_no_classes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    await UserRepository("albion.db").create("100", "coordinator", "Admin")

    from src.bot.pilot import cmd_morning_digest
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_morning_digest(upd, FakeContext([]))
    assert any("Доброе утро" in r for r in upd.message.replies)


# ── P2.8: cmd_mh_contact ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_mh_contact_adds_phone_email(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    await UserRepository("albion.db").create("100", "coordinator", "Admin")

    srepo = MeritHubStudentRepository("albion.db")
    await srepo.upsert("s01", name="Алиса", role="student")

    from src.bot.pilot import cmd_mh_contact
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_mh_contact(upd, FakeContext(["s01", "phone", "+447493994501"]))
    assert any("обновлён" in r for r in upd.message.replies)

    contact = await MeritHubContactRepository("albion.db").get("s01")
    assert contact and contact["phone"] == "+447493994501"

    # Add email
    upd2 = FakeUpdate(FakeUser(100, "admin"))
    await cmd_mh_contact(upd2, FakeContext(["s01", "email", "parent@test.com"]))
    contact = await MeritHubContactRepository("albion.db").get("s01")
    assert contact["email"] == "parent@test.com"
    assert contact["phone"] == "+447493994501"  # не затёрлось


@pytest.mark.asyncio
async def test_cmd_mh_contact_unknown_student(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    await UserRepository("albion.db").create("100", "coordinator", "Admin")

    from src.bot.pilot import cmd_mh_contact
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_mh_contact(upd, FakeContext(["nonexistent", "phone", "+123"]))
    assert any("не найден" in r for r in upd.message.replies)


# ── P1.3: cmd_mh_user with email= and phone= ─────────────────────────

@pytest.mark.asyncio
async def test_cmd_mh_user_with_email_and_phone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    await UserRepository("albion.db").create("100", "coordinator", "Admin")

    from src.bot.pilot import cmd_mh_user
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_mh_user(upd, FakeContext(["s01", "333", "Алиса", "email=p@ex.com", "phone=+44123"]))

    # Student created
    srepo = MeritHubStudentRepository("albion.db")
    s = await srepo.get_by_client_id("s01")
    assert s and s["parent_telegram_id"] == "333"

    # Contact stored with email and phone
    contact = await MeritHubContactRepository("albion.db").get("s01")
    assert contact and contact["email"] == "p@ex.com"
    assert contact["phone"] == "+44123"

    assert any("Контакты родителя" in r for r in upd.message.replies)


# ── P1.4: find_escalated with timezone-aware resolved_at ─────────────

@pytest.mark.asyncio
async def test_find_escalated_respects_2h_window(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    inc_id = await IncidentRepository("albion.db").create(
        lesson_ref="l1", type="absence", status="pending")
    wid = await engine.start_workflow("absence_notification", {
        "incident_id": inc_id, "parent_telegram_id": "777",
        "student_name": "Test", "lesson_ref": "l1"})

    wf = AbsenceWorkflow("albion.db")
    await wf._escalate(wid, inc_id)

    # Just escalated → should find
    result = await wf.find_escalated_incident_for_parent("777")
    assert result is not None

    # Manually set resolved_at to 3 hours ago → should NOT find
    from datetime import datetime as _dt, timezone as _tz
    old_time = (_dt.now(_tz.utc) - timedelta(hours=3)).isoformat()
    await IncidentRepository("albion.db")._execute(
        "UPDATE incidents SET resolved_at=? WHERE id=?", (old_time, inc_id))

    result = await wf.find_escalated_incident_for_parent("777")
    assert result is None


# ── P1.5: timezone in student model ──────────────────────────────────

@pytest.mark.asyncio
async def test_student_timezone_stored_and_retrieved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")

    srepo = MeritHubStudentRepository("albion.db")
    await srepo.upsert("s01", name="Алиса", timezone="Asia/Almaty", country="Kazakhstan", role="student")

    s = await srepo.get_by_client_id("s01")
    assert s["timezone"] == "Asia/Almaty"
    assert s["country"] == "Kazakhstan"


# ── P1.1: format_dual_time helper ───────────────────────────────────

def test_format_dual_time_london_only():
    from src.workflows.lesson_ops import _format_dual_time
    result = _format_dual_time("2026-07-28T15:00:00+01:00", None)
    assert "15:00" in result
    assert "London" in result


def test_format_dual_time_with_user_tz():
    from src.workflows.lesson_ops import _format_dual_time
    result = _format_dual_time("2026-07-28T15:00:00+01:00", "Asia/Almaty")
    assert "London" in result
    assert "ваше время" in result
    assert "Asia/Almaty" in result
