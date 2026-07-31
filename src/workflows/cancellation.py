"""Workflow: отмена/перенос занятия.

Уведомляет репетитора (по TG из UserRepository) и всех координаторов
(через get_coordinator_ids). Vendor Agnostic — использует фабрику.
"""

import logging

from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.db.repository import UserRepository
from src.bot.roles import get_coordinator_ids
from src.integrations.factory import get_merithub_service, get_airtable_service

logger = logging.getLogger(__name__)


class CancellationWorkflow:
    def __init__(self, db_path: str | None = None):
        self.merithub = get_merithub_service()
        self.airtable = get_airtable_service()
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
        lesson = None
        merithub_get_lesson = getattr(self.merithub, "get_lesson", None)
        if callable(merithub_get_lesson):
            try:
                lesson = await merithub_get_lesson(lid)
            except Exception as e:
                logger.warning("MeritHub get_lesson failed for %s: %s", lid, e)
        if not lesson:
            lesson = await self.airtable.get_lesson(lid)
        if not lesson:
            # Проверяем локальную БД — классы, созданные через /mh_schedule
            from src.db.repository import MeritHubClassRepository, MeritHubEnrollmentRepository, MeritHubContactRepository
            class_row = await MeritHubClassRepository(self.users.db_path).get(lid)
            if class_row:
                # Создаём минимальный lesson-like объект из локальных данных
                from types import SimpleNamespace
                tutor_cuid = class_row.get("tutor_client_user_id", "")
                # Ищем имя ученика из enrollments
                enrollments = await MeritHubEnrollmentRepository(self.users.db_path).list_by_class(lid)
                student_names = [e.get("student_name") or e.get("client_user_id", "Ученик") for e in enrollments]
                # Ищем имя репетитора из contacts
                tutor_name = tutor_cuid
                if tutor_cuid:
                    contact = await MeritHubContactRepository(self.users.db_path).get_by_client_id(tutor_cuid)
                    if contact:
                        tutor_name = contact.get("name") or tutor_cuid
                lesson = SimpleNamespace(
                    student_id=tutor_cuid,
                    tutor_id=tutor_cuid,
                    subject=class_row.get("title") or "Занятие",
                    _student_name=", ".join(student_names) if student_names else "Ученик",
                    _tutor_name=tutor_name,
                )
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
        merithub_cancel = getattr(self.merithub, "cancel_lesson", None)
        if callable(merithub_cancel):
            try:
                await merithub_cancel(lid, reason)
            except Exception as e:
                logger.warning("MeritHub cancel_lesson failed for %s: %s", lid, e)
        await self.airtable.cancel_lesson(lid, reason)

        # Отменяем запланированные действия для этого урока
        from src.db.repository import ScheduledActionRepository, WorkflowRepository
        sched = ScheduledActionRepository(self.users.db_path) if self.users.db_path else ScheduledActionRepository()
        wf_repo = WorkflowRepository(self.users.db_path) if self.users.db_path else WorkflowRepository()
        # Находим все running workflow для этого class_id
        active_wfs = await wf_repo._fetchall(
            "SELECT id FROM workflow_instances WHERE state='running' AND data LIKE ?",
            (f'%"class_id": "{lid}"%',),
        )
        for wf in active_wfs:
            await sched.cancel_by_workflow(wf["id"])
            await wf_repo.cancel(wf["id"])
            logger.info("Cancelled workflow %d for cancelled lesson %s", wf["id"], lid)

        # Для классов из MeritHubClassRepository имена уже получены из enrollments/contacts
        if hasattr(lesson, '_student_name'):
            sn = lesson._student_name
            tn = lesson._tutor_name
        else:
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
