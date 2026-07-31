"""Workflow: отмена/перенос занятия.

Уведомляет репетитора (по TG из UserRepository) и всех координаторов
(через get_coordinator_ids). Без хардкодов.
"""

import logging

from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.db.repository import UserRepository
from src.bot.roles import get_coordinator_ids
from src.integrations.merithub_mock import MockMeritHubService
from src.integrations.airtable_mock import MockAirtableService

logger = logging.getLogger(__name__)


class CancellationWorkflow:
    def __init__(self, db_path: str | None = None):
        self.merithub = MockMeritHubService()
        self.airtable = MockAirtableService()
        self.users = UserRepository(db_path)

    async def _get_tutor_telegram(self, tutor_id: str) -> str | None:
        """Ищет TG репетитора: сначала в users (по telegram_id=tutor_id), затем фолбэк."""
        user = await self.users.get_by_telegram_id(tutor_id)
        if user:
            return user["telegram_id"]
        return None

    async def handle_cancelled(self, event):
        lid = event.data.get("lesson_id")
        if not lid:
            return
        lesson = await self.merithub.get_lesson(lid) or await self.airtable.get_lesson(lid)
        if not lesson:
            # Честный фидбэк отправителю: иначе /cancel_lesson выглядит
            # «принятой», но молча ничего не делает.
            reporter = event.data.get("reported_by")
            if reporter:
                await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                    "telegram_id": reporter,
                    "message": (
                        f"❌ Урок {lid} не найден в расписании. "
                        "Проверьте ID (/today) или напишите координатору."
                    ),
                }))
            return
        reason = event.data.get("reason", "Не указана")
        await self.merithub.cancel_lesson(lid, reason)
        await self.airtable.cancel_lesson(lid, reason)

        student = await self.airtable.get_student(lesson.student_id)
        tutor = await self.airtable.get_tutor(lesson.tutor_id)
        sn = student.name if student else "Ученик"
        tn = tutor.name if tutor else "Репетитор"

        # Уведомляем репетитора (если есть TG)
        tutor_tg = await self._get_tutor_telegram(lesson.tutor_id)
        if tutor_tg:
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": tutor_tg,
                "message": f"📅 Отмена: {sn} — {lesson.subject}\n{reason}",
            }))

        # Уведомляем всех координаторов
        coord_ids = await get_coordinator_ids(self.users.db_path)
        for tg in coord_ids:
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": tg,
                "message": f"🔄 Отмена: {sn} + {tn}\n{lesson.subject}\n{reason}",
            }))

    async def handle_classified(self, event):
        if event.data.get("intent") not in ("cancellation", "reschedule"):
            return
        tg = event.data.get("telegram_id")

        # UX U6: вместо ручного ввода ID — кнопки с реальными занятиями
        # (recognition over recall; меньше опечаток). Fallback — текстовая подсказка.
        from src.db.repository import MeritHubClassRepository
        classes = await MeritHubClassRepository(self.users.db_path).list_all()
        buttons = []
        for c in classes[:5]:  # 5 кнопок — предел комфортного выбора на мобильном
            from src.workflows.lesson_ops import _format_class_label
            buttons.append({
                "text": _format_class_label(c["class_id"], c.get("start_time"))[:40],
                "callback_data": f"cancel_class:{c['class_id']}",
            })
        if buttons:
            msg = "Какое занятие отменяем?\n\nЕсли его нет в списке — введите вручную: /cancel_lesson <ID>"
        else:
            msg = "Укажите ID урока:\n/cancel_lesson <ID>"
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
            "telegram_id": tg,
            "message": msg,
            **({"buttons": buttons} if buttons else {}),
        }))


async def register_handlers():
    wf = CancellationWorkflow()
    bus.subscribe(EventTypes.LESSON_CANCELLED, wf.handle_cancelled)
    bus.subscribe(EventTypes.MESSAGE_CLASSIFIED, wf.handle_classified)
    logger.info("Cancellation registered")
