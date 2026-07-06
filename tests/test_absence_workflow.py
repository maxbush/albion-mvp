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
