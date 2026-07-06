import logging
from datetime import datetime
from src.db.repository import NotificationRepository
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.integrations.merithub_mock import MockMeritHubService
from src.integrations.airtable_mock import MockAirtableService
logger = logging.getLogger(__name__)

class CancellationWorkflow:
    def __init__(self):
        self.merithub = MockMeritHubService()
        self.airtable = MockAirtableService()

    async def handle_cancelled(self, event):
        lid = event.data.get("lesson_id")
        if not lid: return
        lesson = await self.merithub.get_lesson(lid) or await self.airtable.get_lesson(lid)
        if not lesson: return
        reason = event.data.get("reason","Не указана")
        await self.merithub.cancel_lesson(lid, reason)
        await self.airtable.cancel_lesson(lid, reason)
        student = await self.airtable.get_student(lesson.student_id)
        tutor = await self.airtable.get_tutor(lesson.tutor_id)
        sn = student.name if student else "Ученик"
        tn = tutor.name if tutor else "Репетитор"
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {"telegram_id":"222222","message":f"📅 Отмена: {sn} — {lesson.subject}\n{reason}"}))
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {"telegram_id":"coordinator_1","message":f"🔄 Отмена: {sn} + {tn}\n{lesson.subject}\n{reason}"}))

    async def handle_classified(self, event):
        if event.data.get("intent") not in ("cancellation","reschedule"): return
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {"telegram_id": event.data.get("telegram_id"), "message": "Укажите ID урока:\n/cancel_lesson <ID>"}))

async def register_handlers():
    wf = CancellationWorkflow()
    bus.subscribe(EventTypes.LESSON_CANCELLED, wf.handle_cancelled)
    bus.subscribe(EventTypes.MESSAGE_CLASSIFIED, wf.handle_classified)
    logger.info("Cancellation registered")
