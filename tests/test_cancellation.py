import pytest
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.db.repository import (
    MeritHubClassRepository, MeritHubContactRepository,
    MeritHubEnrollmentRepository, UserRepository,
)
from src.workflows.cancellation import CancellationWorkflow


@pytest.mark.asyncio
async def test_cancel_airtable_lesson(db_path):
    """Демо-урок (airtable): статус меняется + координатор уведомлён."""
    wf = CancellationWorkflow(db_path)
    await UserRepository(db_path).create("coord_1", "coordinator", "Координатор")
    assert (await wf.airtable.get_lesson("lesson_1")).status == "scheduled"

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await wf.handle_cancelled(Event(EventTypes.LESSON_CANCELLED, {
            "lesson_id": "lesson_1", "reason": "Болен"}))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    assert (await wf.airtable.get_lesson("lesson_1")).status == "cancelled"
    coord = [d for d in captured if d.get("telegram_id") == "coord_1"]
    assert coord and "Отмена" in coord[0]["message"]


@pytest.mark.asyncio
async def test_cancel_db_class_notifies_tutor_and_coordinator(tmp_path, monkeypatch):
    """Реальный класс из merithub_classes: тьютор (EN) и координатор уведомлены.

    R7-10: раньше этот путь притворялся через mock-only merithub.get_lesson;
    теперь источник правды — локальная БД (класс + зачисления + контакты)."""
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    db_path = "albion.db"
    await UserRepository(db_path).create("coord_1", "coordinator", "Координатор")
    await UserRepository(db_path).create("42", "tutor", "Daniel", language="en")
    await MeritHubClassRepository(db_path).upsert(
        "C9", title="Sofia — Physics", start_time="2099-07-31T15:00:00+00:00",
        tutor_client_user_id="t01")
    await MeritHubContactRepository(db_path).upsert(
        "t01", telegram_id="42", role="tutor", name="Daniel John")
    await MeritHubEnrollmentRepository(db_path).add(
        "C9", "mh_s01", client_user_id="s01", parent_telegram_id="555", student_name="Sofia")

    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await CancellationWorkflow(db_path).handle_cancelled(Event(EventTypes.LESSON_CANCELLED, {
            "lesson_id": "C9", "reason": "Отмена родителем", "reported_by": "555"}))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    tutor = [d for d in captured if d.get("telegram_id") == "42"]
    assert tutor, "тьютор из контактов класса должен получить уведомление"
    assert "Cancellation" in tutor[0]["message"]                # EN-версия
    assert "Sofia — Physics" in tutor[0]["message"]
    coord = [d for d in captured if d.get("telegram_id") == "coord_1"]
    assert coord and "Отмена" in coord[0]["message"] and "Sofia" in coord[0]["message"]
    # Никакого ложного «не найден» отправителю:
    assert not any("не найден" in d.get("message", "") for d in captured)


@pytest.mark.asyncio
async def test_cancel_nonexistent_notifies_reporter(db_path):
    """Неизвестный ID — честный фидбэк отправителю (не тихий no-op)."""
    captured = []

    async def cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, cap)
    try:
        await CancellationWorkflow(db_path).handle_cancelled(Event(EventTypes.LESSON_CANCELLED, {
            "lesson_id": "nonexistent", "reason": "тест", "reported_by": "9"}))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, cap)

    reporter = [d for d in captured if d.get("telegram_id") == "9"]
    assert reporter and "не найден" in reporter[0]["message"]
