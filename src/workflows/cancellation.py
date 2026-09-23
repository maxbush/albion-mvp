"""Workflow: отмена/перенос занятия.

Включает логику платных отмен и предупреждений (правило 24 часов):
  - При отмене < 24ч: автоматическое предупреждение о платной отмене, фиксация списания
  - При отмене >= 24ч: бесплатная отмена без списания
  - Перенос занятия: регистрация запроса, инцидент, уведомление координатора
Уведомляет репетитора, родителей и координаторов.
"""

import logging
from datetime import date, datetime, time

from src.config import settings
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.db.repository import UserRepository
from src.bot.roles import get_coordinator_ids
from src.integrations.factory import get_merithub_service, get_airtable_service

logger = logging.getLogger(__name__)


def format_hours_left(hours: float) -> str:
    """Форматирует оставшиеся часы в человекочитаемую строку."""
    if hours < 0:
        return "урок уже начался"
    if hours < 1:
        mins = max(int(hours * 60), 1)
        return f"{mins} мин"
    if hours < 24:
        return f"{hours:.1f} ч" if hours != int(hours) else f"{int(hours)} ч"
    days = int(hours // 24)
    rem_h = int(hours % 24)
    return f"{days} дн {rem_h} ч" if rem_h else f"{days} дн"


async def calculate_cancellation_policy(
    lesson_id: str,
    occurrence_date: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Определяет статус отмены (платная / бесплатная по правилу 24ч) и текст предупреждения."""
    from src.db.repository import MeritHubClassRepository
    from src.utils.recurrence import org_now

    crepo = MeritHubClassRepository(db_path)
    cls = await crepo.get(lesson_id)
    now = org_now()
    notice_limit = settings.cancellation_notice_hours

    if not cls:
        return {
            "found": False,
            "is_paid": False,
            "hours_left": 999.0,
            "hours_str": "—",
            "warning": "",
            "notice_hours": notice_limit,
        }

    org_zone = settings.org_zone()
    c_start_raw = cls.get("start_time") or ""
    c_time_hhmm = c_start_raw[11:16] if len(c_start_raw) >= 16 else "12:00"
    try:
        h, m = int(c_time_hhmm[:2]), int(c_time_hhmm[3:5])
    except Exception:
        h, m = 12, 0

    if occurrence_date:
        try:
            d = date.fromisoformat(occurrence_date[:10])
            lesson_dt = datetime.combine(d, time(h, m), tzinfo=org_zone)
        except Exception:
            lesson_dt = now
    else:
        try:
            d = date.fromisoformat(c_start_raw[:10])
            lesson_dt = datetime.combine(d, time(h, m), tzinfo=org_zone)
        except Exception:
            lesson_dt = now

    hours_left = (lesson_dt - now).total_seconds() / 3600.0
    is_paid = hours_left < notice_limit
    hours_str = format_hours_left(hours_left)

    if is_paid:
        warning = (
            f"⚠️ Внимание: до урока осталось {hours_str} (менее {notice_limit} часов).\n\n"
            f"По правилам центра, при отмене менее чем за {notice_limit}ч занятие является "
            f"ПЛАТНЫМ (100% списание с баланса). Репетитор получит компенсацию за забронированное время.\n\n"
            f"Вы уверены, что хотите отменить занятие?"
        )
    else:
        warning = (
            f"ℹ️ До урока осталось {hours_str} (более {notice_limit} часов).\n\n"
            f"✅ Отмена бесплатная — списание с баланса производиться не будет.\n\n"
            f"Подтвердить отмену занятия?"
        )

    return {
        "found": True,
        "class_id": lesson_id,
        "title": cls.get("title") or lesson_id,
        "lesson_dt": lesson_dt,
        "hours_left": hours_left,
        "hours_str": hours_str,
        "is_paid": is_paid,
        "notice_hours": notice_limit,
        "warning": warning,
    }


async def calculate_reschedule_policy(
    lesson_id: str,
    occurrence_date: str | None = None,
    db_path: str | None = None,
) -> dict:
    """Проверяет условия переноса занятия (правило 24 часов)."""
    info = await calculate_cancellation_policy(lesson_id, occurrence_date, db_path)
    hours_left = info.get("hours_left", 999.0)
    notice_limit = settings.reschedule_notice_hours
    is_urgent = hours_left < notice_limit
    hours_str = info.get("hours_str", "—")
    if is_urgent:
        warning = (
            f"⚠️ Внимание: запрос на перенос менее чем за {notice_limit}ч ({hours_str}).\n"
            f"Срочный перенос возможен только по согласованию с преподавателем и координатором.\n"
            f"Если преподаватель не сможет найти другое окно, занятие может быть списано по правилам платной отмены.\n\n"
            f"Запросить перенос через координатора?"
        )
    else:
        warning = (
            f"ℹ️ До урока осталось {hours_str} (более {notice_limit} часов).\n"
            f"✅ Перенос заблаговременный. Координатор согласует новое удобное время.\n\n"
            f"Отправить запрос координатору?"
        )
    return {
        **info,
        "is_urgent": is_urgent,
        "warning": warning,
    }


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
        # (merithub_classes + зачисления + контакты).
        sn = tn = "—"
        subject = "—"
        subject_full = False
        tutor_tg = None
        parent_tgs: list[str] = []
        reason = event.data.get("reason", "Не указана")
        occ_date = event.data.get("occurrence_date")

        # Политика платной отмены: проверяем флаг в событии или вычисляем
        is_paid = event.data.get("is_paid")
        hours_str = event.data.get("hours_str")

        lesson = await self.airtable.get_lesson(lid)
        if lesson:
            await self.airtable.cancel_lesson(lid, reason)
            student = await self.airtable.get_student(lesson.student_id)
            tutor = await self.airtable.get_tutor(lesson.tutor_id)
            sn = student.name if student else "Ученик"
            tn = tutor.name if tutor else "Репетитор"
            subject = lesson.subject
            subject_full = False
            tutor_tg = await self._get_tutor_telegram(lesson.tutor_id)
            if student and student.parent_telegram_id:
                parent_tgs = [student.parent_telegram_id]
        else:
            from src.db.repository import (
                MeritHubClassRepository, MeritHubContactRepository,
                MeritHubEnrollmentRepository,
            )
            cls = await MeritHubClassRepository(self.users.db_path).get(lid)
            if not cls:
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

            if is_paid is None:
                policy = await calculate_cancellation_policy(lid, occ_date, self.users.db_path)
                is_paid = policy["is_paid"]
                hours_str = policy["hours_str"]

            enr = await MeritHubEnrollmentRepository(self.users.db_path).list_by_class(lid)
            names = [e.get("student_name") or e.get("client_user_id") or "?"
                     for e in enr if (e.get("role") or "student") == "student"]
            sn = ", ".join(names[:3]) or "Ученик"
            subject = cls.get("title") or lid
            subject_full = bool(cls.get("title"))
            parent_tgs = list({e["parent_telegram_id"] for e in enr
                               if e.get("parent_telegram_id")})
            trow = await MeritHubContactRepository(self.users.db_path).get(
                cls.get("tutor_client_user_id") or "")
            tutor_tg = (trow or {}).get("telegram_id")
            tn = (trow or {}).get("name") or "Репетитор"

        # Отменяем запланированные действия для этого урока
        from src.db.repository import ScheduledActionRepository, WorkflowRepository
        sched = ScheduledActionRepository(self.users.db_path) if self.users.db_path else ScheduledActionRepository()
        wf_repo = WorkflowRepository(self.users.db_path) if self.users.db_path else WorkflowRepository()
        active_wfs = await wf_repo.find_by_json(
            "class_id", lid, state="running", limit=100)
        for wf in active_wfs:
            await sched.cancel_by_workflow(wf["id"])
            await wf_repo.cancel(wf["id"])
            logger.info("Cancelled workflow %d for cancelled lesson %s", wf["id"], lid)

        # Уведомляем родителей с учётом статуса оплаты
        from src.utils.i18n import lang_of, tr
        for ptg in parent_tgs:
            if is_paid:
                parent_msg = (
                    f"⚠️ Занятие {subject} отменено.\n"
                    f"Поскольку отмена произведена менее чем за 24 часа, "
                    f"занятие подлежит оплате согласно правилам центра."
                )
            else:
                parent_msg = tr("class_cancelled_parent", "ru", label=subject)

            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": ptg,
                "message": parent_msg,
            }))

        # Уведомляем репетитора (на его языке)
        if tutor_tg:
            subject_tutor = subject if subject_full else f"{sn} — {subject}"
            tutor_lang = await lang_of(tutor_tg)
            if is_paid:
                if tutor_lang == "en":
                    tutor_msg = f"Cancellation: {subject_tutor}\nLate cancellation (<24h notice). Tutor compensation will be applied."
                else:
                    tutor_msg = f"Отмена: {subject_tutor}\nПоздняя отмена (<24ч). Занятие подлежит компенсации репетитору."
            else:
                tutor_msg = tr("tutor_cancelled", tutor_lang, subject=subject_tutor, reason=reason)

            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": tutor_tg,
                "message": tutor_msg,
            }))

        # Уведомляем координаторов с деталями платной/бесплатной отмены
        coord_ids = await get_coordinator_ids(self.users.db_path)
        if is_paid:
            coord_msg = (
                f"💸 ПЛАТНАЯ ОТМЕНА (<24ч): {sn} + {tn}\n"
                f"{subject}\n"
                f"До урока оставалось: {hours_str or '<24ч'}\n"
                f"Причина: {reason}\n"
                f"Статус: Списание с баланса / выплата репетитору"
            )
        else:
            coord_msg = f"🟢 Отмена (>24ч, бесплатно): {sn} + {tn}\n{subject}\n{reason}"

        for tg in coord_ids:
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": tg,
                "message": coord_msg,
            }))

    async def handle_reschedule_requested(self, event):
        """Обработка запроса на перенос занятия."""
        lid = event.data.get("lesson_id")
        occ_date = event.data.get("occurrence_date")
        reporter = event.data.get("reported_by")
        policy = await calculate_reschedule_policy(lid, occ_date, self.users.db_path)
        is_urgent = policy.get("is_urgent", False)
        hours_str = policy.get("hours_str", "—")
        title = policy.get("title") or lid

        from src.db.repository import IncidentRepository
        inc_repo = IncidentRepository(self.users.db_path)
        inc_id = await inc_repo.create(
            lesson_ref=lid,
            type="cancellation",
            status="pending",
        )

        urg_tag = "⚠️ СРОЧНЫЙ (<24ч)" if is_urgent else "🟢 Заблаговременный (>24ч)"
        coord_msg = (
            f"🔁 Запрос на перенос: {title}\n"
            f"Статус: {urg_tag} (до урока {hours_str})\n"
            f"Инициатор: {reporter or 'Родитель'}\n"
            f"Требуется согласовать новое время с репетитором."
        )

        coord_ids = await get_coordinator_ids(self.users.db_path)
        for tg in coord_ids:
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": tg,
                "message": coord_msg,
                "buttons": [
                    {"text": f"✅ Закрыть #{inc_id}", "callback_data": f"coord_resolve:{inc_id}"},
                ],
            }))

        if reporter:
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": reporter,
                "message": (
                    f"📨 Запрос на перенос занятия {title} принят координатором.\n"
                    f"Мы свяжемся с вами и репетитором для согласования нового времени."
                ),
            }))

    async def handle_classified(self, event):
        if event.data.get("intent") not in ("cancellation", "reschedule"):
            return
        tg = event.data.get("telegram_id")

        lessons = await upcoming_lessons_for_parent(tg, limit=5)
        buttons = [
            {"text": f"{l['student_name']} — {l['label']}"[:60],
             "callback_data": f"cancel_class:{l['class_id']}:{l['date']}"}
            for l in lessons
        ]
        if buttons:
            msg = ("Какое занятие отменяем/переносим?\n\n"
                   "Если его нет в списке — напишите координатору.")
        else:
            msg = ("Не вижу ваших ближайших занятий.\n"
                   "Чтобы отменить или перенести — напишите координатору, пожалуйста.")
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
            "telegram_id": tg,
            "message": msg,
            **({"buttons": buttons} if buttons else {}),
        }))


async def upcoming_lessons_for_parent(parent_tg: str, limit: int = 5, days: int = 14) -> list[dict]:
    """Ближайшие занятия КОНКРЕТНОГО родителя (occurrence-aware, серии развёрнуты)."""
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
                continue
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
            break
    out.sort(key=lambda x: (x["date"], x["time"]))
    return out[:limit]


async def register_handlers():
    wf = CancellationWorkflow()
    bus.subscribe(EventTypes.LESSON_CANCELLED, wf.handle_cancelled)
    bus.subscribe(EventTypes.LESSON_RESCHEDULE_REQUESTED, wf.handle_reschedule_requested)
    bus.subscribe(EventTypes.MESSAGE_CLASSIFIED, wf.handle_classified)
    logger.info("Cancellation and Reschedule registered")
