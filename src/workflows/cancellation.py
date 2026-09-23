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
        subject_full = False  # True: subject уже содержит имена (title класса)
        tutor_tg = None
        parent_tgs: list[str] = []  # П1: родители, которых нужно уведомить об отмене
        reason = event.data.get("reason", "Не указана")
        lesson = await self.airtable.get_lesson(lid)
        if lesson:
            await self.airtable.cancel_lesson(lid, reason)
            student = await self.airtable.get_student(lesson.student_id)
            tutor = await self.airtable.get_tutor(lesson.tutor_id)
            sn = student.name if student else "Ученик"
            tn = tutor.name if tutor else "Репетитор"
            subject = lesson.subject
            subject_full = False  # предмет — короткий, префикс с именем нужен
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
            subject_full = bool(cls.get("title"))  # title уже содержит имена
            # П1: родители зачисленных учеников тоже должны узнать об отмене
            parent_tgs = list({e["parent_telegram_id"] for e in enr
                               if e.get("parent_telegram_id")})
            trow = await MeritHubContactRepository(self.users.db_path).get(
                cls.get("tutor_client_user_id") or "")
            tutor_tg = (trow or {}).get("telegram_id")
            tn = (trow or {}).get("name") or "Репетитор"

        # Этап 1: отмена ОДНОГО occurrence → schedule_overrides (маска для
        # материализации); is_paid из проверки окна на стороне кнопки.
        occ_date = event.data.get("occurrence_date")
        is_paid = bool(event.data.get("is_paid"))
        if occ_date and lid:
            from src.db.repository import ScheduleOverrideRepository
            await ScheduleOverrideRepository(self.users.db_path).add(
                lid, occ_date, "cancelled",
                reason=event.data.get("reason"),
                is_paid=is_paid,
                created_by=event.data.get("reported_by"),
            )

        # Отменяем запланированные действия для этого урока
        import json as _json
        from src.db.repository import ScheduledActionRepository, WorkflowRepository
        sched = ScheduledActionRepository(self.users.db_path) if self.users.db_path else ScheduledActionRepository()
        wf_repo = WorkflowRepository(self.users.db_path) if self.users.db_path else WorkflowRepository()
        # Находим все running workflow для этого class_id (R9-1: json_extract).
        # Для occurrence-отмены — только workflow этого слота (start_time[:10]
        # == occ_date), иначе сняли бы напоминания всей серии.
        active_wfs = await wf_repo.find_by_json(
            "class_id", lid, state="running", limit=100)
        for wf in active_wfs:
            if occ_date:
                try:
                    st = (_json.loads(wf.get("data") or "{}")).get("start_time") or ""
                except Exception:
                    st = ""
                if st[:10] != occ_date:
                    continue
            await sched.cancel_by_workflow(wf["id"])
            await wf_repo.cancel(wf["id"])
            logger.info("Cancelled workflow %d for cancelled lesson %s", wf["id"], lid)

        # П1: уведомляем родителей (если отмена инициирована не ими — сообщение
        # закрывает цикл; если родитель отменил сам — это подтверждение).
        from src.utils.i18n import lang_of, tr
        for ptg in parent_tgs:
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": ptg,
                "message": tr("class_cancelled_parent", "ru", label=subject),
            }))

        # Уведомляем репетитора (если есть TG) — на его языке (i18n).
        # Самоаудит: для merithub-классов title уже содержит имена учеников —
        # не дублируем их префиксом («Миша — Миша — математика»).
        if tutor_tg:
            subject_tutor = subject if subject_full else f"{sn} — {subject}"
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": tutor_tg,
                "message": tr("tutor_cancelled", await lang_of(tutor_tg),
                              subject=subject_tutor, reason=reason),
            }))

        # Уведомляем всех координаторов
        coord_ids = await get_coordinator_ids(self.users.db_path)
        paid_note = "💰 Платная отмена\n" if is_paid else ""
        for tg in coord_ids:
            await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                "telegram_id": tg,
                "message": f"{paid_note}🔄 Отмена: {sn} + {tn}\n{subject}\n{reason}",
            }))

    async def handle_classified(self, event):
        intent = event.data.get("intent")
        if intent not in ("cancellation", "reschedule"):
            return
        tg = event.data.get("telegram_id")

        # Персонализированные кнопки (UX-аудит П1): только занятия этого родителя,
        # occurrence-aware (с учётом overrides). Раньше показывались первые 5
        # классов ВСЕЙ организации.
        lessons = await upcoming_lessons_for_parent(tg, limit=5)
        if intent == "reschedule":
            # Перенос исполняет координатор: выбор занятия → запрос карточкой.
            buttons = [
                {"text": f"{l['student_name']} — {l['label']}"[:60],
                 "callback_data": f"resched_pick:{l['class_id']}:{l['date']}"}
                for l in lessons
            ]
            msg = (("Какое занятие перенести?\n\n"
                    "Если его нет в списке — напишите координатору.")
                   if buttons else
                   ("Не вижу ваших ближайших занятий.\n"
                    "Чтобы перенести — напишите координатору, пожалуйста."))
        else:
            buttons = [
                {"text": f"{l['student_name']} — {l['label']}"[:60],
                 "callback_data": f"cancel_class:{l['class_id']}:{l['date']}"}
                for l in lessons
            ]
            msg = (("Какое занятие отменяем?\n\n"
                    "Если его нет в списке — напишите координатору.")
                   if buttons else
                   ("Не вижу ваших ближайших занятий.\n"
                    "Чтобы отменить — напишите координатору, пожалуйста."))
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
            "telegram_id": tg,
            "message": msg,
            **({"buttons": buttons} if buttons else {}),
        }))


async def process_cancel_resched_callback(
        cb: str, actor: str, db_path: str | None = None
) -> tuple[str, list[dict] | None]:
    """Канало-независимая обработка cancel/resched-колбэков родителя.

    Зеркалит TG-обработчик в handlers.py: cancel_class → подтверждение,
    cancel_yes → LESSON_CANCELLED (is_paid считается заново на сервере),
    resched_pick → предупреждение о платном окне, resched_go → состояние
    визарда parent_resched (ждём свободный текст со временем)."""
    parts = cb.split(":")
    class_id = parts[1] if len(parts) > 1 else ""
    occ_date = parts[2] if len(parts) > 2 else ""

    if cb == "cancel_x":
        return "Хорошо, занятие остаётся в расписании 👌", None

    if cb.startswith("cancel_class:"):
        if not class_id:
            return "Не смог прочитать нажатие — попробуйте ещё раз.", None
        from src.bot.handlers import _paid_warning
        from src.db.repository import MeritHubClassRepository
        from src.workflows.lesson_ops import _format_class_label
        cls = await MeritHubClassRepository(db_path).get(class_id)
        label = _format_class_label(class_id, (cls or {}).get("start_time"))
        date_note = f" {occ_date}" if occ_date else ""
        warn = _paid_warning(cls, occ_date, "cancel")
        confirm_text = "✅ Да, отменить" + (" (платно)" if warn else "")
        return (
            f"{warn}Отменяем занятие {label}{date_note}?\n\n"
            "Репетитор и координаторы получат уведомление.",
            [{"text": confirm_text, "callback_data": f"cancel_yes:{class_id}:{occ_date}"},
             {"text": "◀️ Не надо", "callback_data": "cancel_x"}],
        )

    if cb.startswith(("resched_pick:", "resched_go:")):
        if not class_id or not occ_date:
            return "Не смог прочитать нажатие — попробуйте ещё раз.", None
        from src.bot.handlers import _paid_warning
        from src.db.repository import MeritHubClassRepository, WizardStateRepository
        cls = await MeritHubClassRepository(db_path).get(class_id)
        warn = _paid_warning(cls, occ_date, "reschedule")
        if warn and cb.startswith("resched_pick:"):
            return (
                warn + "Продолжить перенос?",
                [{"text": "✅ Всё равно перенести (платно)",
                  "callback_data": f"resched_go:{class_id}:{occ_date}"},
                 {"text": "◀️ Не надо", "callback_data": "cancel_x"}],
            )
        from src.bot.handlers import _wizard_expires_iso
        await WizardStateRepository(db_path).save(
            actor, "parent_resched", "await_time",
            {"class_id": class_id, "occurrence_date": occ_date,
             "is_paid": bool(warn), "requested_by": str(actor)},
            _wizard_expires_iso(),
        )
        return ("На какие дату и время перенести?\n\n"
                "Напишите ответом, например: «25 сентября в 16:00». "
                "Координатор подтвердит перенос."), None

    if cb.startswith("cancel_yes:"):
        if not class_id:
            return "Не смог прочитать нажатие — попробуйте ещё раз.", None
        reason = "Отмена родителем через бота"
        if occ_date:
            reason += f" (занятие {occ_date})"
        from src.bot.handlers import _cancel_policy_verdict
        from src.db.repository import MeritHubClassRepository
        from src.workflows.lesson_ops import _format_class_label
        cls = await MeritHubClassRepository(db_path).get(class_id)
        # Окно считаем в момент подтверждения — клиентской кнопке не доверяем.
        verdict = _cancel_policy_verdict(cls, occ_date, "cancel") if occ_date else None
        is_paid = bool(verdict and verdict.is_paid)
        await bus.publish(Event(EventTypes.LESSON_CANCELLED, {
            "lesson_id": class_id,
            "reason": reason,
            "occurrence_date": occ_date or None,
            "is_paid": is_paid,
            "reported_by": str(actor),
        }))
        label = _format_class_label(class_id, (cls or {}).get("start_time"))
        date_note = f" ({occ_date})" if occ_date else ""
        paid_note = "\n💰 Отмена в позднем окне — оплачиваемая." if is_paid else ""
        logger.info("Cancel via button: class=%s date=%s paid=%s by=%s",
                    class_id, occ_date, is_paid, actor)
        return f"🔄 Отмена {label}{date_note} передана репетитору и координаторам.{paid_note}", None

    return "Не понял действие — попробуйте ещё раз или напишите текстом.", None


def is_cancel_resched_callback(cb: str) -> bool:
    return cb.startswith(("cancel_class:", "cancel_yes:", "resched_pick:",
                          "resched_go:")) or cb == "cancel_x"


async def upcoming_lessons_for_parent(parent_tg: str, limit: int = 5, days: int = 14) -> list[dict]:
    """Ближайшие занятия КОНКРЕТНОГО родителя (occurrence-aware, серии развёрнуты).

    Возвращает до limit занятий (по одному ближайшему на класс):
    {class_id, date, time, label, student_name, tz}.
    Используется командой /cancel_lesson, NLU-интентом отмены и командой /lessons.
    """
    from datetime import date, timedelta as _td
    from src.db.repository import (
        MeritHubClassRepository,
        MeritHubEnrollmentRepository,
        MeritHubStudentRepository,
    )
    from src.utils.recurrence import (
        MONTHS_RU, WD_RU, mh_weekday, org_now,
    )

    from src.channels.inbound import recipient_aliases
    erepo = MeritHubEnrollmentRepository()
    refs = await recipient_aliases(str(parent_tg))
    placeholders = ",".join("?" for _ in refs)
    enrollments = await erepo._fetchall(
        f"SELECT * FROM merithub_enrollments WHERE parent_telegram_id IN ({placeholders}) "
        "AND COALESCE(role,'student')='student'",
        tuple(refs),
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
    from src.services.overrides import effective_dates
    window = [today + _td(days=i) for i in range(days)]
    for class_id in sorted({e["class_id"] for e in enrollments}):
        c = class_map.get(class_id)
        if not c:
            continue
        # effective_dates: overrides маскируют отменённые/перенесённые даты
        # и возвращают занятия, приехавшие в окно переносом (со своим временем).
        eff = await effective_dates(c, window)
        for iso in sorted(eff):
            d = date.fromisoformat(iso)
            hhmm = eff[iso] or (c.get("start_time") or "")[11:16] or "00:00"
            if iso == today.isoformat() and hhmm <= now_hhmm:
                continue  # уже началось/прошло
            enr = next((e for e in enrollments if e["class_id"] == class_id), {})
            name = enr.get("student_name") or enr.get("client_user_id") or "Ученик"
            tz = None
            if enr.get("client_user_id"):
                srow = tz_map.get(enr["client_user_id"])
                tz = (srow or {}).get("timezone")
            out.append({
                "class_id": class_id,
                "date": iso,
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
