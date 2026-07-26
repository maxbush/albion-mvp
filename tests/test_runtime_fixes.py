import asyncio
from datetime import datetime, timezone, timedelta

import pytest

from src.config import settings
from src.db.repository import IncidentRepository, ScheduledActionRepository, WorkflowRepository
from src.events.bus import EventBus
from src.events.types import EventTypes
from src.workflows.engine import engine


class FakeUser:
    def __init__(self, id, username=None, full_name="Test User"):
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
async def test_claim_pending_handles_rfc3339_timestamp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db

    await init_db("albion.db")
    wid = await WorkflowRepository("albion.db").create("test", "running", {})
    aid = await ScheduledActionRepository("albion.db").create(
        wid,
        (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "notify_parent",
        {"incident_id": 1},
    )

    tasks = await ScheduledActionRepository("albion.db").claim_pending(limit=10)
    assert [t["id"] for t in tasks] == [aid]


@pytest.mark.asyncio
async def test_scheduler_marks_action_done_after_success(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    from src.scheduler.scheduler import scheduler_loop

    await init_db("albion.db")
    wid = await WorkflowRepository("albion.db").create("test", "running", {})
    aid = await ScheduledActionRepository("albion.db").create(
        wid,
        (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "notify_parent",
        {"incident_id": 1},
    )

    local_bus = EventBus()
    seen = []

    async def ok_handler(event):
        seen.append(event.data["action_id"])

    local_bus.subscribe(EventTypes.SCHEDULER_TICK, ok_handler)
    monkeypatch.setattr("src.scheduler.scheduler.bus", local_bus)

    def no_background_task(coro):
        coro.close()

        class _DummyTask:
            def cancel(self):
                return None

        return _DummyTask()

    real_create_task = asyncio.create_task
    monkeypatch.setattr("src.scheduler.scheduler.asyncio.create_task", no_background_task)

    task = real_create_task(scheduler_loop(1))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await ScheduledActionRepository("albion.db")._fetchone(
        "SELECT status, attempts FROM scheduled_actions WHERE id=?", (aid,)
    )
    assert seen == [aid]
    assert row["status"] == "done"
    assert row["attempts"] == 1


@pytest.mark.asyncio
async def test_scheduler_requeues_action_after_handler_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    from src.scheduler.scheduler import scheduler_loop

    await init_db("albion.db")
    wid = await WorkflowRepository("albion.db").create("test", "running", {})
    aid = await ScheduledActionRepository("albion.db").create(
        wid,
        (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "notify_parent",
        {"incident_id": 1},
    )

    local_bus = EventBus()

    async def bad_handler(event):
        raise RuntimeError("boom")

    local_bus.subscribe(EventTypes.SCHEDULER_TICK, bad_handler)
    monkeypatch.setattr("src.scheduler.scheduler.bus", local_bus)

    def no_background_task(coro):
        coro.close()

        class _DummyTask:
            def cancel(self):
                return None

        return _DummyTask()

    real_create_task = asyncio.create_task
    monkeypatch.setattr("src.scheduler.scheduler.asyncio.create_task", no_background_task)

    task = real_create_task(scheduler_loop(1))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await ScheduledActionRepository("albion.db")._fetchone(
        "SELECT status, attempts, last_error FROM scheduled_actions WHERE id=?", (aid,)
    )
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert "boom" in (row["last_error"] or "")


@pytest.mark.asyncio
async def test_cmd_kill_switch_admin_only(monkeypatch):
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.bot.handlers import cmd_kill_switch

    non_admin = FakeUpdate(FakeUser(200, "user"))
    await cmd_kill_switch(non_admin, FakeContext(["0"]))
    assert any("Только владелец/админ" in r for r in non_admin.message.replies)

    admin = FakeUpdate(FakeUser(100, "admin"))
    await cmd_kill_switch(admin, FakeContext(["2"]))
    assert any("Kill Switch: ВСЁ" in r for r in admin.message.replies)


@pytest.mark.asyncio
async def test_cmd_ok_uses_workflow_resolution_and_cancels_future_actions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    from src.bot.handlers import cmd_ok

    await init_db("albion.db")
    engine.repo = WorkflowRepository("albion.db")
    engine.scheduler = ScheduledActionRepository("albion.db")

    inc_id = await IncidentRepository("albion.db").create(lesson_ref="l1", type="absence", status="pending")
    wid = await engine.start_workflow("absence_notification", {"incident_id": inc_id})
    await ScheduledActionRepository("albion.db").create(
        wid,
        (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        "escalate",
        {"incident_id": inc_id},
    )

    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_ok(upd, FakeContext([str(inc_id)]))

    inc = await IncidentRepository("albion.db").get(inc_id)
    wf = await WorkflowRepository("albion.db").get(wid)
    actions = await ScheduledActionRepository("albion.db")._fetchall(
        "SELECT status FROM scheduled_actions WHERE workflow_id=?", (wid,)
    )
    assert inc["status"] == "resolved"
    assert wf["state"] == "cancelled"
    assert actions[0]["status"] == "cancelled"
    assert any("закрыта" in r for r in upd.message.replies)
