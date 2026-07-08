"""Integration tests: проверяют Event Bus, DLQ, Scheduler, Engine."""

import pytest
from src.events.bus import bus, EventBus
from src.events.types import Event, EventTypes
from src.db.repository import ScheduledActionRepository, DeadLetterQueueRepository


@pytest.mark.asyncio
async def test_event_bus_handles_handler_failure():
    """Упавший хендлер не ломает шину."""
    b = EventBus()
    results = []

    async def fail(e): raise ValueError("BOOM")
    async def good(e): results.append(e)

    b.subscribe("t", fail); b.subscribe("t", good)
    await b.publish(Event("t", {"workflow_id": None}))
    assert len(results) == 1


@pytest.mark.asyncio
async def test_event_bus_wildcard():
    b = EventBus()
    caught = []
    async def all(e): caught.append(e.type)
    b.subscribe("*", all)
    await b.publish(Event("a.b", {})); await b.publish(Event("x.y", {}))
    assert caught == ["a.b", "x.y"]


@pytest.mark.asyncio
async def test_scheduler_creates_scheduled_action(db_path):
    """Workflow Engine через schedule_action создаёт запись в scheduled_actions."""
    from src.workflows.engine import engine
    from src.db.repository import WorkflowRepository, ScheduledActionRepository

    engine.repo = WorkflowRepository(db_path)
    engine.scheduler = ScheduledActionRepository(db_path)

    wid = await engine.start_workflow("test_wf", {"test": True})
    aid = await engine.schedule_action(wid, 5, "test_action", {"key": "value"})

    assert aid is not None
    row = await ScheduledActionRepository(db_path)._fetchone(
        "SELECT * FROM scheduled_actions WHERE id = ?", (aid,)
    )
    assert row is not None
    assert row["action"] == "test_action"
    assert row["status"] == "pending"
    assert row["workflow_id"] == wid


@pytest.mark.asyncio
async def test_idempotency_cleanup(db_path):
    """Idempotency cleanup работает без ошибок."""
    from src.db.repository import IdempotencyRepository
    repo = IdempotencyRepository(db_path)
    await repo.save("test_key", "test_handler")
    await repo.cleanup_old(24)
    assert True


@pytest.mark.asyncio
async def test_dead_letter_queue_write(db_path):
    """DeadLetterQueue работает с temp db."""
    dlq = DeadLetterQueueRepository(db_path)
    lid = await dlq.put("test", "test.event", {"msg": "hello"}, "test error")
    assert lid > 0
    count = await dlq.count()
    assert count >= 1


@pytest.mark.asyncio
async def test_absence_workflow_marks_incident_through_bus():
    """Проверяем что событие LESSON_ABSENT проходит через шину без краша."""
    from src.workflows.absence import register_handlers
    await register_handlers()

    # Просто проверяем что не падает
    await bus.publish(Event(EventTypes.LESSON_ABSENT, {
        "lesson_id": "lesson_1",
        "reported_by": "tutor_1",
    }))
    assert True
