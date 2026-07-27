import pytest
from src.events.types import Event, EventTypes
from src.integrations.merithub_mock import MockMeritHubService
from src.db.repository import UserRepository
from src.workflows.cancellation import CancellationWorkflow


@pytest.mark.asyncio
async def test_cancel(db_path):
    wf = CancellationWorkflow(db_path)
    mh = MockMeritHubService()
    wf.merithub = mh
    # Создаём координатора, чтобы get_coordinator_ids нашёл кого-то
    await UserRepository(db_path).create("coord_1", "coordinator", "Координатор")
    assert (await mh.get_lesson("mh_lesson_1")).status == "scheduled"
    await wf.handle_cancelled(Event(EventTypes.LESSON_CANCELLED, {"lesson_id": "mh_lesson_1", "reason": "Болен"}))
    assert (await mh.get_lesson("mh_lesson_1")).status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_nonexistent(db_path):
    wf = CancellationWorkflow(db_path)
    await wf.handle_cancelled(Event(EventTypes.LESSON_CANCELLED, {"lesson_id": "nonexistent"}))
