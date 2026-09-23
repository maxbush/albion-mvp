"""Workflow: перенос занятия (этап 1).

RESCHEDULE_REQUESTED — запрос родителя («перенести занятие X с даты D»,
возможно с предложенным временем) → карточка координатору с инструкцией.
Решение и исполнение — у координатора: /reschedule CLASS_ID OLD_DATE
NEW_DATE HH:MM.

LESSON_RESCHEDULED — факт переноса (override 'moved' уже записан):
уведомляет родителей/репетитора/координаторов, снимает напоминания
старого слота и ставит напоминания на новый.
"""

import json
import logging

from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.db.repository import (
    MeritHubClassRepository,
    MeritHubContactRepository,
    MeritHubEnrollmentRepository,
    MeritHubStudentRepository,
    ScheduledActionRepository,
    UserRepository,
    WorkflowRepository,
)
from src.bot.roles import get_coordinator_ids
from src.workflows.lesson_ops import (
    LessonOpsWorkflow,
    _format_class_label,
    _format_dual_time,
)

logger = logging.getLogger(__name__)


class RescheduleWorkflow:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path
        self.users = UserRepository(db_path)

    async def handle_requested(self, event: Event) -> None:
        """Запрос родителя на перенос → карточка координаторам."""
        d = event.data
        class_id, occ_date = d.get("class_id"), d.get("occurrence_date")
        if not class_id:
            return
        cls = await MeritHubClassRepository(self.db_path).get(class_id)
        label = _format_class_label(class_id, (cls or {}).get("start_time"))
        paid = " 💰 (менее чем за {} ч — оплачиваемый)".format(
            d.get("free_hours") or "") if d.get("is_paid") else ""
        proposed = d.get("proposed") or "не указано — уточнить у родителя"
        msg = (
            f"🔁 Запрос на перенос{paid}\n\n"
            f"Занятие: {label}\n"
            f"Дата: {occ_date or '—'}\n"
            f"Родитель просит: {proposed}\n\n"
            f"Исполнить: /reschedule {class_id} {occ_date} НОВАЯ_ДАТА ЧЧ:ММ"
        )
        for tg in await get_coordinator_ids(self.db_path):
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": tg, "message": msg,
            }))

    async def handle_rescheduled(self, event: Event) -> None:
        """Факт переноса: уведомления + перепланирование напоминаний."""
        d = event.data
        class_id, occ_date = d.get("class_id"), d.get("occurrence_date")
        new_date, new_time = d.get("new_date"), d.get("new_time")
        if not (class_id and occ_date and new_date):
            return
        cls = await MeritHubClassRepository(self.db_path).get(class_id)
        if not cls:
            logger.warning("Reschedule: class %s not found", class_id)
            return
        label = _format_class_label(class_id, cls.get("start_time"))
        new_iso = f"{new_date}T{new_time or (cls.get('start_time') or '')[11:16] or '00:00'}"
        new_label = _format_dual_time(new_iso)
        paid_note = " (оплачиваемый перенос)" if d.get("is_paid") else ""
        msg = (
            f"🔁 Занятие перенесено{paid_note}:\n{label}\n"
            f"с {occ_date} → {new_label}"
        )

        # Напоминания старого слота снимаем (per-occurrence workflows
        # идентифицируются class_id + датой start_time в org-каноне).
        wfs = await WorkflowRepository(self.db_path).find_by_json(
            "class_id", class_id, state="running", limit=100)
        sched = ScheduledActionRepository(self.db_path)
        repo = WorkflowRepository(self.db_path)
        for wf in wfs:
            try:
                st = (json.loads(wf.get("data") or "{}")).get("start_time") or ""
            except Exception:
                st = ""
            if st[:10] == occ_date:
                await sched.cancel_by_workflow(wf["id"])
                await repo.cancel(wf["id"])

        # Уведомления сторонам.
        enr = await MeritHubEnrollmentRepository(self.db_path).list_by_class(class_id)
        parent_tgs = list({e["parent_telegram_id"] for e in enr
                           if e.get("parent_telegram_id")})
        trow = await MeritHubContactRepository(self.db_path).get(
            cls.get("tutor_client_user_id") or "")
        tutor_tg = (trow or {}).get("telegram_id")
        tutor_name = (trow or {}).get("name") or "Репетитор"
        for tg in parent_tgs + ([tutor_tg] if tutor_tg else []):
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": tg, "message": msg,
            }))
        for tg in await get_coordinator_ids(self.db_path):
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": tg,
                "message": f"✅ Перенос исполнен:\n{msg}",
            }))

        # Перепланируем напоминания на новый слот — тот же конвейер, что
        # при создании класса (schedule_class_coordination).
        students_tz = await MeritHubStudentRepository(self.db_path).get_by_client_ids(
            [e["client_user_id"] for e in enr if e.get("client_user_id")])
        student_rows = [{
            "name": e.get("student_name") or e.get("client_user_id") or "Ученик",
            "student_name": e.get("student_name"),
            "client_user_id": e.get("client_user_id"),
            "parent_telegram_id": e.get("parent_telegram_id"),
            "timezone": (students_tz.get(e.get("client_user_id")) or {}).get("timezone"),
        } for e in enr if (e.get("role") or "student") == "student"]
        ops = LessonOpsWorkflow(self.db_path)
        await ops.schedule_class_coordination(
            class_id=class_id,
            start_time=new_iso,
            tutor_name=tutor_name,
            tutor_telegram_id=tutor_tg,
            tutor_timezone=(trow or {}).get("timezone"),
            student_rows=student_rows,
        )
        logger.info("Rescheduled %s %s → %s (reminders re-armed)",
                    class_id, occ_date, new_iso)


async def register_handlers() -> None:
    wf = RescheduleWorkflow()
    bus.subscribe(EventTypes.RESCHEDULE_REQUESTED, wf.handle_requested)
    bus.subscribe(EventTypes.LESSON_RESCHEDULED, wf.handle_rescheduled)
    logger.info("Reschedule registered")
