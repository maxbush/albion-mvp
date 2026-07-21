"""Тесты демо-пилота: /pilot_seed, /pilot_absent, прогон сценария на реальных TG."""

import json
import pytest

from src.config import settings
from src.db.repository import (
    UserRepository, IncidentRepository, ScheduledActionRepository, WorkflowRepository,
    MeritHubStudentRepository, MeritHubEnrollmentRepository,
)
from src.workflows.engine import engine
from src.integrations.merithub_client import MeritHubClient


class StubMeritHub:
    """Лёгкая заглушка клиента MeritHub для команд /mh_tutor и /mh_schedule."""

    def __init__(self):
        self.calls = []
        self.last_add = None

    async def add_user(self, **kw):
        self.calls.append(("add_user", kw))
        return {"userId": f"mh_{kw['client_user_id']}"}

    async def schedule_class(self, instructor, **kw):
        self.calls.append(("schedule", instructor, kw))
        return {"classId": "C9", "hostLink": "HL",
                "commonLinks": {"commonHostLink": "HL", "commonParticipantLink": "PL"}}

    async def add_users_to_class(self, class_id, users):
        self.last_add = (class_id, users)
        return {"users": [{"userId": u["userId"], "userLink": "u_" + u["userId"]} for u in users]}

    parse_schedule = staticmethod(MeritHubClient.parse_schedule)
    parse_user_links = staticmethod(MeritHubClient.parse_user_links)

    def room_url(self, link, device_test=False):
        return f"ROOM/{link}"


# ── Лёгкие фейки telegram-объектов ─────────────────────────────────────
class FakeUser:
    def __init__(self, id, username=None, full_name="T"):
        self.id = id
        self.username = username
        self.full_name = full_name


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, user):
        self.effective_user = user
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []


@pytest.mark.asyncio
async def test_pilot_seed_reports_missing_roles(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    await UserRepository("albion.db").create("100", "coordinator", "Админ")

    from src.bot.pilot import cmd_pilot_seed
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_pilot_seed(upd, FakeContext([]))
    # Нет parent → пилот не готов
    assert any("❌" in r and "parent" in r for r in upd.message.replies)


@pytest.mark.asyncio
async def test_pilot_absent_creates_flow_with_real_parent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    monkeypatch.setattr(settings, "albion_pilot_student_name", "Тест Ученик")

    # engine → albion.db (tmp)
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    repo = UserRepository("albion.db")
    await repo.create("100", "coordinator", "Админ")
    await repo.create("777", "parent", "Родитель Пилотный")
    await repo.create("888", "tutor", "Репетитор")

    from src.bot.pilot import cmd_pilot_absent, cmd_pilot_seed

    # preflight: теперь есть parent + coordinator → готов
    seed = FakeUpdate(FakeUser(100, "admin"))
    await cmd_pilot_seed(seed, FakeContext([]))
    assert any("✅ Пилот готов" in r for r in seed.message.replies)

    # запуск сценария
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_pilot_absent(upd, FakeContext([]))
    assert any("🚀" in r for r in upd.message.replies)

    # инцидент создан
    inc = await IncidentRepository("albion.db").get(1)
    assert inc and inc["type"] == "absence" and inc["status"] == "pending"

    # запланировано уведомление родителя
    actions = await ScheduledActionRepository("albion.db")._fetchall(
        "SELECT * FROM scheduled_actions WHERE action='notify_parent'")
    assert len(actions) == 1

    # workflow несёт реальный TG родителя и имя ученика
    wf = await WorkflowRepository("albion.db").get(1)
    data = json.loads(wf["data"])
    assert data["parent_telegram_id"] == "777"
    assert data["student_name"] == "Тест Ученик"


@pytest.mark.asyncio
async def test_pilot_absent_gated_for_non_admin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.bot.pilot import cmd_pilot_absent
    upd = FakeUpdate(FakeUser(999, "rando"))
    await cmd_pilot_absent(upd, FakeContext([]))
    assert any("⛔" in r for r in upd.message.replies)


@pytest.mark.asyncio
async def test_notify_parent_uses_workflow_parent_not_mock(tmp_path, monkeypatch):
    """_notify_parent берёт родителя из данных workflow (реальный TG), а не из mock."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    from src.workflows.absence import AbsenceWorkflow
    from src.events.bus import bus
    from src.events.types import EventTypes

    # Регистрируем родителя с реальным TG (как через /role в пилоте)
    await UserRepository("albion.db").create("777", "parent", "Родитель")

    inc_id = await IncidentRepository("albion.db").create(
        lesson_ref="pilot_lesson_1", student_id="pilot_student_1",
        type="absence", status="pending")
    wid = await engine.start_workflow("absence_notification", {
        "incident_id": inc_id, "student_name": "Тест",
        "parent_telegram_id": "777", "lesson_ref": "pilot_lesson_1",
    })

    captured = []

    async def capture(event):
        captured.append(event.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, capture)
    try:
        wf = AbsenceWorkflow("albion.db")
        await wf._notify_parent(wid, inc_id)
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, capture)

    # Уведомление ушло на реальный TG родителя из данных workflow
    assert any(d.get("telegram_id") == "777" for d in captured)
    # И scheduled-эскалация создана
    esc = await ScheduledActionRepository("albion.db")._fetchall(
        "SELECT * FROM scheduled_actions WHERE action='escalate'")
    assert len(esc) == 1


@pytest.mark.asyncio
async def test_cmd_mh_tutor_creates_and_maps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    monkeypatch.setattr(settings, "merithub_client_id", "cid")
    monkeypatch.setattr(settings, "merithub_client_secret", "csec")
    stub = StubMeritHub()
    monkeypatch.setattr("src.integrations.factory.get_merithub_service", lambda: stub)
    await UserRepository("albion.db").create("100", "coordinator", "Админ")

    from src.bot.pilot import cmd_mh_tutor
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_mh_tutor(upd, FakeContext(["t1", "Anna"]))
    s = await MeritHubStudentRepository("albion.db").get_by_client_id("t1")
    assert s and s["role"] == "tutor" and s["merithub_user_id"] == "mh_t1"
    assert any("mh_t1" in r for r in upd.message.replies)


@pytest.mark.asyncio
async def test_cmd_mh_schedule_creates_class_and_enrolls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    monkeypatch.setattr(settings, "merithub_client_id", "cid")
    monkeypatch.setattr(settings, "merithub_client_secret", "csec")
    stub = StubMeritHub()
    monkeypatch.setattr("src.integrations.factory.get_merithub_service", lambda: stub)
    await UserRepository("albion.db").create("100", "coordinator", "Админ")
    srepo = MeritHubStudentRepository("albion.db")
    await srepo.upsert("t1", merithub_user_id="mh_t1", name="Anna", role="tutor")
    await srepo.upsert("s1", merithub_user_id="mh_s1", name="Миша", parent_telegram_id="777", role="student")

    from src.bot.pilot import cmd_mh_schedule
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_mh_schedule(upd, FakeContext(["t1", "2026-07-20T15:00:00+03:00", "60", "s1"]))

    enr = await MeritHubEnrollmentRepository("albion.db").list_by_class("C9")
    roles = {r["merithub_user_id"]: r["role"] for r in enr}
    assert roles.get("mh_t1") == "tutor" and roles.get("mh_s1") == "student"
    assert stub.last_add[0] == "C9"
    assert {"mh_t1", "mh_s1"} <= {u["userId"] for u in stub.last_add[1]}
    assert any("C9" in r for r in upd.message.replies)
