"""Pre-lesson coordination workflows for parent/tutor/coordinator."""

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from src.config import settings
from src.db.repository import (
    MeritHubClassStatusRepository,
    NotificationRepository,
    ScheduledActionRepository,
    UserRepository,
    WorkflowRepository,
)
from src.events.bus import bus
from src.events.types import Event, EventTypes

logger = logging.getLogger(__name__)


def _parse_dt(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def _schedule_at(target: datetime, fallback_seconds: int = 5) -> str:
    now = datetime.now(timezone.utc)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    if target <= now:
        return (now + timedelta(seconds=fallback_seconds)).isoformat()
    return target.astimezone(timezone.utc).isoformat()


def _format_class_label(class_id: str, start_time: str | None = None) -> str:
    """Человекочитаемое название занятия: 'C9 (28.07, 15:00)' вместо голого 'C9'."""
    time_part = ""
    if start_time:
        try:
            dt = _parse_dt(start_time)
            time_part = f" — {dt.strftime('%d.%m, %H:%M')}"
        except Exception:
            pass
    return f"{class_id}{time_part}"


def _format_dual_time(start_time: str, user_tz: str | None = None) -> str:
    """Форматирует время в dual-timezone формате.

    ALBION хранит/создаёт занятия в Europe/London.
    Показываем: '15:00 (London)' или '15:00 (London) / 20:00 (ваше время, Asia/Almaty)'.
    """
    try:
        from zoneinfo import ZoneInfo
        dt = _parse_dt(start_time)
        london_tz = ZoneInfo("Europe/London")
        london_time = dt.astimezone(london_tz)
        result = london_time.strftime("%H:%M") + " (London)"

        if user_tz and user_tz != "Europe/London":
            try:
                user_zone = ZoneInfo(user_tz)
                user_time = dt.astimezone(user_zone)
                result += f" / {user_time.strftime('%H:%M')} (ваше время, {user_tz})"
                # Добавляем "через N часов" если есть разница
                diff_hours = int((user_time - london_time).total_seconds() / 3600)
                if abs(diff_hours) > 0:
                    sign = "+" if diff_hours > 0 else ""
                    result += f" [{sign}{diff_hours}ч к London]"
            except Exception:
                pass
        return result
    except Exception:
        return start_time[:16] if start_time else "—"


class LessonOpsWorkflow:
    def __init__(self, db_path: str | None = None):
        self.repo = WorkflowRepository(db_path)
        self.scheduler = ScheduledActionRepository(db_path)
        self.users = UserRepository(db_path)
        self.notifications = NotificationRepository(db_path)
        self.class_status = MeritHubClassStatusRepository(db_path)

    async def schedule_class_coordination(
        self,
        *,
        class_id: str,
        start_time: str,
        tutor_name: str,
        tutor_telegram_id: str | None,
        tutor_timezone: str | None = None,
        student_rows: list[dict],
    ) -> None:
        start_dt = _parse_dt(start_time)
        reminder_dt = start_dt - timedelta(minutes=settings.albion_prelesson_reminder_min)
        live_check_dt = start_dt + timedelta(minutes=settings.albion_class_live_grace_min)

        # Parent-side reminders: один workflow на каждого ученика/родителя.
        for student in student_rows:
            parent_tg = student.get("parent_telegram_id")
            if not parent_tg:
                continue
            data = {
                "class_id": class_id,
                "actor_type": "parent",
                "actor_telegram_id": parent_tg,
                "student_name": student.get("name") or student.get("student_name") or "Ученик",
                "student_client_user_id": student.get("client_user_id"),
                "tutor_name": tutor_name,
                "start_time": start_time,
                # Timezone ученика — для dual-time display в напоминаниях
                "actor_timezone": student.get("timezone") or "Europe/London",
            }
            wid = await self.repo.create("prelesson_parent", "running", data)
            await self.scheduler.create(wid, _schedule_at(reminder_dt), "parent_prelesson_reminder", {"workflow_id": wid})
            await self.scheduler.create(wid, _schedule_at(start_dt, 30), "parent_prelesson_no_reply", {"workflow_id": wid})

        # Tutor-side prelesson reminder.
        if tutor_telegram_id:
            student_names = [s.get("name") or s.get("student_name") or s.get("client_user_id") or "Ученик" for s in student_rows]
            tutor_tz = tutor_timezone or "Europe/London"
            tutor_data = {
                "class_id": class_id,
                "actor_type": "tutor",
                "actor_telegram_id": tutor_telegram_id,
                "tutor_name": tutor_name,
                "student_names": student_names,
                "student_count": len(student_names),
                "start_time": start_time,
                "actor_timezone": tutor_tz,
            }
            wid = await self.repo.create("prelesson_tutor", "running", tutor_data)
            await self.scheduler.create(wid, _schedule_at(reminder_dt), "tutor_prelesson_reminder", {"workflow_id": wid})
            await self.scheduler.create(wid, _schedule_at(start_dt, 45), "tutor_prelesson_no_reply", {"workflow_id": wid})

            start_data = {
                "class_id": class_id,
                "actor_type": "tutor_start",
                "actor_telegram_id": tutor_telegram_id,
                "tutor_name": tutor_name,
                "student_names": student_names,
                "student_count": len(student_names),
                "start_time": start_time,
                "actor_timezone": tutor_tz,
                "student_client_user_id": student_rows[0].get("client_user_id") if len(student_rows) == 1 else None,
                "parent_telegram_id": student_rows[0].get("parent_telegram_id") if len(student_rows) == 1 else None,
            }
            start_wid = await self.repo.create("tutor_start_check", "running", start_data)
            await self.scheduler.create(start_wid, _schedule_at(start_dt, 60), "tutor_start_check", {"workflow_id": start_wid})
            await self.scheduler.create(start_wid, _schedule_at(live_check_dt, 90), "class_live_check", {"workflow_id": start_wid})

    async def _load_workflow(self, wid: int) -> tuple[dict | None, dict]:
        wf = await self.repo.get(wid)
        if not wf:
            return None, {}
        try:
            data = json.loads(wf.get("data") or "{}")
        except Exception:
            data = {}
        return wf, data

    async def _save_workflow(self, wid: int, state: str, data: dict) -> None:
        await self.repo.update_state(wid, state, data)

    async def _cancel_future_actions(self, wid: int) -> None:
        await self.scheduler.cancel_by_workflow(wid)

    async def find_active_checkin(self, actor_tg: str, actor_types: tuple[str, ...]) -> tuple[int, dict, str] | None:
        for wf_type in ("tutor_start_check", "prelesson_parent", "prelesson_tutor"):
            wf = await self.repo._fetchone(
                "SELECT * FROM workflow_instances WHERE workflow_type=? AND state='running' AND data LIKE ? ORDER BY id DESC LIMIT 1",
                (wf_type, f'%"actor_telegram_id": "{actor_tg}"%'),
            )
            if not wf:
                continue
            try:
                data = json.loads(wf.get("data") or "{}")
            except Exception:
                data = {}
            if data.get("actor_type") in actor_types and data.get("nonce"):
                return wf["id"], data, wf_type
        return None

    async def notify_coordinators(self, title: str, lines: list[str]) -> None:
        msg = title + "\n" + "\n".join(lines)
        from src.bot.roles import notify_all_coordinators
        await notify_all_coordinators(msg, notification_type="ops_alert", db_path=self.repo.db_path)

    async def record_checkin_response(
        self,
        wid: int,
        *,
        actor_tg: str,
        action: str,
        free_text: str | None = None,
    ) -> None:
        wf, data = await self._load_workflow(wid)
        if not wf or wf["state"] != "running":
            return
        data["response_status"] = action
        data["responded_at"] = datetime.now(timezone.utc).isoformat()
        if free_text:
            data["response_text"] = free_text
        actor_type = data.get("actor_type")
        class_id = data.get("class_id", "—")
        student_name = data.get("student_name") or ", ".join(data.get("student_names") or []) or "Ученик"
        tutor_name = data.get("tutor_name") or "Репетитор"

        # Уведомляем координатора о ЛЮБОМ ответе
        action_labels = {
            "ready": "✅ Подтвердил",
            "late": "⏰ Опоздает",
            "no_show": "❌ Не будет",
            "tech": "🛠 Техпроблема",
            "other": "💬 Другое",
        }
        status_label = action_labels.get(action, action)

        if actor_type == "parent":
            await self.notify_coordinators(
                "📣 Ответ родителя",
                [
                    f"Занятие: {_format_class_label(class_id, data.get('start_time'))}",
                    f"Ученик: {student_name}",
                    f"Репетитор: {tutor_name}",
                    f"Статус: {status_label}",
                    *( [f"Текст: {free_text[:300]}"] if free_text else [] ),
                ],
            )
        elif actor_type == "tutor":
            await self.notify_coordinators(
                "🧑‍🏫 Ответ репетитора",
                [
                    f"Занятие: {_format_class_label(class_id, data.get('start_time'))}",
                    f"Репетитор: {tutor_name}",
                    f"Ученики: {student_name}",
                    f"Статус: {status_label}",
                    *( [f"Текст: {free_text[:300]}"] if free_text else [] ),
                ],
            )
        elif actor_type == "tutor_start":
            if action == "class_started":
                # Уведомляем родителя что урок начался
                parent_tg = data.get("parent_telegram_id")
                if parent_tg:
                    class_label = _format_class_label(class_id, data.get('start_time'))
                    await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                        "telegram_id": parent_tg,
                        "message": (
                            f"🔔 Урок начался!\n"
                            f"📚 {class_label}\n"
                            f"🧑‍🏫 Репетитор: {tutor_name}\n"
                            f"👤 Ученик: {student_name}"
                        ),
                    }))
                await self.notify_coordinators(
                    "👍 Урок начался",
                    [
                        f"Занятие: {_format_class_label(class_id, data.get('start_time'))}",
                        f"Репетитор: {tutor_name}",
                        f"Ученики: {student_name}",
                        f"Статус: {status_label}",
                    ],
                )
            elif action == "student_absent":
                student_names = data.get("student_names") or []
                if len(student_names) == 1 and data.get("parent_telegram_id"):
                    # Автоматический absence-flow только когда ученик один и есть TG родителя.
                    from src.bot.pilot import trigger_absence
                    await trigger_absence(
                        lesson_ref=class_id,
                        student_id=data.get("student_client_user_id") or class_id,
                        student_name=student_names[0],
                        parent_telegram_id=data["parent_telegram_id"],
                        tutor_id=actor_tg,
                        source="tutor_start_check",
                    )
                await self.notify_coordinators(
                    "👤 Репетитор отметил отсутствие ученика",
                    [
                        f"Занятие: {_format_class_label(class_id, data.get('start_time'))}",
                        f"Репетитор: {tutor_name}",
                        f"Ученики: {student_name}",
                    ],
                )
            elif action == "tech":
                await self.notify_coordinators(
                    "🛠 Проблема на старте урока",
                    [f"Занятие: {_format_class_label(class_id, data.get('start_time'))}", f"Репетитор: {tutor_name}", f"Ученики: {student_name}"],
                )
            elif action == "other":
                await self.notify_coordinators(
                    "💬 Ответ репетитора на старте урока (требует внимания)",
                    [
                        f"Занятие: {_format_class_label(class_id, data.get('start_time'))}",
                        f"Репетитор: {tutor_name}",
                        f"Ученики: {student_name}",
                        *( [f"Текст: {free_text[:300]}"] if free_text else [] ),
                    ],
                )

        await self._cancel_future_actions(wid)
        await self._save_workflow(wid, "completed", data)

    async def _send_parent_prelesson_reminder(self, wid: int) -> None:
        wf, data = await self._load_workflow(wid)
        if not wf or wf["state"] != "running":
            return
        nonce = data.get("nonce") or secrets.token_hex(4)
        data["nonce"] = nonce
        await self.repo.update_data(wid, data)
        # Dual timezone display: London + пользовательский
        start_time = data.get("start_time", "")
        user_tz = data.get("actor_timezone")
        time_display = _format_dual_time(start_time, user_tz) if start_time else ""
        time_line = f"\n🕐 Время: {time_display}" if time_display else ""
        msg = (
            f"⏰ Напоминание: через {settings.albion_prelesson_reminder_min} мин занятие.{time_line}\n"
            f"Ученик: {data.get('student_name', 'Ученик')}\n"
            f"Репетитор: {data.get('tutor_name', 'Репетитор')}\n\n"
            f"Подтвердите, пожалуйста, статус или ответьте текстом."
        )
        buttons = [
            {"text": "✅ Будем", "callback_data": f"checkin:{wid}:{nonce}:ready"},
            {"text": "⏰ Опоздаем", "callback_data": f"checkin:{wid}:{nonce}:late"},
            {"text": "❌ Не придём", "callback_data": f"checkin:{wid}:{nonce}:no_show"},
        ]
        user = await self.users.get_by_telegram_id(data["actor_telegram_id"])
        if not user:
            return
        nid = await self.notifications.create(user["id"], "parent_prelesson_reminder", msg)
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
            "notification_id": nid,
            "telegram_id": data["actor_telegram_id"],
            "message": msg,
            "workflow_id": wid,
            "buttons": buttons,
        }))

    async def _send_tutor_prelesson_reminder(self, wid: int) -> None:
        wf, data = await self._load_workflow(wid)
        if not wf or wf["state"] != "running":
            return
        nonce = data.get("nonce") or secrets.token_hex(4)
        data["nonce"] = nonce
        await self.repo.update_data(wid, data)
        students = ", ".join(data.get("student_names") or []) or "учеником"
        # Dual timezone display
        start_time = data.get("start_time", "")
        user_tz = data.get("actor_timezone")
        time_display = _format_dual_time(start_time, user_tz) if start_time else ""
        time_line = f"\n🕐 Время: {time_display}" if time_display else ""
        msg = (
            f"🧑‍🏫 Через {settings.albion_prelesson_reminder_min} мин урок.{time_line}\n"
            f"Репетитор: {data.get('tutor_name', 'Репетитор')}\n"
            f"Ученики: {students}\n\n"
            f"Подтвердите готовность."
        )
        buttons = [
            {"text": "✅ Готов(а)", "callback_data": f"checkin:{wid}:{nonce}:ready"},
            {"text": "⏰ Опоздаю", "callback_data": f"checkin:{wid}:{nonce}:late"},
            {"text": "❌ Не смогу провести", "callback_data": f"checkin:{wid}:{nonce}:no_show"},
            {"text": "🛠 Проблема с платформой", "callback_data": f"checkin:{wid}:{nonce}:tech"},
        ]
        user = await self.users.get_by_telegram_id(data["actor_telegram_id"])
        if not user:
            return
        nid = await self.notifications.create(user["id"], "tutor_prelesson_reminder", msg)
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
            "notification_id": nid,
            "telegram_id": data["actor_telegram_id"],
            "message": msg,
            "workflow_id": wid,
            "buttons": buttons,
        }))

    async def _send_tutor_start_check(self, wid: int) -> None:
        wf, data = await self._load_workflow(wid)
        if not wf or wf["state"] != "running":
            return
        nonce = data.get("nonce") or secrets.token_hex(4)
        data["nonce"] = nonce
        await self.repo.update_data(wid, data)
        students = ", ".join(data.get("student_names") or []) or "учеником"
        msg = (
            f"▶️ Время урока: {_format_class_label(data.get('class_id', '—'), data.get('start_time'))}.\n"
            f"Ученики: {students}\n\n"
            f"Отметьте статус старта урока."
        )
        buttons = [
            {"text": "✅ Урок начался", "callback_data": f"checkin:{wid}:{nonce}:class_started"},
            {"text": "👤 Ученик не пришёл", "callback_data": f"checkin:{wid}:{nonce}:student_absent"},
            {"text": "🛠 Техпроблема", "callback_data": f"checkin:{wid}:{nonce}:tech"},
        ]
        user = await self.users.get_by_telegram_id(data["actor_telegram_id"])
        if not user:
            return
        nid = await self.notifications.create(user["id"], "tutor_start_check", msg)
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
            "notification_id": nid,
            "telegram_id": data["actor_telegram_id"],
            "message": msg,
            "workflow_id": wid,
            "buttons": buttons,
        }))

    async def _check_no_reply(self, wid: int, title: str) -> None:
        wf, data = await self._load_workflow(wid)
        if not wf or wf["state"] != "running":
            return
        data["response_status"] = "no_reply"
        await self.notify_coordinators(
            title,
            [
                f"Занятие: {_format_class_label(data.get('class_id', '—'), data.get('start_time'))}",
                f"Actor: {data.get('actor_type')}",
                f"Telegram: {data.get('actor_telegram_id')}",
                f"Ученик(и): {data.get('student_name') or ', '.join(data.get('student_names') or []) or '—'}",
                f"Репетитор: {data.get('tutor_name', '—')}",
            ],
        )
        await self._cancel_future_actions(wid)
        await self._save_workflow(wid, "completed", data)

    async def _check_class_live(self, wid: int) -> None:
        wf, data = await self._load_workflow(wid)
        if not wf or wf["state"] != "running":
            return
        class_id = data.get("class_id")
        status = await self.class_status.get(class_id)
        if status and status.get("last_status") == "lv":
            data["response_status"] = "class_live"
            await self._cancel_future_actions(wid)
            await self._save_workflow(wid, "completed", data)
            return

        # Контекст: проверяем, ответил ли tutor на start check.
        # Это помогает координатору понять, в чём проблема.
        tutor_status = "не ответил"
        tutor_start_wf = await self.repo._fetchone(
            "SELECT * FROM workflow_instances "
            "WHERE workflow_type='tutor_start_check' AND data LIKE ? "
            "ORDER BY id DESC LIMIT 1",
            (f'%"class_id": "{class_id}"%',),
        )
        if tutor_start_wf:
            try:
                start_data = json.loads(tutor_start_wf.get("data") or "{}")
                resp = start_data.get("response_status")
                if resp == "class_started":
                    tutor_status = "подтвердил старт"
                elif resp == "student_absent":
                    tutor_status = "отметил отсутствие ученика"
                elif resp == "tech":
                    tutor_status = "сообщил о техпроблеме"
                elif resp == "no_reply":
                    tutor_status = "не ответил"
                elif resp:
                    tutor_status = resp
            except Exception:
                pass

        # Формируем сообщение в зависимости от контекста
        if tutor_status == "подтвердил старт":
            # Tutor на месте, урок не в live → скорее всего техпроблема или ученик не подключился
            title = "⚠️ Урок не перешёл в live, но репетитор подтвердил готовность"
            extra = [
                f"Репетитор: {data.get('tutor_name', '—')} (✅ подтвердил старт)",
                f"Ученики: {', '.join(data.get('student_names') or []) or '—'}",
                "Возможные причины: техпроблема на платформе / ученик не подключился",
            ]
        elif tutor_status == "отметил отсутствие ученика":
            # Tutor уже сообщил что ученика нет — отдельный absence flow уже запущен
            title = "ℹ️ Урок не в live: репетитор уже отметил отсутствие ученика"
            extra = [
                f"Репетитор: {data.get('tutor_name', '—')}",
                f"Ученики: {', '.join(data.get('student_names') or []) or '—'}",
                "Уведомление родителю уже отправлено.",
            ]
        elif tutor_status == "сообщил о техпроблеме":
            title = "🛠 Урок не в live: репетитор сообщил о техпроблеме"
            extra = [
                f"Репетитор: {data.get('tutor_name', '—')}",
                f"Ученики: {', '.join(data.get('student_names') or []) or '—'}",
            ]
        else:
            # Tutor не ответил — может не быть на месте
            title = "🚨 Урок не перешёл в live, репетитор не ответил"
            extra = [
                f"Репетитор: {data.get('tutor_name', '—')} (нет подтверждения)",
                f"Ученики: {', '.join(data.get('student_names') or []) or '—'}",
                "Возможные причины: репетитор не подключился / техпроблема / неявка",
            ]

        await self.notify_coordinators(title, [f"Занятие: {_format_class_label(class_id, data.get('start_time'))}", *extra])
        data["response_status"] = "class_not_live"
        await self._save_workflow(wid, "completed", data)

    async def handle_scheduler_tick(self, event: Event) -> None:
        action = event.data.get("action")
        payload = event.data.get("data", {})
        wid = payload.get("workflow_id") or event.data.get("workflow_id")
        if not action or not wid:
            return
        if action == "parent_prelesson_reminder":
            await self._send_parent_prelesson_reminder(wid)
        elif action == "parent_prelesson_no_reply":
            await self._check_no_reply(wid, "⚠️ Родитель не ответил до начала урока")
        elif action == "tutor_prelesson_reminder":
            await self._send_tutor_prelesson_reminder(wid)
        elif action == "tutor_prelesson_no_reply":
            await self._check_no_reply(wid, "⚠️ Репетитор не подтвердил готовность")
        elif action == "tutor_start_check":
            await self._send_tutor_start_check(wid)
        elif action == "class_live_check":
            await self._check_class_live(wid)


async def register_handlers() -> None:
    ops = LessonOpsWorkflow()
    bus.subscribe(EventTypes.SCHEDULER_TICK, ops.handle_scheduler_tick)
    logger.info("Lesson ops workflow registered")
