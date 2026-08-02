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

# Чек-ин теряет смысл, когда занятие давно прошло: протухший workflow не должен
# перехватывать обычные сообщения пользователя (см. find_active_checkin).
CHECKIN_EXPIRY_AFTER_START_H = 3
CHECKIN_EXPIRY_NO_START_H = 24  # запасной TTL, если в данных нет start_time


def _parse_dt(v: str) -> datetime:
    """Парсит RFC3339. Наивное время (без зоны) трактуем в зоне ОРГАНИЗАЦИИ
    (settings.albion_org_timezone, канон расписания — решение владельца H4),
    НЕ как UTC: иначе напоминания уезжают на час летом (BST = UTC+1)."""
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        logger.debug("Naive datetime %r assumed %s (org canonical TZ)",
                     v, settings.albion_org_timezone)
        dt = dt.replace(tzinfo=settings.org_zone())
    return dt


def _schedule_at(target: datetime, fallback_seconds: int = 5) -> str:
    """Возвращает ВСЕГДА aware UTC ISO-строку для SQLite scheduler.

    Наивное время трактуем в зоне организации (канон расписания), тот же
    принцип, что в _parse_dt."""
    now = datetime.now(timezone.utc)
    if target.tzinfo is None:
        target = target.replace(tzinfo=settings.org_zone())
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

    Опорная зона — зона организации (settings.albion_org_timezone).
    Показываем: '15:00 (London)' или '15:00 (London) / 20:00 (ваше время, Asia/Almaty)'.
    """
    try:
        from zoneinfo import ZoneInfo
        org_tz_name = settings.albion_org_timezone
        org_label = org_tz_name.split("/")[-1]  # Europe/London → 'London'
        dt = _parse_dt(start_time)
        london_tz = ZoneInfo(org_tz_name)
        london_time = dt.astimezone(london_tz)
        result = london_time.strftime("%H:%M") + f" ({org_label})"

        if user_tz and user_tz != org_tz_name:
            try:
                user_zone = ZoneInfo(user_tz)
                user_time = dt.astimezone(user_zone)
                result += f" / {user_time.strftime('%H:%M')} (ваше время, {user_tz})"
                # Разница в ЧАСОВЫХ ПОЯСАХ (не в инстантах!): вычитание aware-datetime
                # одного и того же момента даёт 0. Сравниваем utcoffset().
                london_off = london_time.utcoffset() or timedelta(0)
                user_off = user_time.utcoffset() or timedelta(0)
                diff_hours = (user_off - london_off).total_seconds() / 3600
                if diff_hours != 0:
                    sign = "+" if diff_hours > 0 else ""
                    # Целые часы показываем как int, дробные (5:45 и т.п.) — с 1 знаком
                    shown = int(diff_hours) if diff_hours == int(diff_hours) else round(diff_hours, 1)
                    result += f" [{sign}{shown}ч к {org_label}]"
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
                # Timezone ученика — только для dual-time display в напоминаниях
                # (на создание класса не влияет — канон = зона организации, H4/P4.1).
                "actor_timezone": student.get("timezone") or settings.albion_org_timezone,
            }
            wid = await self.repo.create("prelesson_parent", "running", data)
            await self.scheduler.create(wid, _schedule_at(reminder_dt), "parent_prelesson_reminder", {"workflow_id": wid})
            await self.scheduler.create(wid, _schedule_at(start_dt, 30), "parent_prelesson_no_reply", {"workflow_id": wid})

        # Tutor-side prelesson reminder.
        if tutor_telegram_id:
            student_names = [s.get("name") or s.get("student_name") or s.get("client_user_id") or "Ученик" for s in student_rows]
            tutor_tz = tutor_timezone or settings.albion_org_timezone
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

            # Live-check — на ОТДЕЛЬНОМ workflow. Иначе ответ репетитора на
            # start-check (_cancel_future_actions) отменял и live-check, и ветка
            # «урок не перешёл в live после подтверждённого старта» была недостижима.
            live_data = {
                "class_id": class_id,
                "actor_type": "coordinator_check",
                "tutor_name": tutor_name,
                "student_names": student_names,
                "start_time": start_time,
                "actor_timezone": tutor_tz,
                "tutor_start_wid": start_wid,
            }
            live_wid = await self.repo.create("class_live_check", "running", live_data)
            await self.scheduler.create(live_wid, _schedule_at(live_check_dt, 90), "class_live_check", {"workflow_id": live_wid})

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

    async def _expire_stale_checkin(self, wf: dict, data: dict) -> bool:
        """Протухший чек-ин (занятие давно прошло) — auto-complete и пропуск.

        Без этого workflow зависал в 'running' навсегда (например, пользователь
        не был зарегистрирован в момент напоминания), а следующее обычное
        сообщение от него молча «закрывало» чек-ин недельной давности."""
        now = datetime.now(timezone.utc)
        start_raw = data.get("start_time")
        try:
            if start_raw:
                expiry = _parse_dt(start_raw) + timedelta(hours=CHECKIN_EXPIRY_AFTER_START_H)
            else:
                created = datetime.fromisoformat(str(wf.get("created_at") or ""))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                expiry = created + timedelta(hours=CHECKIN_EXPIRY_NO_START_H)
        except Exception:
            return False  # дата не распарсилась — не рискуем, оставляем как есть
        if now <= expiry:
            return False  # ещё живой
        data["response_status"] = "expired"
        await self._cancel_future_actions(wf["id"])
        await self._save_workflow(wf["id"], "completed", data)
        logger.info("Check-in workflow #%d expired (start=%s)", wf["id"], start_raw)
        return True

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
            if data.get("actor_type") not in actor_types or not data.get("nonce"):
                continue
            if await self._expire_stale_checkin(wf, data):
                continue  # протухший — добили, ищем свежий дальше
            return wf["id"], data, wf_type
        return None

    async def notify_coordinators(self, title: str, lines: list[str],
                                  buttons: list[dict] | None = None) -> None:
        msg = title + "\n" + "\n".join(lines)
        from src.bot.roles import notify_all_coordinators
        await notify_all_coordinators(msg, notification_type="ops_alert",
                                      db_path=self.repo.db_path, buttons=buttons)

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
                # Отменяем class_live_check workflow (отдельный workflow, свой wid)
                # — чтобы не было лишнего алерта через 5 мин, раз ученик уже отмечен отсутствующим.
                live_check_wf = await self.repo._fetchone(
                    "SELECT * FROM workflow_instances "
                    "WHERE workflow_type='class_live_check' AND state='running' AND data LIKE ? "
                    "ORDER BY id DESC LIMIT 1",
                    (f'%"class_id": "{class_id}"%',),
                )
                if live_check_wf:
                    await self.scheduler.cancel_by_workflow(live_check_wf["id"])
                    await self.repo.cancel(live_check_wf["id"])
                    logger.info("Cancelled class_live_check #%d for class %s (student_absent)", live_check_wf["id"], class_id)
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

    async def notify_late_detail(self, wid: int, mins_str: str) -> None:
        """R8-10: Отправляет координаторам точное время опоздания репетитора."""
        wf, data = await self._load_workflow(wid)
        if not wf:
            return
        class_id = data.get("class_id", "—")
        tutor_name = data.get("tutor_name") or "Репетитор"
        student_name = data.get("student_name") or ", ".join(data.get("student_names") or []) or "Ученик"
        await self.notify_coordinators(
            "ℹ️ Уточнение по опозданию репетитора",
            [
                f"Занятие: {_format_class_label(class_id, data.get('start_time'))}",
                f"Репетитор: {tutor_name} задержится {mins_str}",
                f"Ученики: {student_name}",
            ],
        )

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
        # Тьюторы — англоязычные; язык из users.language (i18n, аудит П3)
        from src.utils.i18n import lang_of, tr
        lang = await lang_of(data["actor_telegram_id"])
        msg = tr("tutor_reminder", lang, mins=settings.albion_prelesson_reminder_min,
                 time_line=time_line, tutor=data.get("tutor_name", "Репетитор"),
                 students=students)
        buttons = [
            {"text": tr("tutor_btn_ready", lang), "callback_data": f"checkin:{wid}:{nonce}:ready"},
            {"text": tr("tutor_btn_late", lang), "callback_data": f"checkin:{wid}:{nonce}:late"},
            {"text": tr("tutor_btn_no_show", lang), "callback_data": f"checkin:{wid}:{nonce}:no_show"},
            {"text": tr("tutor_btn_tech", lang), "callback_data": f"checkin:{wid}:{nonce}:tech"},
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
        from src.utils.i18n import lang_of, tr
        lang = await lang_of(data["actor_telegram_id"])
        msg = tr("tutor_start_check", lang,
                 label=_format_class_label(data.get("class_id", "—"), data.get("start_time")),
                 students=students)
        buttons = [
            {"text": tr("tutor_btn_class_started", lang), "callback_data": f"checkin:{wid}:{nonce}:class_started"},
            {"text": tr("tutor_btn_student_absent", lang), "callback_data": f"checkin:{wid}:{nonce}:student_absent"},
            {"text": tr("tutor_btn_tech_short", lang), "callback_data": f"checkin:{wid}:{nonce}:tech"},
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
        # R7-5: вместо «Actor: tutor / Telegram: <id>» — контекст словами и
        # кнопка связи с молчащим (кто именно молчит — уже сказано в title).
        actor_tg = data.get("actor_telegram_id")
        who = "родителю" if data.get("actor_type") == "parent" else "репетитору"
        buttons = ([{"text": f"👤 Написать {who}", "url": f"tg://user?id={actor_tg}"}]
                   if actor_tg else None)
        await self.notify_coordinators(
            title,
            [
                f"Занятие: {_format_class_label(data.get('class_id', '—'), data.get('start_time'))}",
                f"Репетитор: {data.get('tutor_name', '—')}",
                f"Ученик(и): {data.get('student_name') or ', '.join(data.get('student_names') or []) or '—'}",
            ],
            buttons=buttons,
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

        # Отменяем все связанные workflow (tutor_start_check, prelesson_*)
        await self._cancel_future_actions(wid)
        await self._save_workflow(wid, "completed", data)

        await self.notify_coordinators(title, [f"Занятие: {_format_class_label(class_id, data.get('start_time'))}", *extra])

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
        elif action == MORNING_DIGEST_ACTION:
            await self._send_morning_digest_auto(wid)

    async def _send_morning_digest_auto(self, wid: int) -> None:
        """Авто-утренняя сводка координаторам. Пересоздаёт себя на следующий день
        (07:30 org) — самоподдерживающаяся рекуррентная задача в SQLite scheduler."""
        from src.bot.roles import notify_all_coordinators
        try:
            text = await build_morning_digest_text()
            await notify_all_coordinators(
                text, notification_type="morning_digest", db_path=self.repo.db_path)
        finally:
            # workflow выполнен → немедленно планируем следующий день
            await self.repo.update_state(wid, "completed", {"auto_reschedule": True})
            await _schedule_next_digest()


    async def handle_lesson_started(self, event: Event) -> None:
        class_id = event.data.get("class_id")
        if not class_id:
            return
        rows = await self.repo._fetchall(
            "SELECT * FROM workflow_instances "
            "WHERE workflow_type='class_live_check' AND state='running' AND data LIKE ?",
            (f'%"class_id": "{class_id}"%',),
        )
        for row in rows:
            wid = row["id"]
            try:
                data = json.loads(row.get("data") or "{}")
            except Exception:
                data = {}
            data["response_status"] = "class_live"
            data["resolved_by"] = "webhook_lv"
            await self._cancel_future_actions(wid)
            await self._save_workflow(wid, "completed", data)
            logger.info("LessonOps: class_live_check wid=%d completed reactively via LESSON_STARTED webhook (class_id=%s)", wid, class_id)

    async def handle_lesson_completed(self, event: Event) -> None:
        class_id = event.data.get("class_id")
        if not class_id:
            return
        rows = await self.repo._fetchall(
            "SELECT * FROM workflow_instances "
            "WHERE workflow_type IN ('prelesson_parent','prelesson_tutor','tutor_start_check','class_live_check') "
            "AND state='running' AND data LIKE ?",
            (f'%"class_id": "{class_id}"%',),
        )
        for row in rows:
            wid = row["id"]
            try:
                data = json.loads(row.get("data") or "{}")
            except Exception:
                data = {}
            data["resolved_by"] = "webhook_cp"
            await self._cancel_future_actions(wid)
            await self._save_workflow(wid, "completed", data)
            logger.info("LessonOps: wid=%d (%s) closed via LESSON_COMPLETED webhook (class_id=%s)",
                        wid, row["workflow_type"], class_id)


async def register_handlers() -> None:
    ops = LessonOpsWorkflow()
    bus.subscribe(EventTypes.SCHEDULER_TICK, ops.handle_scheduler_tick)
    bus.subscribe(EventTypes.LESSON_STARTED, ops.handle_lesson_started)
    bus.subscribe(EventTypes.LESSON_COMPLETED, ops.handle_lesson_completed)
    logger.info("Lesson ops workflow registered")


# =====================================================================
# АВТО-УТРЕННЯЯ СВОДКА (07:30 по зоне организации)
# =====================================================================

MORNING_DIGEST_ACTION = "morning_digest_send"


def _next_digest_exec() -> str:
    """Следующие 07:30 по зоне организации → aware UTC ISO для scheduler."""
    zone = settings.org_zone()
    now = datetime.now(zone)
    cand = now.replace(hour=7, minute=30, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    return cand.astimezone(timezone.utc).isoformat()


async def _schedule_next_digest() -> str:
    """Создаёт следующую задачу сводки (свежий workflow на каждый день)."""
    wid = await WorkflowRepository().create(
        "morning_digest_auto", "running", {"recurring": True})
    aid = await ScheduledActionRepository().create(
        wid, _next_digest_exec(), MORNING_DIGEST_ACTION, {"workflow_id": wid})
    logger.info("Morning digest scheduled (wf=%d, action=%s)", wid, aid)
    return aid


async def ensure_morning_digest() -> None:
    """Гарантирует наличие следующей авто-сводки. Идемпотентно — вызывается на старте бота."""
    repo = ScheduledActionRepository()
    rows = await repo._fetchall(
        "SELECT 1 FROM scheduled_actions WHERE action=? AND status='pending' LIMIT 1",
        (MORNING_DIGEST_ACTION,),
    )
    if rows:
        return
    await _schedule_next_digest()


async def build_morning_digest_text(db_path: str | None = None) -> str:
    """Текст утренней сводки (occurrence-aware). Общий для /morning и авто-рассылки."""
    from src.db.repository import (
        IncidentRepository,
        MeritHubClassRepository,
        MeritHubEnrollmentRepository,
        MeritHubStudentRepository,
    )
    from src.utils.recurrence import class_occurs_on, org_now, org_zone_label

    today = org_now().date()
    crepo = MeritHubClassRepository(db_path)
    classes = [c for c in await crepo.list_all() if class_occurs_on(c, today)]
    classes.sort(key=lambda c: (c.get("start_time") or "")[11:16] or "99:99")

    if not classes:
        return "☀️ Доброе утро!\n\n📅 Сегодня занятий нет."

    erepo = MeritHubEnrollmentRepository(db_path)
    srepo = MeritHubStudentRepository(db_path)
    org = org_zone_label()
    # R7-15: батчи вместо N+1 (enrollments + tutor rows одним запросом каждый).
    by_cid = await erepo.list_by_classes([c["class_id"] for c in classes])
    tutor_map = await srepo.get_by_client_ids(
        [c["tutor_client_user_id"] for c in classes if c.get("tutor_client_user_id")])
    lines = [f"☀️ Доброе утро!\n\n📅 Занятия сегодня ({len(classes)}):"]
    tutors: set[str] = set()
    students_total = 0
    for c in classes:
        marker = "🔁" if (c.get("class_type") or "oneTime") == "perma" else "1️⃣"
        hhmm = (c.get("start_time") or "—")[11:16]
        time_part = f"{hhmm} ({org})" if hhmm else "—"
        enr = [
            e for e in by_cid.get(c["class_id"], [])
            if (e.get("role") or "student") not in ("tutor", "teacher", "C", "host")
        ]
        students_total += len(enr)
        names = ", ".join(e.get("student_name") or "" for e in enr[:3]) or "—"
        tutor_name = ""
        if c.get("tutor_client_user_id"):
            trow = tutor_map.get(c["tutor_client_user_id"])
            tutor_name = (trow or {}).get("name") or c["tutor_client_user_id"]
            tutors.add(tutor_name)
        suffix = f" · {names}" + (f" · {tutor_name}" if tutor_name else "")
        lines.append(f"  {marker} {time_part}{suffix}")
    lines.append(f"\n👥 Учеников: {students_total} · репетиторов: {len(tutors)}")

    inc_repo = IncidentRepository(db_path)
    active = await inc_repo._fetchone(
        "SELECT COUNT(*) as cnt FROM incidents WHERE status IN ('pending','escalated','open')")
    if active and active["cnt"]:
        lines.append(f"📋 Активных инцидентов: {active['cnt']} — /incidents")
    return "\n".join(lines)
