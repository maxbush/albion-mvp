import logging
from src.ai.client import llm_client
from src.db.repository import LeadRepository
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.integrations.airtable_mock import MockAirtableService, Lead
logger = logging.getLogger(__name__)

class LeadCaptureWorkflow:
    def __init__(self):
        self.repo = LeadRepository()
        self.airtable = MockAirtableService()

    async def handle_lead_new(self, event):
        text = event.data.get("raw_message", event.data.get("text",""))
        extracted = event.data.get("extracted_data",{})
        if not text: return
        if not extracted: extracted = await llm_client.extract_entities(text)
        lid = await self.repo.create(text, extracted)
        await self.airtable.create_lead(Lead("", text, extracted))
        subj = extracted.get("subject","не указан")
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
            "telegram_id": "coordinator_1",
            "message": f"📥 Новая заявка! #{lid}\nПредмет: {subj}\n\n{text[:150]}",
        }))
        logger.info("Lead #%d: %s", lid, subj)

    async def handle_classified(self, event):
        if event.data.get("intent") != "lead": return
        text = event.data.get("text","")
        if not text: return
        extracted = await llm_client.extract_entities(text)
        await bus.publish(Event(EventTypes.LEAD_NEW, {"raw_message": text, "telegram_id": event.data.get("telegram_id"), "extracted_data": extracted}))

async def register_handlers():
    wf = LeadCaptureWorkflow()
    bus.subscribe(EventTypes.LEAD_NEW, wf.handle_lead_new)
    bus.subscribe(EventTypes.MESSAGE_CLASSIFIED, wf.handle_classified)
    logger.info("Lead capture registered")
