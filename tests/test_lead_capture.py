import pytest
from src.events.types import Event, EventTypes
from src.db.repository import LeadRepository
from src.workflows.lead_capture import LeadCaptureWorkflow

@pytest.mark.asyncio
async def test_lead_created(db_path):
    wf = LeadCaptureWorkflow()
    wf.repo = LeadRepository(db_path)
    await wf.handle_lead_new(Event(EventTypes.LEAD_NEW, {"raw_message":"Нужен репетитор","extracted_data":{"subject":"math","is_lead":True}}))
    lead = await LeadRepository(db_path).get(1)
    assert lead and "репетитор" in lead["raw_message"]

@pytest.mark.asyncio
async def test_lead_empty(db_path):
    wf = LeadCaptureWorkflow()
    wf.repo = LeadRepository(db_path)
    await wf.handle_lead_new(Event(EventTypes.LEAD_NEW, {"raw_message":""}))
    assert len(await LeadRepository(db_path)._fetchall("SELECT * FROM leads")) == 0
