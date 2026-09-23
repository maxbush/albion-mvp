"""Тесты Этапа 1: Логика платных отмен, переносов и предупреждений."""

from datetime import datetime, timedelta
import pytest

from src.config import settings
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.db.repository import MeritHubClassRepository, UserRepository, MeritHubContactRepository
from src.workflows.cancellation import (
    calculate_cancellation_policy,
    calculate_reschedule_policy,
    CancellationWorkflow,
    format_hours_left,
)


def test_format_hours_left():
    assert format_hours_left(0.5) == "30 мин"
    assert format_hours_left(5.0) == "5 ч"
    assert format_hours_left(26.0) == "1 дн 2 ч"
    assert format_hours_left(-1.0) == "урок уже начался"


@pytest.mark.asyncio
async def test_policy_free_when_more_than_24h(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")

    future_start = (datetime.now(settings.org_zone()) + timedelta(hours=48)).isoformat()
    await MeritHubClassRepository("albion.db").upsert(
        "C_free", title="Maths", start_time=future_start
    )

    policy = await calculate_cancellation_policy("C_free", db_path="albion.db")
    assert policy["found"] is True
    assert policy["is_paid"] is False
    assert "бесплатная" in policy["warning"]


@pytest.mark.asyncio
async def test_policy_paid_when_less_than_24h(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")

    near_start = (datetime.now(settings.org_zone()) + timedelta(hours=5)).isoformat()
    await MeritHubClassRepository("albion.db").upsert(
        "C_paid", title="Physics", start_time=near_start
    )

    policy = await calculate_cancellation_policy("C_paid", db_path="albion.db")
    assert policy["found"] is True
    assert policy["is_paid"] is True
    assert "ПЛАТНЫМ" in policy["warning"] or "платной" in policy["warning"]


@pytest.mark.asyncio
async def test_reschedule_policy_urgent_vs_advance(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")

    near = (datetime.now(settings.org_zone()) + timedelta(hours=3)).isoformat()
    await MeritHubClassRepository("albion.db").upsert("C_near", title="Chemistry", start_time=near)
    pol_near = await calculate_reschedule_policy("C_near", db_path="albion.db")
    assert pol_near["is_urgent"] is True
    assert "Срочный перенос" in pol_near["warning"]

    far = (datetime.now(settings.org_zone()) + timedelta(hours=72)).isoformat()
    await MeritHubClassRepository("albion.db").upsert("C_far", title="Biology", start_time=far)
    pol_far = await calculate_reschedule_policy("C_far", db_path="albion.db")
    assert pol_far["is_urgent"] is False
    assert "заблаговременный" in pol_far["warning"]


@pytest.mark.asyncio
async def test_workflow_paid_cancellation_notifications(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")

    # Репетитор и координатор
    await UserRepository("albion.db").create("coord_1", "coordinator", "Координатор")
    await UserRepository("albion.db").create("tutor_tg", "tutor", "Alex Tutor")
    await MeritHubContactRepository("albion.db").upsert("t_cuid", telegram_id="tutor_tg", role="tutor")

    # Урок через 2 часа (<24ч)
    near = (datetime.now(settings.org_zone()) + timedelta(hours=2)).isoformat()
    await MeritHubClassRepository("albion.db").upsert(
        "C_paid_test", title="English", start_time=near, tutor_client_user_id="t_cuid"
    )

    captured = []

    async def _cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, _cap)
    try:
        wf = CancellationWorkflow("albion.db")
        await wf.handle_cancelled(Event(EventTypes.LESSON_CANCELLED, {
            "lesson_id": "C_paid_test",
            "reason": "Заболел",
            "reported_by": "parent_1",
        }))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, _cap)

    coord_msgs = [d for d in captured if d.get("telegram_id") == "coord_1"]
    assert len(coord_msgs) == 1
    assert "ПЛАТНАЯ ОТМЕНА" in coord_msgs[0]["message"]

    tutor_msgs = [d for d in captured if d.get("telegram_id") == "tutor_tg"]
    assert len(tutor_msgs) == 1
    assert "компенсации" in tutor_msgs[0]["message"] or "сохранена" in tutor_msgs[0]["message"]


@pytest.mark.asyncio
async def test_reschedule_requested_flow(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")

    await UserRepository("albion.db").create("coord_2", "coordinator", "Координатор 2")
    future = (datetime.now(settings.org_zone()) + timedelta(hours=50)).isoformat()
    await MeritHubClassRepository("albion.db").upsert("C_resched", title="History", start_time=future)

    captured = []

    async def _cap(ev):
        captured.append(ev.data)

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, _cap)
    try:
        wf = CancellationWorkflow("albion.db")
        await wf.handle_reschedule_requested(Event(EventTypes.LESSON_RESCHEDULE_REQUESTED, {
            "lesson_id": "C_resched",
            "reported_by": "parent_42",
        }))
    finally:
        bus.unsubscribe(EventTypes.NOTIFICATION_REQUESTED, _cap)

    # Координатор получил алерт с кнопками
    coord = [d for d in captured if d.get("telegram_id") == "coord_2"]
    assert len(coord) == 1
    assert "Запрос на перенос" in coord[0]["message"]
    assert coord[0].get("buttons") is not None

    # Родитель получил подтверждение
    parent = [d for d in captured if d.get("telegram_id") == "parent_42"]
    assert len(parent) == 1
    assert "принят координатором" in parent[0]["message"]
