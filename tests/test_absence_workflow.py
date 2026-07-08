import pytest
from src.events.types import Event, EventTypes
from src.db.repository import IncidentRepository
from src.integrations.airtable_mock import MockAirtableService
from src.workflows.absence import AbsenceWorkflow
from src.workflows.engine import engine

@pytest.mark.asyncio
async def test_absence_creates_incident(db_path):
    wf = AbsenceWorkflow(db_path)
    await wf.handle_lesson_absent(Event(EventTypes.LESSON_ABSENT, {"lesson_id":"lesson_1","reported_by":"tutor_1"}))
    inc = await IncidentRepository(db_path).get(1)
    assert inc and inc["type"]=="absence" and inc["lesson_ref"]=="lesson_1"

@pytest.mark.asyncio
async def test_absence_unknown_lesson(db_path):
    wf = AbsenceWorkflow(db_path)
    await wf.handle_lesson_absent(Event(EventTypes.LESSON_ABSENT, {"lesson_id":"no_such_lesson"}))

@pytest.mark.asyncio
async def test_absence_marks_lesson(db_path):
    wf = AbsenceWorkflow(db_path)
    lesson = await wf.airtable.get_lesson("lesson_1")
    assert lesson.status == "scheduled"
    await wf.handle_lesson_absent(Event(EventTypes.LESSON_ABSENT, {"lesson_id":"lesson_1","reported_by":"tutor_1"}))
    assert (await wf.airtable.get_lesson("lesson_1")).status == "absent"

@pytest.mark.asyncio
async def test_resolve(db_path):
    repo = IncidentRepository(db_path)
    await repo.create(lesson_ref="l1", type="absence", status="pending")
    wf = AbsenceWorkflow(db_path)
    await wf.resolve_absence(1, "parent")
    assert (await repo.get(1))["status"] == "resolved"

@pytest.mark.asyncio
async def test_check_incident_active(db_path):
    wf = AbsenceWorkflow(db_path)
    repo = IncidentRepository(db_path)
    await repo.create(lesson_ref="l1", type="absence", status="pending")
    assert await wf._check_incident_active(1) is True
    await repo.update_status(1, "resolved")
    assert await wf._check_incident_active(1) is False


@pytest.mark.asyncio
async def test_cancel_by_workflow_marks_pending_cancelled(db_path):
    """cancel_by_workflow помечает pending actions как cancelled."""
    from src.db.repository import ScheduledActionRepository, WorkflowRepository
    from datetime import datetime, timezone, timedelta

    wf_repo = WorkflowRepository(db_path)
    wid = await wf_repo.create("test", "running", {})

    sched = ScheduledActionRepository(db_path)
    now = datetime.now(timezone.utc)
    aid1 = await sched.create(wid, (now + timedelta(minutes=5)).isoformat(), "notify_parent", {})
    aid2 = await sched.create(wid, (now + timedelta(minutes=10)).isoformat(), "escalate", {})

    await sched.cancel_by_workflow(wid)

    t1 = await sched._fetchone("SELECT status FROM scheduled_actions WHERE id=?", (aid1,))
    t2 = await sched._fetchone("SELECT status FROM scheduled_actions WHERE id=?", (aid2,))
    assert t1["status"] == "cancelled"
    assert t2["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_by_workflow_specific_action(db_path):
    """cancel_by_workflow с указанием action отменяет только конкретный action."""
    from src.db.repository import ScheduledActionRepository, WorkflowRepository
    from datetime import datetime, timezone, timedelta

    wf_repo = WorkflowRepository(db_path)
    wid = await wf_repo.create("test", "running", {})

    sched = ScheduledActionRepository(db_path)
    now = datetime.now(timezone.utc)
    aid1 = await sched.create(wid, (now + timedelta(minutes=5)).isoformat(), "notify_parent", {})
    aid2 = await sched.create(wid, (now + timedelta(minutes=10)).isoformat(), "escalate", {})

    await sched.cancel_by_workflow(wid, action="notify_parent")

    t1 = await sched._fetchone("SELECT status FROM scheduled_actions WHERE id=?", (aid1,))
    t2 = await sched._fetchone("SELECT status FROM scheduled_actions WHERE id=?", (aid2,))
    assert t1["status"] == "cancelled"
    assert t2["status"] == "pending"


@pytest.mark.asyncio
async def test_scheduler_tick_skips_cancelled_workflow(db_path):
    """handle_scheduler_tick игнорирует cancelled workflow."""
    from src.db.repository import IncidentRepository, ScheduledActionRepository, WorkflowRepository
    from src.workflows.absence import AbsenceWorkflow
    from src.events.types import Event, EventTypes

    inc_repo = IncidentRepository(db_path)
    wf_repo = WorkflowRepository(db_path)

    inc_id = await inc_repo.create(lesson_ref="l1", type="absence", status="pending")
    wid = await wf_repo.create("absence_notification", "cancelled", {"incident_id": inc_id})

    sched = ScheduledActionRepository(db_path)
    await sched.create(wid, "2020-01-01T00:00:00", "notify_parent", {"incident_id": inc_id})

    wf = AbsenceWorkflow(db_path)
    await wf.handle_scheduler_tick(Event(EventTypes.SCHEDULER_TICK, {
        "action": "notify_parent",
        "workflow_id": wid,
        "data": {"incident_id": inc_id},
    }))

    inc = await inc_repo.get(inc_id)
    assert inc["status"] == "pending"


@pytest.mark.asyncio
async def test_resolve_absence_cancels_workflow(db_path):
    """resolve_absence отменяет будущие эскалации через cancel_by_workflow."""
    from src.db.repository import IncidentRepository, ScheduledActionRepository, WorkflowRepository
    from src.workflows.absence import AbsenceWorkflow
    from datetime import datetime, timezone, timedelta

    inc_repo = IncidentRepository(db_path)
    sched = ScheduledActionRepository(db_path)
    wf_repo = WorkflowRepository(db_path)

    inc_id = await inc_repo.create(lesson_ref="l1", type="absence", status="pending")

    old_repo = engine.repo
    old_sched = engine.scheduler
    engine.repo = wf_repo
    engine.scheduler = sched
    wid = await engine.start_workflow("absence_notification", {"incident_id": inc_id})
    engine.repo = old_repo
    engine.scheduler = old_sched

    now = datetime.now(timezone.utc)
    await sched.create(wid, (now + timedelta(minutes=15)).isoformat(), "escalate", {"incident_id": inc_id})

    tasks = await sched._fetchall("SELECT * FROM scheduled_actions WHERE workflow_id=?", (wid,))
    assert len(tasks) == 1
    assert tasks[0]["status"] == "pending"

    wf = AbsenceWorkflow(db_path)
    await wf.resolve_absence(inc_id, "parent")

    inc = await inc_repo.get(inc_id)
    assert inc["status"] == "resolved"

    wf_row = await wf_repo.get(wid)
    assert wf_row["state"] == "cancelled"

    tasks2 = await sched._fetchall("SELECT * FROM scheduled_actions WHERE workflow_id=?", (wid,))
    assert tasks2[0]["status"] == "cancelled"
