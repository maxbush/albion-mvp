"""Workflow: Отсутствие на занятии → уведомление.

Фиксы:
- Проверка статуса инцидента перед каждым action (seedance fix)
- Использование SQLite scheduler
- Inline-кнопки (генерация payload для кнопок через callback_data)
"""

import json
import logging
import secrets

from src.config import settings
from src.db.repository import (
    IncidentRepository,
    NotificationRepository,
    ScheduledActionRepository,
    UserRepository,
    WorkflowRepository,
)
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.integrations.factory import get_airtable_service, get_merithub_service
from src.workflows.engine import engine

logger = logging.getLogger(__name__)


class AbsenceWorkflow:
    def __init__(self, db_path: str | None = None):
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

        # Чтение урока — только airtable (демо-данные): у merithub-сервиса
        # read-методов нет (create-only вендор, R7-10).
        lesson = await self.airtable.get_lesson(lid)
        if not lesson:
            logger.warning("Lesson %s not found", lid)
            # Честный фидбэк отправителю: /absent уже ответил «зафиксировал» —
            # без этого уведомления неизвестный урок был бы тихим no-op.
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

    async def handle_classified(self, event: Event) -> None:
        """Свободный текст «ученик не пришёл / не будет» (интент absence_report).

        Раньше такие сообщения уходили в никуда: классификатор ставил интент,
        но подписчика не было. Теперь — алерт всем координаторам с TG автора
        и исходным текстом для ручной обработки (management by exception)."""
        if event.data.get("intent") != "absence_report":
            return
        text = (event.data.get("text") or "").strip()
        tg = event.data.get("telegram_id") or "?"
        if not text:
            return
        from src.bot.roles import notify_all_coordinators
        # Автор — по имени из карточки (П9/R7-4), TG только в url-кнопке ответа.
        user = await self.users.get_by_telegram_id(str(tg))
        author = (user or {}).get("name") or "—"
        role = (user or {}).get("role") or "?"
        msg = (
            "📣 Сообщение о неявке (из чата)\n"
            f"От: {author} ({role})\n"
            f"Текст: {text[:300]}"
        )
        await notify_all_coordinators(
            msg, notification_type="absence_report", db_path=self.incidents.db_path,
            buttons=[{"text": "👤 Написать пользователю", "url": f"tg://user?id={tg}"}])
        # Отправителю — ack (R7-2): иначе репорт уходил в молчание.
        from src.utils.i18n import lang_of, tr
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
            "telegram_id": str(tg),
            "message": tr("absence_report_ack", await lang_of(str(tg))),
        }))
        logger.info("absence_report from %s forwarded to coordinators", tg)

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
        # Lesson ops actions (prelesson_*, tutor_start_check, class_live_check)
        # обрабатываются в LessonOpsWorkflow — просто пропускаем

    async def _check_incident_active(self, inc_id: int | None) -> bool:
        """Проверка: инцидент всё ещё открыт? Если resolved/escalated — пропускаем."""
        if not inc_id:
            return False
        inc = await self.incidents.get(inc_id)
        if not inc or inc["status"] in ("resolved", "escalated"):
            return False
        return True

    async def _workflow_data(self, wid: int) -> dict:
        wf = await WorkflowRepository(self.incidents.db_path).get(wid)
        return json.loads(wf["data"]) if wf and wf.get("data") else {}

    async def _class_label(self, lesson_ref: str | None) -> str:
        """Человекочитаемое имя занятия: 'C9 (28.07, 15:00)' из метаданных класса."""
        if not lesson_ref:
            return "—"
        try:
            from src.db.repository import MeritHubClassRepository
            cls = await MeritHubClassRepository(self.incidents.db_path).get(lesson_ref)
            if cls and cls.get("start_time"):
                from src.workflows.lesson_ops import _parse_dt
                dt = _parse_dt(cls["start_time"])
                return f"{lesson_ref} ({dt.strftime('%d.%m, %H:%M')})"
        except Exception:
            pass
        return lesson_ref

    # R9-1: точный поиск по JSON-полям через json_extract (LIKE-подстрока
    # матчила чужие значения: 5 vs 55). Хелпер: WorkflowRepository.find_by_json.
    async def find_active_incident_for_parent(self, parent_tg: str) -> tuple[int, dict] | None:
        """Находит активный incident для родителя по данным workflow."""
        wf_rows = await WorkflowRepository(self.incidents.db_path).find_by_json(
            "parent_telegram_id", parent_tg, state="running", limit=1)
        wf = wf_rows[0] if wf_rows else None
        if not wf:
            return None
        try:
            data = json.loads(wf.get("data") or "{}")
        except Exception:
            data = {}
        inc_id = data.get("incident_id")
        if not inc_id:
            return None
        inc = await self.incidents.get(int(inc_id))
        if not inc or inc["status"] in ("resolved", "escalated"):
            return None
        return int(inc_id), data

    async def find_escalated_incident_for_parent(self, parent_tg: str) -> tuple[int, dict] | None:
        """Находит недавно эскалированный инцидент (для позднего ответа родителя)."""
        wf_rows = await WorkflowRepository(self.incidents.db_path).find_by_json(
            "parent_telegram_id", parent_tg, limit=1)
        wf = wf_rows[0] if wf_rows else None
        if not wf:
            return None
        try:
            data = json.loads(wf.get("data") or "{}")
        except Exception:
            data = {}
        inc_id = data.get("incident_id")
        if not inc_id:
            return None
        inc = await self.incidents.get(int(inc_id))
        if not inc or inc["status"] != "escalated":
            return None
        # Не старше 2 часов (используем timezone-aware now для корректного сравнения)
        try:
            from datetime import datetime as _dt, timezone as _tz
            resolved_at = _dt.fromisoformat(inc.get("resolved_at") or "")
            now = _dt.now(_tz.utc)
            # Если resolved_at naive — считаем UTC
            if resolved_at.tzinfo is None:
                resolved_at = resolved_at.replace(tzinfo=_tz.utc)
            age_minutes = (now - resolved_at).total_seconds() / 60
            if age_minutes > 120:
                return None
        except Exception:
            pass
        return int(inc_id), data

    async def notify_coordinators_parent_reply(
        self,
        inc_id: int,
        outcome: str,
        *,
        parent_text: str | None = None,
        parent_telegram_id: str | None = None,
        late_minutes: str | None = None,
    ) -> None:
        """R9-14: late_minutes — '15'/'30+' → «ученик опоздает (на 15 мин)»."""
        labels = {
            "ok": "✅ Родитель подтвердил: всё в порядке",
            "no_show": "❌ Родитель подтвердил: сегодня занятия не будет",
            "late": "⏰ Родитель сообщил: ученик опоздает",
            "free_text": "💬 Родитель ответил свободным текстом",
        }
        inc = await self.incidents.get(inc_id)
        wf_rows = await WorkflowRepository(self.incidents.db_path).find_by_json(
            "incident_id", inc_id, limit=1)
        wf = wf_rows[0] if wf_rows else None
        wf_data = json.loads(wf["data"]) if wf and wf.get("data") else {}
        student_name = wf_data.get("student_name") or "Ученик"
        lesson_ref = (inc or {}).get("lesson_ref") or wf_data.get("lesson_ref") or "—"
        class_label = await self._class_label(lesson_ref)
        base = labels.get(outcome, "ℹ️ Родитель обновил статус")
        if outcome == "late" and late_minutes:
            base += f" (на {late_minutes} мин)"
        msg = f"{base}\nИнцидент #{inc_id}\nУченик: {student_name}\nЗанятие: {class_label}"
        if parent_text:
            msg += f"\nОтвет: {parent_text[:300]}"
        from src.bot.roles import notify_all_coordinators
        # Сырой TG в текст не пишем (П9/R7-4): действие — url-кнопкой.
        buttons = None
        if parent_telegram_id:
            buttons = [{"text": "👤 Написать родителю",
                        "url": f"tg://user?id={parent_telegram_id}"}]
        await notify_all_coordinators(
            msg, notification_type="parent_reply",
            db_path=self.incidents.db_path, buttons=buttons)

    async def _notify_parent(self, wid: int, inc_id: int | None) -> None:
        """Уведомить родителя. С проверкой статуса инцидента."""
        if not await self._check_incident_active(inc_id):
            logger.info("Incident %s already resolved, skipping notify_parent", inc_id)
            await engine.complete_workflow(wid, {"skipped": True})
            return

        inc = await self.incidents.get(inc_id)

        # Родитель/имя ученика: сначала из данных workflow (пилот / реальные данные
        # MeritHub), затем фолбэк на Airtable/MeritHub по student_id.
        wf_data = await self._workflow_data(wid)

        # R7-16: идемпотентность. Повторное выполнение того же scheduled action
        # (requeue после падения/гонки воркера) не должно дублировать сообщение
        # родителю и планировать вторую эскалацию.
        if wf_data.get("notify_parent_sent"):
            logger.info("Workflow %d: notify_parent уже отправлен — дубль пропущен", wid)
            return

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

        # Сохраняем nonce в workflow: им валидируем callback и защищаемся от
        # повторных/устаревших нажатий на inline-кнопку.
        nonce = secrets.token_hex(4)
        wf_data["parent_callback_nonce"] = nonce
        await WorkflowRepository(self.incidents.db_path).update_data(wid, wf_data)

        # Человекочитаемое название занятия
        lesson_label = await self._class_label(inc.get("lesson_ref"))

        # UX U5: варианты не дублируем текстом — их называют сами кнопки
        # (минимализм: одна мысль на сообщение, меньше чтения на мобильном).
        msg = (
            f"👋 Здравствуйте!\n\n"
            f"{student_name or 'Ученик'} отсутствовал(а) на занятии ({lesson_label}).\n"
            f"Подскажите, пожалуйста, что верно — ответьте кнопкой ниже или просто текстом."
        )
        nid = await self.notifications.create(user["id"], "absence_warning", msg)

        buttons = [
            {"text": "✅ Всё в порядке", "callback_data": f"resolve:{inc_id}:{nonce}:ok"},
            {"text": "❌ Сегодня не будет", "callback_data": f"resolve:{inc_id}:{nonce}:no"},
            {"text": "⏰ Опоздаем", "callback_data": f"resolve:{inc_id}:{nonce}:late"},
        ]

        # Публикуем запрос на отправку с несколькими кнопками и возможностью
        # свободного текстового ответа. callback_data НЕ дублируем —
        # buttons уже содержат все нужные callback_data.
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
            "notification_id": nid,
            "telegram_id": ptg,
            "message": msg,
            "incident_id": inc_id,
            "workflow_id": wid,
            "nonce": nonce,
            "buttons": buttons,
        }))

        # R7-16: флаг — ПОСЛЕ успешной публикации (requeue-тик увидит его и выйдет).
        # Если упадём между publish и флагом — возможен редкий дубль сообщения
        # с тем же nonce; это осознанно предпочтительнее потери уведомления.
        wf_data["notify_parent_sent"] = True
        await WorkflowRepository(self.incidents.db_path).update_data(wid, wf_data)

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

        # Уведомляем ВСЕХ координаторов с полным контекстом
        # (раньше была сухая строка без ученика/занятия/родителя).
        wf_data_ctx = await self._workflow_data(wid)
        inc_row = await self.incidents.get(inc_id) or {}
        student_name = wf_data_ctx.get("student_name") or "—"
        parent_tg = wf_data_ctx.get("parent_telegram_id")
        class_label = await self._class_label(
            inc_row.get("lesson_ref") or wf_data_ctx.get("lesson_ref"))
        esc_msg = (
            f"🚨 Эскалация: инцидент #{inc_id}\n"
            f"Причина: {reason}\n"
            f"Ученик: {student_name}\n"
            f"Занятие: {class_label}"
        )
        # Сырой TG в тексте не нужен — ниже url-кнопка «Написать родителю» (R7-4).
        created = inc_row.get("created_at")
        if created:
            from src.utils.recurrence import fmt_dt_org
            esc_msg += f"\nСоздан: {fmt_dt_org(created)}"

        # UX U2: действия прямо на эскалации — без ручного ввода /ok <ID>
        # (management by exception должен решаться в один тап).
        buttons = [{"text": "✅ Закрыть ситуацию", "callback_data": f"coord_resolve:{inc_id}:ok"}]
        if parent_tg:
            buttons.append({"text": "👤 Написать родителю", "url": f"tg://user?id={parent_tg}"})

        from src.bot.roles import notify_all_coordinators
        await notify_all_coordinators(
            esc_msg, notification_type="absence_escalation",
            db_path=self.incidents.db_path, buttons=buttons)

        # Сохраняем ключевые поля в result, чтобы find_* методы могли
        # найти workflow по json_extract даже после эскалации.
        wf_data = await self._workflow_data(wid)
        await engine.complete_workflow(wid, {
            **wf_data,
            "incident_id": inc_id,
            "resolution": f"escalated: {reason}",
        })
        logger.info("Incident %d escalated (%s)", inc_id, reason)

    async def resolve_absence(self, inc_id: int, by: str, resolution: str = "parent_confirmed") -> None:
        """Закрыть инцидент (через кнопку или /ok).

        Работает и для pending, и для escalated инцидентов.
        Для escalated: закрывает инцидент + уведомляет координатора о позднем ответе.
        """
        inc = await self.incidents.get(inc_id)
        if not inc:
            return
        if inc["status"] == "resolved":
            logger.info("Incident %d already resolved", inc_id)
            return
        await self.incidents.update_status(inc_id, "resolved", resolution)
        logger.info("Incident %d resolved by %s: %s (was: %s)", inc_id, by, resolution, inc["status"])
        # Отменяем будущие эскалации, если workflow активен
        wf_repo = WorkflowRepository(self.incidents.db_path)
        wf_rows = await wf_repo.find_by_json("incident_id", inc_id, limit=1)
        wf = wf_rows[0] if wf_rows else None
        if wf:
            if wf["state"] == "running":
                await ScheduledActionRepository(self.incidents.db_path).cancel_by_workflow(wf["id"])
                await wf_repo.cancel(wf["id"])
                logger.info("Cancelled running workflow %d for incident %d", wf["id"], inc_id)
            # Если workflow уже completed (после эскалации) — ничего не отменяем,
            # инцидент просто переходит в resolved.


async def register_handlers() -> None:
    wf = AbsenceWorkflow()
    bus.subscribe(EventTypes.LESSON_ABSENT, wf.handle_lesson_absent)
    bus.subscribe(EventTypes.SCHEDULER_TICK, wf.handle_scheduler_tick)
    bus.subscribe(EventTypes.MESSAGE_CLASSIFIED, wf.handle_classified)
    logger.info("Absence workflow registered")
