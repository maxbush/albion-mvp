"""Этап 1, PR4: schedule_overrides, переносы, платная отмена.

- Политика окна (pure): free >= free_hours, иначе paid.
- Overrides: upsert по (class_id, occurrence_date); маска в материализации
  (effective_dates / occurrences_on_date) — общая для всех списков занятий.
- /reschedule (координатор): override + уведомления сторонам.
- Платная отмена: is_paid → override + «💰» координатору.
"""
import pytest
from datetime import date, datetime, timedelta

from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.db.repository import (
    MeritHubClassRepository, MeritHubEnrollmentRepository,
    ScheduleOverrideRepository, UserRepository,
)


async def _init_tmp_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    return "albion.db"


class FakeUser:
    def __init__(self, id, full_name="T"):
        self.id = id
        self.full_name = full_name


class FakeMessage:
    def __init__(self):
        self.replies = []

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


def _perma(class_id="C9", days=None, start="2026-01-05T15:00:00", hhmm=None):
    """perma-класс: schedule_days — коды дней MeritHub (0=вс)."""
    return {
        "class_id": class_id,
        "title": "Math — Misha",
        "start_time": hhmm and f"{start[:10]}T{hhmm}:00" or start,
        "class_type": "perma",
        "schedule_days": "[1,3]" if days is None else str(days).replace(" ", ""),
        "duration": 60,
        "tutor_client_user_id": "t1",
        "end_date": None,
    }


# ── Политика окна (pure) ────────────────────────────────────────────────

def test_policy_window_boundaries():
    from src.services.cancellation_policy import evaluate_policy
    start = datetime(2026, 10, 1, 15, 0)
    base = datetime(2026, 9, 30, 15, 0)  # ровно 24ч до
    assert evaluate_policy(start, now=base, free_hours=24).window == "free"
    paid = evaluate_policy(start, now=base + timedelta(hours=1), free_hours=24)
    assert paid.is_paid and paid.hours_left == 23.0
    # После начала занятия — тоже paid (hours_left < 0).
    assert evaluate_policy(start, now=start + timedelta(hours=1),
                           free_hours=24).is_paid


def test_paid_warning_text_kind():
    from src.services.cancellation_policy import evaluate_policy, paid_warning_text
    v = evaluate_policy(datetime(2026, 10, 1), now=datetime(2026, 9, 30, 23),
                        free_hours=24)
    assert "оплачивается" in paid_warning_text(v, "cancel")
    assert "Перенос" in paid_warning_text(v, "reschedule")
    assert paid_warning_text(
        evaluate_policy(datetime(2026, 10, 5), now=datetime(2026, 9, 30),
                        free_hours=24), "cancel") == ""


# ── Overrides: репозиторий + материализация ────────────────────────────

@pytest.mark.asyncio
async def test_override_upsert_and_queries(db_path):
    repo = ScheduleOverrideRepository(db_path)
    await repo.add("C1", "2026-10-05", "cancelled", reason="x", is_paid=True)
    # upsert той же даты: action меняется, is_paid обновляется
    await repo.add("C1", "2026-10-05", "moved", new_date="2026-10-07",
                   new_time="16:00", is_paid=False)
    row = await repo.get("C1", "2026-10-05")
    assert row["action"] == "moved" and row["new_time"] == "16:00"
    assert row["is_paid"] == 0
    assert len(await repo.list_for_class("C1")) == 1
    assert (await repo.list_on("2026-10-05"))[0]["class_id"] == "C1"
    assert (await repo.moved_to("2026-10-07"))[0]["occurrence_date"] == "2026-10-05"
    assert await repo.moved_to("2026-10-08") == []


@pytest.mark.asyncio
async def test_effective_dates_cancelled_and_moved(db_path):
    """cancelled маскирует дату; moved убирает исходную и добавляет новую."""
    from src.services.overrides import effective_dates
    cls = _perma()  # пн(1), ср(3), старт 15:00
    crepo = MeritHubClassRepository(db_path)
    await crepo.upsert(**{k: v for k, v in cls.items()})
    repo = ScheduleOverrideRepository(db_path)
    # 2026-10-05 — пн, 2026-10-07 — ср
    await repo.add("C9", "2026-10-05", "cancelled")
    await repo.add("C9", "2026-10-07", "moved", new_date="2026-10-09",
                   new_time="17:30")
    window = [date(2026, 10, 5) + timedelta(days=i) for i in range(10)]
    eff = await effective_dates(cls, window, db_path)
    assert "2026-10-05" not in eff                       # cancelled скрыт
    assert "2026-10-07" not in eff                       # moved-away скрыт
    assert eff.get("2026-10-09") == "17:30"              # moved-in со своим временем
    assert "2026-10-12" in eff and eff["2026-10-12"] is None  # обычный день


@pytest.mark.asyncio
async def test_occurrences_on_date_moved_in(db_path):
    """В дату приехавшее переносом занятие видно; отменённое — нет."""
    from src.services.overrides import occurrences_on_date
    crepo = MeritHubClassRepository(db_path)
    await crepo.upsert(**_perma("A"))
    await crepo.upsert(**_perma("B"))
    repo = ScheduleOverrideRepository(db_path)
    await repo.add("A", "2026-10-05", "cancelled")
    await repo.add("B", "2026-10-07", "moved", new_date="2026-10-05", new_time="18:00")
    pairs = await occurrences_on_date(date(2026, 10, 5), db_path)
    ids = {c["class_id"]: t for c, t in pairs}
    assert "A" not in ids            # отменено в этот день
    assert ids.get("B") == "18:00"   # приехало переносом со своим временем


# ── /reschedule (координатор) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_reschedule_command_happy_path(tmp_path, monkeypatch):
    """Координатор переносит occurrence: override + уведомления всем сторонам."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    await UserRepository(db).create("999", "coordinator", "Координатор")
    await UserRepository(db).create("555", "parent", "Мама")
    crepo = MeritHubClassRepository(db)
    await crepo.upsert(**_perma("C9"))
    await MeritHubEnrollmentRepository(db).add(
        "C9", "m1", student_name="Миша", parent_telegram_id="555")

    # Подписываем reschedule-workflow (в проде — register_all в main.py)
    # и ловим уведомления.
    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    from src.workflows.reschedule import RescheduleWorkflow
    wf = RescheduleWorkflow(db)
    bus.subscribe(EventTypes.LESSON_RESCHEDULED, wf.handle_rescheduled)
    try:
        from src.bot.handlers import cmd_reschedule
        upd = FakeUpdate(FakeUser(999))
        await cmd_reschedule(upd, FakeContext(["C9", "2026-10-05", "2026-10-08", "16:00"]))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
        bus.unsubscribe(EventTypes.LESSON_RESCHEDULED, wf.handle_rescheduled)

    row = await ScheduleOverrideRepository(db).get("C9", "2026-10-05")
    assert row and row["action"] == "moved" and row["new_date"] == "2026-10-08"
    assert "✅ Перенос исполнен" in upd.message.replies[-1]
    parent_msgs = [d for d in captured if d.get("telegram_id") == "555"]
    coord_msgs = [d for d in captured if d.get("telegram_id") == "999"]
    assert parent_msgs and "перенесено" in parent_msgs[0]["message"].lower()
    assert coord_msgs and "Перенос исполнен" in coord_msgs[0]["message"]


@pytest.mark.asyncio
async def test_reschedule_rejects_bad_inputs(tmp_path, monkeypatch):
    """Нет занятия в исходную дату / дата в прошлом / не координатор → отказ."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    await UserRepository(db).create("999", "coordinator", "Координатор")
    await UserRepository(db).create("555", "parent", "Мама")
    await MeritHubClassRepository(db).upsert(**_perma("C9"))
    from src.bot.handlers import cmd_reschedule

    upd = FakeUpdate(FakeUser(555))  # parent — нет прав
    await cmd_reschedule(upd, FakeContext(["C9", "2026-10-05", "2026-10-08", "16:00"]))
    assert "⛔" in upd.message.replies[-1]

    upd = FakeUpdate(FakeUser(999))
    await cmd_reschedule(upd, FakeContext(["C9", "2026-10-06", "2026-10-08", "16:00"]))  # вт — нет занятия
    assert "нет занятия" in upd.message.replies[-1]

    upd = FakeUpdate(FakeUser(999))
    await cmd_reschedule(upd, FakeContext(["C9", "2026-10-05", "2020-01-01", "16:00"]))
    assert "в прошлом" in upd.message.replies[-1]

    assert await ScheduleOverrideRepository(db).list_for_class("C9") == []


# ── Платная отмена ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_paid_cancel_writes_override_and_flags_coordinator(tmp_path, monkeypatch):
    """occurrence-отмена: override 'cancelled' + is_paid; координатор видит 💰."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    await UserRepository(db).create("999", "coordinator", "Координатор")
    await MeritHubClassRepository(db).upsert(**_perma("C9"))
    await MeritHubEnrollmentRepository(db).add(
        "C9", "m1", student_name="Миша", parent_telegram_id="555")

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        from src.workflows.cancellation import CancellationWorkflow
        await CancellationWorkflow(db).handle_cancelled(Event(
            EventTypes.LESSON_CANCELLED, {
                "lesson_id": "C9", "reason": "Отмена родителем (занятие 2026-10-05)",
                "occurrence_date": "2026-10-05", "is_paid": True,
                "reported_by": "555"}))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    row = await ScheduleOverrideRepository(db).get("C9", "2026-10-05")
    assert row and row["action"] == "cancelled" and row["is_paid"] == 1
    coord = [d for d in captured if d.get("telegram_id") == "999"]
    assert coord and "Платная отмена" in coord[0]["message"]


@pytest.mark.asyncio
async def test_occurrence_cancel_scopes_workflow_kill(tmp_path, monkeypatch):
    """Occurrence-отмена снимает только workflows ЭТОГО слота, не всей серии."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    import json as _json
    from src.db.repository import WorkflowRepository, ScheduledActionRepository
    await MeritHubClassRepository(db).upsert(**_perma("C9"))
    wrepo = WorkflowRepository(db)
    srepo = ScheduledActionRepository(db)
    w1 = await wrepo.create("prelesson_parent", "running",
                            {"class_id": "C9", "start_time": "2026-10-05T15:00:00"})
    w2 = await wrepo.create("prelesson_parent", "running",
                            {"class_id": "C9", "start_time": "2026-10-07T15:00:00"})
    for wid in (w1, w2):
        await srepo.create(wid, "2999-01-01T00:00:00+00:00", "x", {})

    from src.workflows.cancellation import CancellationWorkflow
    await CancellationWorkflow(db).handle_cancelled(Event(
        EventTypes.LESSON_CANCELLED, {
            "lesson_id": "C9", "reason": "x",
            "occurrence_date": "2026-10-05", "reported_by": "555"}))

    assert (await wrepo.get(w1))["state"] == "cancelled"   # слот отменён
    assert (await wrepo.get(w2))["state"] == "running"     # соседнее занятие живо


# ── Списки занятий родителя учитывают overrides ─────────────────────────

@pytest.mark.asyncio
async def test_upcoming_lessons_skips_cancelled_occurrence(tmp_path, monkeypatch):
    """Отменённая дата пропадает из списка /cancel_lesson — берётся следующая."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    from src.utils.recurrence import mh_weekday, org_now
    await MeritHubClassRepository(db).upsert(
        **_perma("C9", days=[mh_weekday(d) for d in
                             (org_now().date() + timedelta(days=i) for i in (1, 2))]))
    await MeritHubEnrollmentRepository(db).add(
        "C9", "m1", student_name="Миша", parent_telegram_id="555")
    from src.workflows.cancellation import upcoming_lessons_for_parent
    lessons = await upcoming_lessons_for_parent("555")
    assert lessons and lessons[0]["class_id"] == "C9"
    first_date = lessons[0]["date"]
    await ScheduleOverrideRepository(db).add("C9", first_date, "cancelled")
    lessons2 = await upcoming_lessons_for_parent("555")
    assert all(l["date"] != first_date for l in lessons2)


@pytest.mark.asyncio
async def test_reschedule_request_card_to_coordinators(tmp_path, monkeypatch):
    """RESCHEDULE_REQUESTED → карточка ко��рд��натору с инструкцией /reschedule."""
    db = await _init_tmp_db(tmp_path, monkeypatch)
    await UserRepository(db).create("999", "coordinator", "Координатор")
    await MeritHubClassRepository(db).upsert(**_perma("C9"))
    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        from src.workflows.reschedule import RescheduleWorkflow
        await RescheduleWorkflow(db).handle_requested(Event(
            EventTypes.RESCHEDULE_REQUESTED, {
                "class_id": "C9", "occurrence_date": "2026-10-05",
                "proposed": "завтра в 16:00", "is_paid": True,
                "free_hours": 24, "requested_by": "555"}))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    coord = [d for d in captured if d.get("telegram_id") == "999"]
    assert coord and "/reschedule C9 2026-10-05" in coord[0]["message"]
    assert "оплачиваемый" in coord[0]["message"]
