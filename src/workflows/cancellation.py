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
        # Источник правды (R7-10): airtable (демо-уроки) или локальная БД занятий
        # (merithub_classes + зачисления + контакты). Веток get/cancel у
        # merithub-сервиса нет — вендор create-only («import & observe»).
        sn = tn = "—"
        subject = "—"
        tutor_tg = None
        reason = event.data.get("reason", "Не указана")
        lesson = await self.airtable.get_lesson(lid)
        if lesson:
            await self.airtable.cancel_lesson(lid, reason)
            student = await self.airtable.get_student(lesson.student_id)
            tutor = await self.airtable.get_tutor(lesson.tutor_id)
            sn = student.name if student else "Ученик"
            tn = tutor.name if tutor else "Репетитор"
            subject = lesson.subject
            tutor_tg = await self._get_tutor_telegram(lesson.tutor_id)
        else:
            from src.db.repository import (
                MeritHubClassRepository, MeritHubContactRepository,
                MeritHubEnrollmentRepository,
            )
            cls = await MeritHubClassRepository(self.users.db_path).get(lid)
            if not cls:
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
            enr = await MeritHubEnrollmentRepository(self.users.db_path).list_by_class(lid)
            names = [e.get("student_name") or e.get("client_user_id") or "?"
                     for e in enr if (e.get("role") or "student") == "student"]
            sn = ", ".join(names[:3]) or "Ученик"
            subject = cls.get("title") or lid
            trow = await MeritHubContactRepository(self.users.db_path).get(
                cls.get("tutor_client_user_id") or "")
            tutor_tg = (trow or {}).get("telegram_id")
            tn = (trow or {}).get("name") or "Репетитор"

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

        # Уведомляем репетитора (если есть TG) — на его языке (i18n)
        if tutor_tg:
            from src.utils.i18n import lang_of, tr
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": tutor_tg,
                "message": tr("tutor_cancelled", await lang_of(tutor_tg),
                              subject=f"{sn} — {subject}", reason=reason),
            }))

        # Уведомляем всех координаторов
        coord_ids = await get_coordinator_ids(self.users.db_path)
        for tg in coord_ids:
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": tg,
                "message": f"🔄 Отмена: {sn} + {tn}\n{subject}\n{reason}",
            }))

    async def handle_classified(self, event):
        if event.data.get("intent") not in ("cancellation", "reschedule"):
            return
        tg = event.data.get("telegram_id")

        # Персонализированные кнопки (UX-аудит П1): только занятия этого родителя,
        # occurrence-aware. Раньше показывались первые 5 классов ВСЕЙ организации.
        lessons = await upcoming_lessons_for_parent(tg, limit=5)
        buttons = [
            {"text": f"{l['student_name']} — {l['label']}"[:60],
             "callback_data": f"cancel_class:{l['class_id']}:{l['date']}"}
            for l in lessons
        ]
        if buttons:
            msg = ("Какое занятие отменяем?\n\n"
                   "Если его нет в списке — напишите координатору.")
        else:
            msg = ("Не вижу ваших ближайших занятий.\n"
                   "Чтобы отменить — напишите координатору, пожалуйста.")
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
            "telegram_id": tg,
            "message": msg,
            **({"buttons": buttons} if buttons else {}),
        }))


async def upcoming_lessons_for_parent(parent_tg: str, limit: int = 5, days: int = 14) -> list[dict]:
    """Ближайшие занятия КОНКРЕТНОГО родителя (occurrence-aware, серии развёрнуты).

    Возвращает до limit занятий (по одному ближайшему на класс):
    {class_id, date, time, label, student_name, tz}.
    Используется командой /cancel_lesson, NLU-интентом отмены и командой /lessons.
    """
    from datetime import timedelta as _td
    from src.db.repository import (
        MeritHubClassRepository,
        MeritHubEnrollmentRepository,
        MeritHubStudentRepository,
    )
    from src.utils.recurrence import (
        MONTHS_RU, WD_RU, class_occurs_on, mh_weekday, org_now,
    )

    erepo = MeritHubEnrollmentRepository()
    enrollments = await erepo._fetchall(
        "SELECT * FROM merithub_enrollments WHERE parent_telegram_id=? "
        "AND COALESCE(role,'student')='student'",
        (str(parent_tg),),
    )
    if not enrollments:
        return []

    srepo = MeritHubStudentRepository()
    crepo = MeritHubClassRepository()
    now = org_now()
    today = now.date()
    now_hhmm = now.strftime("%H:%M")
    out = []
    # R7-15: батчи вместо N+1 — классы и tz-маппинги одним запросом каждый.
    class_map = await crepo.get_many([e["class_id"] for e in enrollments])
    tz_map = await srepo.get_by_client_ids(
        [e["client_user_id"] for e in enrollments if e.get("client_user_id")])
    for class_id in sorted({e["class_id"] for e in enrollments}):
        c = class_map.get(class_id)
        if not c:
            continue
        hhmm = (c.get("start_time") or "")[11:16] or "00:00"
        for i in range(days):
            d = today + _td(days=i)
            if not class_occurs_on(c, d):
                continue
            if i == 0 and hhmm <= now_hhmm:
                continue  # уже началось/прошло
            enr = next((e for e in enrollments if e["class_id"] == class_id), {})
            name = enr.get("student_name") or enr.get("client_user_id") or "Ученик"
            tz = None
            if enr.get("client_user_id"):
                srow = tz_map.get(enr["client_user_id"])
                tz = (srow or {}).get("timezone")
            out.append({
                "class_id": class_id,
                "date": d.isoformat(),
                "time": hhmm,
                "student_name": name,
                "tz": tz,
                "label": (f"{WD_RU[mh_weekday(d)]} {d.day:02d} {MONTHS_RU[d.month]}, {hhmm}"),
            })
            break  # по одному ближайшему занятию на класс
    out.sort(key=lambda x: (x["date"], x["time"]))
    return out[:limit]


async def register_handlers():
    wf = CancellationWorkflow()
    bus.subscribe(EventTypes.LESSON_CANCELLED, wf.handle_cancelled)
    bus.subscribe(EventTypes.MESSAGE_CLASSIFIED, wf.handle_classified)
    logger.info("Cancellation registered")
