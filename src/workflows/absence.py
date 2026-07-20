"""Workflow: Отсутствие на занятии → уведомление.

Фиксы:
- Проверка статуса инцидента перед каждым action (seedance fix)
- Использование SQLite scheduler
- Inline-кнопки (генерация payload для кнопок через callback_data)
"""

import logging, json

from src.config import settings
from src.db.repository import IncidentRepository, NotificationRepository, UserRepository, WorkflowRepository, ScheduledActionRepository
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.integrations.factory import get_airtable_service, get_merithub_service
from src.workflows.engine import engine

logger = logging.getLogger(__name__)


class AbsenceWorkflow:
    def __init__(self, db_path: str = "albion.db"):
        self.incidents = IncidentRepository(db_path)
        self.notifications = NotificationRepository(db_path)
        self.users = UserRepository(db_path)
        # Vendor Agnostic: реальные MeritHub/Airtable при наличии credentials, иначе mock.
        self.airtable = get_airtable_service()
        self.merithub = get_merithub_service()

    async def handle_lesson_absent(self, event: Event) -> None:
        lid = event.data.get("lesson_id")
        if not lid:
            return

        lesson = await self.merithub.get_lesson(lid) or await self.airtable.get_lesson(lid)
        if not lesson:
            logger.warning("Lesson %s not found", lid)
            return

        await self.merithub.mark_absent(lid)
        await self.airtable.mark_absent(lid, event.data.get("reported_by", ""))

        student = await self.airtable.get_student(lesson.student_id)
        if not student:
            logger.error("Student %s not found", lesson.student_id)
            return

        # Создаём инцидент
        inc_id = await self.incidents.create(
            lesson_ref=lid,
            student_id=lesson.student_id,
            tutor_id=lesson.tutor_id,
            type="absence",
            status="pending",
        )

        # Стартуем workflow
        wid = await engine.start_workflow("absence_notification", {
            "incident_id": inc_id,
            "student_id": lesson.student_id,
            "tutor_id": lesson.tutor_id,
            "student_name": student.name,
            "parent_telegram_id": student.parent_telegram_id,
            "lesson_ref": lid,
        })

        # Планируем: уведомить родителя (задержка настраивается, по умолчанию 5 мин)
        await engine.schedule_action(wid, settings.albion_notify_parent_delay_min, "notify_parent", {"incident_id": inc_id})
        logger.info("Absence: lesson=%s inc=%d wf=%d", lid, inc_id, wid)

    async def handle_scheduler_tick(self, event: Event) -> None:
        """Обрабатывает тики шедулера — notify_parent или escalate."""
        action = event.data.get("action")
        payload = event.data.get("data", {})
        inc_id = payload.get("incident_id")
        wid = event.data.get("workflow_id")

        if not action or not wid:
            return

        # Если workflow отменён — ничего не делаем
        wf = await WorkflowRepository(self.incidents.db_path).get(wid)
        if wf and wf["state"] == "cancelled":
            logger.info("Workflow %d cancelled, skipping %s", wid, action)
            return

        if action == "notify_parent":
            await self._notify_parent(wid, inc_id)
        elif action == "escalate":
            await self._escalate(wid, inc_id)
        else:
            logger.warning("Unknown action: %s", action)

    async def _check_incident_active(self, inc_id: int | None) -> bool:
        """Проверка: инцидент всё ещё открыт? Если resolved/escalated — пропускаем."""
        if not inc_id:
            return False
        inc = await self.incidents.get(inc_id)
        if not inc or inc["status"] in ("resolved", "escalated"):
            return False
        return True

    async def _notify_parent(self, wid: int, inc_id: int | None) -> None:
        """Уведомить родителя. С проверкой статуса инцидента."""
        if not await self._check_incident_active(inc_id):
            logger.info("Incident %s already resolved, skipping notify_parent", inc_id)
            await engine.complete_workflow(wid, {"skipped": True})
            return

        inc = await self.incidents.get(inc_id)

        # Родитель/имя ученика: сначала из данных workflow (пилот / реальные данные
        # MeritHub), затем фолбэк на Airtable/MeritHub по student_id.
        wf = await WorkflowRepository(self.incidents.db_path).get(wid)
        wf_data = json.loads(wf["data"]) if wf and wf.get("data") else {}
        ptg = wf_data.get("parent_telegram_id")
        student_name = wf_data.get("student_name")

        student = await self.airtable.get_student(inc.get("student_id")) if inc.get("student_id") else None
        if not ptg and student:
            ptg = student.parent_telegram_id
        if not student_name and student:
            student_name = student.name

        if not ptg:
            return await self._escalate(wid, inc_id, reason="no parent telegram")

        user = await self.users.get_by_telegram_id(ptg)
        if not user:
            return await self._escalate(wid, inc_id, reason="parent not registered")

        # Сохраняем nonce для идемпотентности кнопки
        import secrets
        nonce = secrets.token_hex(4)

        msg = (
            f"👋 Здравствуйте!\n\n"
            f"{student_name or 'Ученик'} отсутствовал(а) на занятии (ID: {inc['lesson_ref']}).\n"
            f"Всё ли в порядке?"
        )
        nid = await self.notifications.create(user["id"], "absence_warning", msg)

        # Публикуем запрос на отправку с callback_data для кнопки
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
            "notification_id": nid,
            "telegram_id": ptg,
            "message": msg,
            "incident_id": inc_id,
            "workflow_id": wid,
            "nonce": nonce,
            "callback_data": f"resolve:{inc_id}:{nonce}",
        }))

        # Планируем эскалацию (задержка настраивается, по умолчанию 15 мин)
        await engine.schedule_action(wid, settings.albion_escalate_delay_min, "escalate", {"incident_id": inc_id})
        logger.info("Parent notified for incident %d (parent=%s)", inc_id, ptg)

    async def _escalate(self, wid: int, inc_id: int | None, reason: str = "no response") -> None:
        """Эскалация координатору. С проверкой статуса."""
        if not await self._check_incident_active(inc_id):
            logger.info("Incident %s already resolved, skipping escalate", inc_id)
            await engine.complete_workflow(wid, {"skipped": True})
            return

        await self.incidents.update_status(inc_id, "escalated", reason)

        # Уведомляем ВСЕХ координаторов (реальные TG-аккаунты, назначенные /role).
        # Фолбэк на coordinator_1 — для совместимости со старым демо-сидом.
        coords = await self.users.list_by_role("coordinator")
        if not coords:
            fallback = await self.users.get_by_telegram_id("coordinator_1")
            coords = [fallback] if fallback else []
        for coord in coords:
            msg = f"🚨 Эскалация: инцидент #{inc_id} (причина: {reason})"
            nid = await self.notifications.create(coord["id"], "absence_escalation", msg)
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "notification_id": nid,
                "telegram_id": coord["telegram_id"],
                "message": msg,
                "incident_id": inc_id,
            }))

        await engine.complete_workflow(wid, {
            "incident_id": inc_id,
            "resolution": f"escalated: {reason}",
        })
        logger.info("Incident %d escalated (%s)", inc_id, reason)

    async def resolve_absence(self, inc_id: int, by: str, resolution: str = "parent_confirmed") -> None:
        """Закрыть инцидент (через кнопку или /ok)."""
        inc = await self.incidents.get(inc_id)
        if not inc:
            return
        if inc["status"] == "resolved":
            logger.info("Incident %d already resolved", inc_id)
            return
        await self.incidents.update_status(inc_id, "resolved", resolution)
        logger.info("Incident %d resolved by %s: %s", inc_id, by, resolution)
        # Отменяем будущие эскалации, если workflow активен
        wf_repo = WorkflowRepository(self.incidents.db_path)
        wf = await wf_repo._fetchone(
            "SELECT id FROM workflow_instances WHERE data LIKE ? AND state='running' ORDER BY id DESC LIMIT 1",
            (f'%"incident_id": {inc_id}%',),
        )
        if wf:
            await ScheduledActionRepository(self.incidents.db_path).cancel_by_workflow(wf["id"])
            await wf_repo.cancel(wf["id"])
            logger.info("Cancelled workflow %d for incident %d", wf["id"], inc_id)


async def register_handlers() -> None:
    wf = AbsenceWorkflow()
    bus.subscribe(EventTypes.LESSON_ABSENT, wf.handle_lesson_absent)
    bus.subscribe(EventTypes.SCHEDULER_TICK, wf.handle_scheduler_tick)
    logger.info("Absence workflow registered")
