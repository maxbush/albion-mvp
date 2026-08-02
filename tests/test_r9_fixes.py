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


# ── R9-3: webhook-режим поднимает локальный приёмник апдейтов ─────────

@pytest.mark.asyncio
async def test_r9_3_webhook_mode_starts_local_receiver(tmp_path, monkeypatch):
    """R9-3: --webhook теперь регистрирует URL И запускает updater.start_webhook
    (раньше апдейты было некому принимать — бот молчал на VPS)."""
    monkeypatch.chdir(tmp_path)
    from src.config import settings
    monkeypatch.setattr(settings, "telegram_webhook_url", "https://bot.example.com/tg/secret-path")
    monkeypatch.setattr(settings, "telegram_webhook_host", "0.0.0.0")
    monkeypatch.setattr(settings, "telegram_webhook_port", 8443)
    monkeypatch.setattr(settings, "telegram_webhook_secret", "s3cret")

    calls = {}

    class FakeBot:
        async def set_webhook(self, url=None, secret_token=None):
            calls["set_webhook"] = (url, secret_token)

    class FakeUpdater:
        async def start_webhook(self, **kw):
            calls["start_webhook"] = kw

    class FakeApp:
        bot = FakeBot()
        updater = FakeUpdater()

    from src.main import _configure_webhook
    await _configure_webhook(FakeApp())

    assert calls["set_webhook"][0] == "https://bot.example.com/tg/secret-path"
    assert calls["set_webhook"][1] == "s3cret"
    sw = calls["start_webhook"]
    assert sw["url_path"] == "/tg/secret-path"       # путь из WEBHOOK_URL
    assert sw["port"] == 8443 and sw["listen"] == "0.0.0.0"
    assert sw["secret_token"] == "s3cret"
    assert sw["allowed_updates"] == ["message", "callback_query"]
    assert sw["drop_pending_updates"] is True


@pytest.mark.asyncio
async def test_r9_3_webhook_mode_requires_url(tmp_path, monkeypatch):
    """R9-3: без TELEGRAM_WEBHOOK_URL webhook-режим падает с понятной ошибкой."""
    monkeypatch.chdir(tmp_path)
    from src.config import settings
    monkeypatch.setattr(settings, "telegram_webhook_url", None)

    from src.main import _configure_webhook
    with pytest.raises(SystemExit):
        await _configure_webhook(object())


# ── R9-4: help-карточка не обещает недоступное ────────────────────────

@pytest.mark.asyncio
async def test_r9_4_help_commands_hides_admin_section_for_coordinator(tmp_path, monkeypatch):
    """R9-4: кнопка «📋 Команды» у не-админа-координатора не показывает
    /kill_switch, /roles, /mh_* (UI не обещает то, что backend отвергает)."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    from src.config import settings
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.db.repository import UserRepository
    await UserRepository("albion.db").create("200", "coordinator", "Координатор Оля")
    await UserRepository("albion.db").create("100", "coordinator", "Админ Макс")

    from src.bot.handlers import handle_callback

    class _U:
        def __init__(self, id, full_name="X"):
            self.id = id
            self.full_name = full_name
            self.username = None

    class _M:
        def __init__(self):
            self.text = ""

    class _Q:
        def __init__(self, data, user):
            self.data = data
            self.from_user = user
            self.message = _M()
            self.answers = []
            self.edits = []

        async def answer(self, *a, **k):
            pass

        async def edit_message_text(self, text, **kw):
            self.edits.append((text, kw))

    class _C:
        def __init__(self, id):
            self.id = id

    class _Upd:
        def __init__(self, user, data):
            self.effective_user = user
            self.effective_chat = _C(42)
            self.callback_query = _Q(data, user)

    class _Ctx:
        args = []
        bot = None

    # Не-админ координатор: владельческих команд нет
    upd = _Upd(_U(200), "help_commands")
    await handle_callback(upd, _Ctx())
    text = upd.callback_query.edits[-1][0]
    assert "/kill" not in text and "/roles" not in text and "/mh_" not in text
    assert "/schedule" in text and "/incidents" in text

    # Админ: секция владельца присутствует
    upd2 = _Upd(_U(100), "help_commands")
    await handle_callback(upd2, _Ctx())
    text2 = upd2.callback_query.edits[-1][0]
    assert "/kill" in text2 and "/roles" in text2


# ── R9-6: «сегодня» в /today — org-зона, а не серверная ───────────────

def test_r9_6_org_day_utc_bounds_london_bst():
    """R9-6: London в августе (UTC+1): org-день начинается в 23:00 UTC предыдущего дня."""
    from src.utils.recurrence import org_day_utc_bounds
    from datetime import date
    start, end = org_day_utc_bounds(date(2026, 8, 2))
    assert start == "2026-08-01 23:00:00"
    assert end == "2026-08-02 23:00:00"


def test_r9_6_org_day_utc_bounds_almaty(monkeypatch):
    """R9-6: Asia/Almaty (UTC+5): org-день начинается в 19:00 UTC предыдущего дня."""
    from src.config import settings
    monkeypatch.setattr(settings, "albion_org_timezone", "Asia/Almaty")
    from src.utils.recurrence import org_day_utc_bounds
    from datetime import date
    start, end = org_day_utc_bounds(date(2026, 8, 2))
    assert start == "2026-08-01 19:00:00"
    assert end == "2026-08-02 19:00:00"


@pytest.mark.asyncio
async def test_r9_6_cmd_today_filters_incidents_by_org_day(tmp_path, monkeypatch):
    """R9-6: /today берёт инциденты org-дня: вчерашний UTC-вечер = сегодняшний
    London-день (попадает), позавчерашний — не попадает."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    from src.config import settings
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")

    # London (UTC+1 в августе). «Сегодня» = 2026-08-02 (org).
    # Инцидент вчера 23:30 UTC = сегодня 00:30 London → должен попасть.
    # Инцидент позавчера 23:30 UTC = вчера London → не должен.
    from src.db.repository import IncidentRepository, MeritHubClassRepository
    inc_repo = IncidentRepository("albion.db")
    await inc_repo._execute(
        "INSERT INTO incidents (lesson_ref, type, status, created_at) VALUES ('A','absence','pending','2026-08-01 23:30:00')")
    await inc_repo._execute(
        "INSERT INTO incidents (lesson_ref, type, status, created_at) VALUES ('B','absence','pending','2026-07-31 23:30:00')")
    # Без классов — чтобы не мешали занятия
    await MeritHubClassRepository("albion.db")._execute("DELETE FROM merithub_classes", ())

    from src.bot.pilot import cmd_today

    class _U:
        def __init__(self):
            self.id = 100

    class _M:
        def __init__(self):
            self.replies = []

        async def reply_text(self, text, **kw):
            self.replies.append((text, kw))

    class _Upd:
        def __init__(self):
            self.effective_user = _U()
            self.message = _M()

    class _Ctx:
        args = []

    # Ставим «сегодня» = 2026-08-02 London: мокаем org_now? Проще — проверить
    # что при фиксированной org-дате границы корректны (юнит выше), а здесь
    # проверяем, что команда вообще фильтрует по диапазону: вставим инциденты
    # с created_at = ровно границы org-дня «сегодня» по London.
    from datetime import date
    from src.utils.recurrence import org_day_utc_bounds
    start, end = org_day_utc_bounds(date(2026, 8, 2))
    # сдвинем «текущее» время не будем — вместо этого удалим старые и вставим
    # относительно границ (этот тест проверяет механику диапазона в cmd_today)
    await inc_repo._execute("DELETE FROM incidents", ())
    await inc_repo._execute(
        "INSERT INTO incidents (lesson_ref, type, status, created_at) VALUES ('IN','absence','pending', ?)",
        (start,))
    await inc_repo._execute(
        "INSERT INTO incidents (lesson_ref, type, status, created_at) VALUES ('OUT','absence','pending', ?)",
        ("2026-07-01 12:00:00",))

    # Мокаем org_now, чтобы «сегодня» было фиксированным
    from src.utils import recurrence as rec
    fake_now = rec.datetime(2026, 8, 2, 12, 0, tzinfo=settings.org_zone())
    monkeypatch.setattr(rec, "org_now", lambda: fake_now)

    upd = _Upd()
    await cmd_today(upd, _Ctx())
    text = upd.message.replies[0][0]
    assert "IN" not in text  # lesson_ref не выводится, но инцидент IN должен посчитаться
    assert "Инциденты сегодня: 1" in text, text
    assert "2" not in text.split("Инциденты сегодня:")[1][:5] or "Инциденты сегодня: 1" in text


# ── R9-7: attendance-webhook идемпотентен (ретраи не плодят дубли) ─────

@pytest.mark.asyncio
async def test_r9_7_trigger_absence_dedup(tmp_path, monkeypatch):
    """R9-7: повторный триггер неявки по тому же (lesson, student) с активным
    workflow — пропускается; /pilot_absent (pilot_command) не дедуплицируется."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    from src.bot.pilot import trigger_absence
    from src.db.repository import IncidentRepository

    inc1, w1 = await trigger_absence(
        lesson_ref="C9", student_id="s01", student_name="Миша",
        parent_telegram_id="777", source="merithub_attendance_webhook")
    assert inc1 is not None and w1 is not None

    # Ретрай того же webhook — dedup
    inc2, w2 = await trigger_absence(
        lesson_ref="C9", student_id="s01", student_name="Миша",
        parent_telegram_id="777", source="merithub_attendance_webhook")
    assert inc2 is None and w2 is None
    async def _count():
        r = await IncidentRepository("albion.db")._fetchone("SELECT COUNT(*) as c FROM incidents")
        return r["c"]
    assert await _count() == 1

    # Другой студент того же класса — создаётся
    inc3, w3 = await trigger_absence(
        lesson_ref="C9", student_id="s02", student_name="Катя",
        parent_telegram_id="888", source="merithub_attendance_webhook")
    assert inc3 is not None
    r = await IncidentRepository("albion.db")._fetchone("SELECT COUNT(*) as c FROM incidents")
    assert r["c"] == 2

    # pilot_command не дедуплицируется (админ-демо, сброс через /demo_reset)
    inc4, w4 = await trigger_absence(
        lesson_ref="C9", student_id="s01", student_name="Миша",
        parent_telegram_id="777", source="pilot_command")
    assert inc4 is not None
    r = await IncidentRepository("albion.db")._fetchone("SELECT COUNT(*) as c FROM incidents")
    assert r["c"] == 3


@pytest.mark.asyncio
async def test_r9_7_attendance_webhook_double_delivery_single_notification(tmp_path, monkeypatch):
    """R9-7: два attendance-webhook по одному классу → одно уведомление родителю."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    from src.db.repository import MeritHubEnrollmentRepository, UserRepository
    await UserRepository("albion.db").create("777", "parent", "Родитель")
    await MeritHubEnrollmentRepository("albion.db").add(
        "C9", "mh_s01", client_user_id="s01",
        parent_telegram_id="777", student_name="Миша", role="student")

    from src.api.webhook import _dispatch_attendance
    payload = {"classId": "C9", "attendance": []}  # никто не присутствовал
    await _dispatch_attendance(payload)
    await _dispatch_attendance(payload)  # ретрай

    from src.db.repository import IncidentRepository
    r = await IncidentRepository("albion.db")._fetchone("SELECT COUNT(*) as c FROM incidents")
    assert r["c"] == 1

    # Догоняем отложенное уведомление родителя (один scheduler-тик)
    from src.db.repository import NotificationRepository
    from src.events.bus import bus
    from src.events.types import Event, EventTypes
    from src.workflows.absence import AbsenceWorkflow
    await ScheduledActionRepository("albion.db")._execute(
        "UPDATE scheduled_actions SET execute_at=datetime('now','-1 minute') WHERE status='pending'", ())
    tasks = await ScheduledActionRepository("albion.db").claim_pending(limit=20)
    assert len(tasks) == 1, "должна быть ровно одна отложенная задача"
    for t in tasks:
        await AbsenceWorkflow("albion.db").handle_scheduler_tick(Event(EventTypes.SCHEDULER_TICK, {
            "action": t["action"], "workflow_id": t["workflow_id"],
            "data": json.loads(t["payload"]),
        }))
    rows = await NotificationRepository("albion.db")._fetchall(
        "SELECT content FROM notifications WHERE type='absence_warning'")
    assert len(rows) == 1, "родитель должен получить одно уведомление"


# ── R9-9: фантомная эмиссия удалена ───────────────────────────────────

def test_r9_9_no_phantom_event_types_published():
    """R9-9: типы без подписчиков удалены из реестра (прецедент R7-13)."""
    from src.events.types import EventTypes
    for phantom in ("notification.delivered", "notification.failed",
                    "system.kill_switch"):
        assert not hasattr(EventTypes, phantom.upper().replace(".", "_")), phantom


# ── R9-11: /incidents — батч вместо N+1 ───────────────────────────────

@pytest.mark.asyncio
async def test_r9_11_incidents_student_names_loaded_in_batch(tmp_path, monkeypatch):
    """R9-11: имена учеников в /incidents берутся батчем (та же картина на выходе)."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")
    from src.config import settings
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")

    from src.db.repository import UserRepository, MeritHubClassRepository
    await UserRepository("albion.db").create("100", "coordinator", "Босс")
    await MeritHubClassRepository("albion.db").upsert(
        "C9", title="Sofia — Physics", start_time="2099-08-03T15:00:00+00:00")

    # Три инцидента с workflow (имена Миша, Катя, без workflow)
    i1 = await IncidentRepository("albion.db").create(
        lesson_ref="C9", type="absence", status="pending")
    i2 = await IncidentRepository("albion.db").create(
        lesson_ref="C9", type="absence", status="pending")
    i3 = await IncidentRepository("albion.db").create(
        lesson_ref="C9", type="absence", status="pending")
    await engine.start_workflow("absence_notification", {
        "incident_id": i1, "student_name": "Миша", "parent_telegram_id": "1"})
    await engine.start_workflow("absence_notification", {
        "incident_id": i2, "student_name": "Катя", "parent_telegram_id": "2"})

    from src.bot.pilot import cmd_today  # noqa — убеждаемся что pilot импортируется
    from src.bot.pilot import cmd_incidents

    class _U:
        id = 100

    class _M:
        def __init__(self):
            self.replies = []

        async def reply_text(self, text, **kw):
            self.replies.append((text, kw))

    class _Upd:
        def __init__(self):
            self.effective_user = _U()
            self.message = _M()

    class _Ctx:
        args = []

    upd = _Upd()
    await cmd_incidents(upd, _Ctx())
    text = upd.message.replies[0][0]
    assert f"#{i1}" in text and f"#{i2}" in text and f"#{i3}" in text
    assert "Миша" in text and "Катя" in text
    assert "Sofia — Physics" in text or "15:00" in text  # label из карточки класса


# ── R9-14: родительское «Опоздаем» — вопрос «на сколько минут» ─────────

class _CBUser:
    def __init__(self, id, full_name="Родитель"):
        self.id = id
        self.full_name = full_name
        self.username = None


class _CBMsg:
    def __init__(self):
        self.text = ""


class _CBQuery:
    def __init__(self, data, user):
        self.data = data
        self.from_user = user
        self.message = _CBMsg()
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))

    async def edit_message_reply_markup(self, *a, **k):
        pass


class _CBUpd:
    def __init__(self, user, data, chat_id=1):
        self.effective_user = user
        self.effective_chat = _FakeChat(chat_id)
        self.callback_query = _CBQuery(data, user)


@pytest.mark.asyncio
async def test_r9_14_parent_late_asks_minutes_then_resolves(tmp_path, monkeypatch):
    """R9-14: родитель жмёт «⏰ Опоздаем» на уведомлении о неявке — бот
    спрашивает интервал; после выбора резолвит и сообщает координатору минуты."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    await UserRepository("albion.db").create("777", "parent", "Родитель")
    await UserRepository("albion.db").create("999", "coordinator", "Координатор")

    inc_id = await IncidentRepository("albion.db").create(
        lesson_ref="C9", type="absence", status="pending")
    wid = await engine.start_workflow("absence_notification", {
        "incident_id": inc_id, "parent_telegram_id": "777",
        "student_name": "Миша", "lesson_ref": "C9",
        "parent_callback_nonce": "nn1"})

    from src.bot.handlers import handle_callback
    from src.events.bus import bus
    from src.events.types import Event, EventTypes

    # Шаг 1: родитель жмёт «⏰ Опоздаем» → вопрос с кнопками интервалов
    upd1 = _CBUpd(_CBUser(777), f"resolve:{inc_id}:nn1:late")
    await handle_callback(upd1, _FakeCtx())
    assert upd1.callback_query.edits, "должен быть вопрос"
    q_text = upd1.callback_query.edits[-1][0]
    assert "На сколько минут" in q_text
    kb = upd1.callback_query.edits[-1][1].get("reply_markup")
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"resolve_late_time:{inc_id}:nn1:5" in cbs
    assert f"resolve_late_time:{inc_id}:nn1:30+" in cbs
    # Инцидент ещё НЕ закрыт (эскалация по таймеру — страховка)
    inc = await IncidentRepository("albion.db").get(inc_id)
    assert inc["status"] == "pending"

    # Шаг 2: родитель выбирает «на 15 мин» → инцидент закрыт, координатор в курсе
    captured = []
    async def cap(ev):
        captured.append(ev.data)
    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        upd2 = _CBUpd(_CBUser(777), f"resolve_late_time:{inc_id}:nn1:15")
        await handle_callback(upd2, _FakeCtx())
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    inc = await IncidentRepository("albion.db").get(inc_id)
    assert inc["status"] == "resolved"
    assert inc["resolution"] == "parent_late"
    wf = await WorkflowRepository("albion.db").get(wid)
    assert wf["state"] == "cancelled"

    coord_msgs = [d["message"] for d in captured if d.get("telegram_id") == "999"]
    assert coord_msgs
    assert "ученик опоздает (на 15 мин)" in coord_msgs[0]
    assert "Инцидент #%d" % inc_id in coord_msgs[0]

    parent_ack = upd2.callback_query.edits[-1][0]
    assert "опоздает на 15 мин" in parent_ack
    assert f"ситуация #{inc_id} закрыта" in parent_ack

    # Шаг 3: повторный тап — идемпотентно
    upd3 = _CBUpd(_CBUser(777), f"resolve_late_time:{inc_id}:nn1:15")
    await handle_callback(upd3, _FakeCtx())
    assert any("Уже обработано" in (a[0] or "") for a in upd3.callback_query.answers)
