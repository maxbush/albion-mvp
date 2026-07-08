"""Telegram bot — команды, inline кнопки, kill switch, demo-data seed."""

import asyncio, logging, secrets
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

from src.config import settings
from src.db.repository import (
    UserRepository,
    IncidentRepository,
    ScheduledActionRepository,
    NotificationRepository,
)
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.integrations.airtable_mock import MockAirtableService
from src.workflows.engine import engine
from src.workflows.absence import AbsenceWorkflow

logger = logging.getLogger(__name__)

_kill_switch_level = 2


async def can_send_async(telegram_id: str) -> bool:
    """Проверка с доступом к БД и kill switch."""
    global _kill_switch_level
    if _kill_switch_level == 2:
        return True
    if _kill_switch_level <= 0:
        return False
    repo = UserRepository()
    user = await repo.get_by_telegram_id(telegram_id)
    return user is not None and user.get("role") == "coordinator"


async def seed_demo_data() -> None:
    """Create demo users and demo notification (only if demo mode is on)."""
    if not settings.albion_demo_mode:
        logger.info("Seed skipped: ALBION_DEMO_MODE=false")
        return
    # Create demo users and a demo notification in 30s
    repo = UserRepository()
    demo_users = [
        ("111111", "tutor", "Анна Петрова (репетитор)"),
        ("222222", "tutor", "Иван Сидоров (репетитор)"),
        ("parent_1", "parent", "Родитель Миши"),
        ("parent_2", "parent", "Родитель Кати"),
        ("coordinator_1", "coordinator", "Мария Координатор"),
    ]
    for tg_id, role, name in demo_users:
        existing = await repo.get_by_telegram_id(tg_id)
        if not existing:
            await repo.create(tg_id, role, name)
            logger.info("Seed: created user %s (%s)", name, role)

    sched_repo = ScheduledActionRepository()
    pending = await sched_repo._fetchone("SELECT COUNT(*) as cnt FROM scheduled_actions")
    if pending and pending["cnt"] == 0:
        now = datetime.now(timezone.utc)
        await sched_repo.create(
            workflow_id=0,
            execute_at=(now + timedelta(seconds=30)).isoformat(),
            action="demo_notify",
            payload={"message": "Демо-уведомление! Система работает!"},
        )
        logger.info("Seed: demo notification fires in 30s")


async def _ensure_user(upd: Update, default_role: str = "parent") -> dict:
    user = upd.effective_user
    repo = UserRepository()
    existing = await repo.get_by_telegram_id(str(user.id))
    if not existing:
        lid = await repo.create(str(user.id), default_role, user.full_name or str(user.id), username=user.username)
        existing = await repo.get(lid)
        logger.info("New user: %s (%s)", user.full_name, default_role)
    return existing


async def cmd_start(upd: Update, _ctx) -> None:
    await _ensure_user(upd, "parent")
    await upd.message.reply_text(
        "👋 *Добро пожаловать в ALBION!*\n\n"
        "Команды:\n"
        "`/absent <ID>` — ученик отсутствует\n"
        "`/mock_absent` — демо: absent через 10 сек\n"
        "`/mock_demo` — демо: уведомление через 30 сек\n"
        "`/status` — состояние системы\n"
        "`/kill_switch <0|1|2>` — режим отправки\n\n"
        "Или просто напишите — разберусь.",
        parse_mode="Markdown",
    )


async def cmd_status(upd: Update, _ctx) -> None:
    global _kill_switch_level
    labels = {0: "ВСЁ ВЫКЛ", 1: "Только координаторам", 2: "Полностью"}
    sched = ScheduledActionRepository()
    p = await sched._fetchone("SELECT COUNT(*) as cnt FROM scheduled_actions WHERE status='pending'")
    cnt = p["cnt"] if p else 0
    role = "неизвестно"
    u = await UserRepository().get_by_telegram_id(str(upd.effective_user.id))
    if u: role = u["role"]
    ai = "Mock" if not settings.openrouter_api_key else "Claude"
    await upd.message.reply_text(
        f"✅ *ALBION MVP*\nВремя: {datetime.now():%H:%M:%S}\nРоль: {role}\nAI: {ai}\nОжидает: {cnt} задач\nKill Switch: {labels.get(_kill_switch_level, '?')}",
        parse_mode="Markdown",
    )


async def cmd_absent(upd: Update, _ctx) -> None:
    if not _ctx.args:
        await upd.message.reply_text("Используйте: /absent <ID урока>", parse_mode="Markdown")
        return
    lid = _ctx.args[0]
    await _ensure_user(upd, "tutor")
    await bus.publish(Event(EventTypes.LESSON_ABSENT, {"lesson_id": lid, "reported_by": str(upd.effective_user.id)}))
    await upd.message.reply_text(f"Зафиксировал отсутствие по `{lid}`.", parse_mode="Markdown")


async def cmd_mock_absent(upd: Update, _ctx) -> None:
    await _ensure_user(upd, "coordinator")
    at = MockAirtableService()
    lesson = await at.get_lesson("lesson_1")
    student = await at.get_student("student_1")
    if not lesson or not student:
        await upd.message.reply_text("Ошибка: демо-данные не найдены")
        return
    repo = IncidentRepository()
    inc_id = await repo.create(lesson_ref="lesson_1", student_id="student_1", tutor_id="tutor_1", type="absence", status="pending")
    wid = await engine.start_workflow("absence_demo", {"incident_id": inc_id, "student_name": student.name, "parent_telegram_id": student.parent_telegram_id, "lesson_ref": "lesson_1"})
    sched = ScheduledActionRepository()
    await sched.create(wid, (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(), "notify_parent", {"incident_id": inc_id})
    await upd.message.reply_text(f"Демо! Инцидент #{inc_id}. Через 10 сек родитель получит уведомление.", parse_mode="Markdown")


async def cmd_mock_demo(upd: Update, _ctx) -> None:
    await _ensure_user(upd, "coordinator")
    sched = ScheduledActionRepository()
    await sched.create(0, (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(), "demo_notify", {"message": "Привет! Демо-уведомление."})
    await upd.message.reply_text("Через 30 сек придёт демо-уведомление.", parse_mode="Markdown")


async def cmd_kill_switch(upd: Update, _ctx) -> None:
    global _kill_switch_level
    if not _ctx.args:
        await upd.message.reply_text("/kill_switch 0|1|2")
        return
    try:
        level = int(_ctx.args[0])
        if level not in (0, 1, 2): raise ValueError
    except ValueError:
        await upd.message.reply_text("Уровни: 0 выкл, 1 координаторы, 2 всё")
        return
    _kill_switch_level = level
    labels = {0: "ВЫКЛ", 1: "Координаторы", 2: "ВСЁ"}
    await upd.message.reply_text(f"Kill Switch: {labels[level]}")
    logger.warning("kill_switch=%d by %s", level, upd.effective_user.id)


async def cmd_ok(upd: Update, _ctx) -> None:
    if not _ctx.args:
        await upd.message.reply_text("/ok <ID>")
        return
    try:
        iid = int(_ctx.args[0])
    except ValueError:
        await upd.message.reply_text("ID должен быть числом")
        return
    repo = IncidentRepository()
    inc = await repo.get(iid)
    if not inc:
        await upd.message.reply_text(f"Инцидент #{iid} не найден.")
        return
    if inc["status"] == "resolved":
        await upd.message.reply_text("Уже закрыт.")
        return
    await repo.update_status(iid, "resolved", "parent_confirmed")
    await upd.message.reply_text(f"Спасибо! Инцидент #{iid} закрыт!")


async def handle_callback(upd: Update, _ctx) -> None:
    query = upd.callback_query
    await query.answer()
    data = query.data
    if data.startswith("resolve:"):
        parts = data.split(":")
        try:
            inc_id = int(parts[1])
        except (IndexError, ValueError):
            await query.edit_message_text("Ошибка: некорректный инцидент.")
            return
        wf = AbsenceWorkflow()
        await wf.resolve_absence(inc_id, str(query.from_user.id))
        await query.edit_message_text(f"Все в порядке! Инцидент #{inc_id} закрыт. (подтверждено в {datetime.now():%H:%M})")
        logger.info("Incident %d resolved via button", inc_id)
        return
    await query.edit_message_text("Неизвестная команда.")


async def handle_message(upd: Update, _ctx) -> None:
    await _ensure_user(upd, "parent")
    text = upd.message.text
    logger.info("Msg from %s: %s", upd.effective_user.id, text[:100])
    await bus.publish(Event(EventTypes.MESSAGE_INCOMING, {
        "text": text, "telegram_id": str(upd.effective_user.id), "chat_id": str(upd.effective_chat.id),
    }))
    await upd.message.reply_text("Обрабатываю...")


async def _demo_tick_handler(event: Event) -> None:
    action = event.data.get("action")
    payload = event.data.get("data", {})
    if action == "demo_notify":
        msg = payload.get("message", "Демо-уведомление!")
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
            "telegram_id": "coordinator_1", "message": msg,
        }))


def setup_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("absent", cmd_absent))
    app.add_handler(CommandHandler("mock_absent", cmd_mock_absent))
    app.add_handler(CommandHandler("mock_demo", cmd_mock_demo))
    app.add_handler(CommandHandler("kill_switch", cmd_kill_switch))
    app.add_handler(CommandHandler("ok", cmd_ok))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    async def notif_handler(event: Event):
        tg = event.data.get("telegram_id")
        msg = event.data.get("message", "")
        cb_data = event.data.get("callback_data")
        if not tg or not msg:
            return
        if not await can_send_async(tg):
            logger.info("Kill switch blocked msg to %s", tg)
            return
        try:
            if cb_data:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("Все в порядке", callback_data=cb_data)]])
                await app.bot.send_message(chat_id=tg, text=msg, reply_markup=kb)
            else:
                await app.bot.send_message(chat_id=tg, text=msg)
            nid = event.data.get("notification_id")
            if nid:
                await NotificationRepository().mark_sent(nid)
            await bus.publish(Event(EventTypes.NOTIFICATION_DELIVERED, {"telegram_id": tg, "notification_id": nid}))
        except Exception as e:
            logger.error("Send to %s failed: %s", tg, e)
            nid = event.data.get("notification_id")
            if nid:
                await NotificationRepository().mark_failed(nid, str(e))
            await bus.publish(Event(EventTypes.NOTIFICATION_FAILED, {"telegram_id": tg, "notification_id": nid, "error": str(e)}))

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, notif_handler)

    async def dlq_handler(event: Event):
        if not await can_send_async("coordinator_1"):
            return
        d = event.data
        try:
            await app.bot.send_message(chat_id="coordinator_1", text=f"ALERT: {d.get('event_type')} handler={d.get('handler')} error={d.get('error', '?')[:200]}")
        except Exception as e:
            logger.error("DLQ alert send failed: %s", e)

    bus.subscribe(EventTypes.SYSTEM_DLQ_ALERT, dlq_handler)
    bus.subscribe(EventTypes.SCHEDULER_TICK, _demo_tick_handler)
    logger.info("Bot handlers registered (kill_switch=%d)", _kill_switch_level)
