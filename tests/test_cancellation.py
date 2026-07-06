import pytest
from src.events.types import Event, EventTypes
from src.integrations.merithub_mock import MockMeritHubService
from src.workflows.cancellation import CancellationWorkflow

@pytest.mark.asyncio
async def test_cancel():
    wf = CancellationWorkflow()
    mh = MockMeritHubService()
    wf.merithub = mh
    assert (await mh.get_lesson("mh_lesson_1")).status == "scheduled"
    await wf.handle_cancelled(Event(EventTypes.LESSON_CANCELLED, {"lesson_id":"mh_lesson_1","reason":"Болен"}))
    assert (await mh.get_lesson("mh_lesson_1")).status == "cancelled"

@pytest.mark.asyncio
async def test_cancel_nonexistent():
    wf = CancellationWorkflow()
    await wf.handle_cancelled(Event(EventTypes.LESSON_CANCELLED, {"lesson_id":"nonexistent"}))
