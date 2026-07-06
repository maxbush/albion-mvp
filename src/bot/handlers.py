"""Telegram bot — команды, сообщения, inline-кнопки, kill switch."""

import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

from src.config import settings
from src.db.repository import UserRepository, IncidentRepository
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.workflows.absence import AbsenceWorkflow

logger = logging.getLogger(__name__)

# Kill switch: 0=off, 1=coordinators only, 2=fully on
_kill_switch_level = 2


def can_send(telegram_id: str | None) -> bool:
    """Проверяет kill switch. Координаторы проходят при level >= 1."""
    global _kill_switch_level
    if _kill_switch_level == 2:
        return True
    if _kill_switch_level == 1 and telegram_id and "coordinator" in str(telegram_id):
        return True
    return False


# ─── Commands ───────────────────────────────────────────────────

async def cmd_start(upd, ctx):
    await upd.message.reply_text(
        "👋 *Добро пожаловать в ALBION!*\n\n"
        "Команды:\n"
        "`/absent <ID>` — ученик отсутствует\n"
        "`/mock_absent` — демо: absent через 10 сек\n"
        "`/status` — состояние системы\n"
        "`/kill_switch <0|1|2>` — режим отправки\n\n"
        "Или просто напишите — разберусь.",
        parse_mode="Markdown",
    )

async def cmd_status(upd, ctx):
    global _kill_switch_level
    kill_labels = {0: "🔴 ВСЁ ВЫКЛ", 1: "🟡 Только координаторам", 2: "🟢 Полностью"}
    await upd.message.reply_text(
        f"✅ *ALBION MVP*\n"
        f"Время: {datetime.now():%H:%M:%S}\n"
        f"AI: {'🟡 Mock' if not settings.openrouter_api_key else '🟢 Claude'}\n"
        f"БД: SQLite 🟢\n"
        f"Kill Switch: {kill_labels.get(_kill_switch_level, '?')}",
        parse_mode="Markdown",
    )

async def cmd_absent(upd, ctx):
    """Обычный absent."""
    if not ctx.args:
        await upd.message.reply_text("ℹ️ /absent <ID урока>\nНапример: /absent lesson_1", parse_mode="Markdown")
        return
    _register_user(upd)
    await bus.publish(Event(EventTypes.LESSON_ABSENT, {
        "lesson_id": ctx.args[0],
        "reported_by": str(upd.effective_user.id),
        "tutor_telegram_id": str(upd.effective_user.id),
    }))
    await upd.message.reply_text(f"✅ Зафиксировал отсутствие по `{ctx.args[0]}`.", parse_mode="Markdown")

async def cmd_mock_absent(upd, ctx):
    """Демо: absent через 10 секунд вместо 5 минут."""
    _register_user(upd)
    # Публикуем absent, но в absence.py уже schedule на 5 мин.
    # Для демо мы напрямую создадим scheduled_action с execute_at = now + 10s
    from src.db.repository import ScheduledActionRepository
    from src.workflows.engine import engine
    from datetime import timezone, timedelta

    # Создаём инцидент напрямую
    airtable = __import__('src.integrations.airtable_mock', fromlist=['MockAirtableService']).MockAirtableService()
    lesson = await airtable.get_lesson("lesson_1")
    student = await airtable.get_student("student_1")
    repo = IncidentRepository()
    inc_id = await repo.create(lesson_ref="lesson_1", student_id="student_1", tutor_id="tutor_1", type="absence", status="pending")

    wid = await engine.start_workflow("absence_notification_demo", {
        "incident_id": inc_id, "student_name": student.name,
        "parent_telegram_id": student.parent_telegram_id, "lesson_ref": "lesson_1",
    })

    # 10 секунд вместо 5 минут
    sch = ScheduledActionRepository()
    execute_at = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    await sch.create(wid, execute_at, "notify_parent", {"incident_id": inc_id})

    await upd.message.reply_text(
        f"🎬 *Демо-режим!*\n\n"
        f"Инцидент #{inc_id} создан.\n"
        f"Уведомление родителю — через 10 секунд.\n"
        f"(в реальности — через 5 минут)",
        parse_mode="Markdown",
    )

async def cmd_kill_switch(upd, ctx):
    """Управление kill switch."""
    global _kill_switch_level
    if not ctx.args:
        await upd.message.reply_text("ℹ️ /kill_switch <0|1|2>\n0=выкл, 1=только координаторам, 2=вкл")
        return
    try:
        level = int(ctx.args[0])
        if level not in (0, 1, 2):
            raise ValueError
    except ValueError:
        await upd.message.reply_text("❌ Уровень: 0 (выкл), 1 (координаторы), 2 (всё)")
        return

    _kill_switch_level = level
    labels = {0: "🔴 ВЫКЛЮЧЕНО", 1: "🟡 Только координаторам", 2: "🟢 ПОЛНОСТЬЮ"}
    await upd.message.reply_text(f"✅ Kill Switch: {labels[level]}")
    logger.warning("Kill switch set to %d by %s", level, upd.effective_user.id)

    await bus.publish(Event(EventTypes.SYSTEM_KILL_SWITCH, {
        "level": level,
        "set_by": str(upd.effective_user.id),
    }))

async def cmd_ok(upd, ctx):
    """Ручной /ok (на случай если кнопка не сработала)."""
    if not ctx.args:
        await upd.message.reply_text("ℹ️ /ok <ID инцидента>")
        return
    try:
        iid = int(ctx.args[0])
    except ValueError:
        await upd.message.reply_text("❌ ID должен быть числом.")
        return
    repo = IncidentRepository()
    inc = await repo.get(iid)
    if not inc:
        await upd.message.reply_text(f"❌ Инцидент #{iid} не найден.")
        return
    if inc["status"] == "resolved":
        await upd.message.reply_text("✅ Уже закрыт.")
        return
    await repo.update_status(iid, "resolved", "parent_confirmed")
    await upd.message.reply_text(f"✅ *Спасибо!* Инцидент #{iid} закрыт! 🙌", parse_mode="Markdown")
    logger.info("Incident %d resolved via /ok by %s", iid, upd.effective_user.id)

async def cmd_replay(upd, ctx):
    """Переопубликовать событие из DLQ (для отладки)."""
    if not ctx.args:
        await upd.message.reply_text("ℹ️ /replay <event_id или dlq_id>")
        return
    await upd.message.reply_text("🔄 Функция /replay — в разработке. Пока что перезапустите бота.")


# ─── Inline Buttons ─────────────────────────────────────────────

async def handle_callback(upd, ctx):
    """Обрабатывает нажатия inline-кнопок."""
    query = upd.callback_query
    await query.answer()  # всегда отвечаем, чтобы Telegram не ждал

    data = query.data
    # Формат: resolve:incident_id:nonce
    if data.startswith("resolve:"):
        parts = data.split(":")
        if len(parts) >= 2:
            try:
                inc_id = int(parts[1])
            except ValueError:
                await query.edit_message_text("❌ Ошибка: некорректный инцидент.")
                return

            wf = AbsenceWorkflow()
            await wf.resolve_absence(inc_id, str(query.from_user.id))

            # Меняем текст сообщения — убираем кнопки
            await query.edit_message_text(
                f"✅ Всё в порядке! Инцидент #{inc_id} закрыт. 🙌\n\n"
                f"(подтверждено в {datetime.now():%H:%M})",
            )
            logger.info("Incident %d resolved via inline button by %s", inc_id, query.from_user.id)
            return

    await query.edit_message_text("❌ Неизвестная команда.")


# ─── Messages ───────────────────────────────────────────────────

async def handle_message(upd, ctx):
    _register_user(upd)
    text = upd.message.text
    await bus.publish(Event(EventTypes.MESSAGE_INCOMING, {
        "text": text,
        "telegram_id": str(upd.effective_user.id),
        "chat_id": str(upd.effective_chat.id),
    }))
    await upd.message.reply_text("⏳ Обрабатываю...")


# ─── Utils ──────────────────────────────────────────────────────

def _register_user(upd):
    """Регистрирует пользователя если новый."""
    user = upd.effective_user
    import asyncio
    repo = UserRepository()
    existing = asyncio.get_event_loop().run_until_complete(repo.get_by_telegram_id(str(user.id)))
    if not existing:
        asyncio.get_event_loop().run_until_complete(
            repo.create(str(user.id), "parent", user.full_name or str(user.id), username=user.username)
        )


# ─── Setup ──────────────────────────────────────────────────────

def setup_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("absent", cmd_absent))
    app.add_handler(CommandHandler("mock_absent", cmd_mock_absent))
    app.add_handler(CommandHandler("kill_switch", cmd_kill_switch))
    app.add_handler(CommandHandler("ok", cmd_ok))
    app.add_handler(CommandHandler("replay", cmd_replay))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Подписка на NOTIFICATION_REQUESTED — отправка через Telegram с учётом kill switch
    async def notif_handler(event: Event):
        tg = event.data.get("telegram_id")
        msg = event.data.get("message", "")
        callback_data = event.data.get("callback_data")

        if not tg or not msg:
            return
        if not can_send(tg):
            logger.info("Kill switch: blocked message to %s", tg)
            return

        try:
            if callback_data:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Всё в порядке", callback_data=callback_data)]])
                await app.bot.send_message(chat_id=tg, text=msg, parse_mode="Markdown", reply_markup=kb)
            else:
                await app.bot.send_message(chat_id=tg, text=msg, parse_mode="Markdown")

            # Публикуем DELIVERED
            nid = event.data.get("notification_id")
            if nid:
                from src.db.repository import NotificationRepository
                await NotificationRepository().mark_sent(nid)
            await bus.publish(Event(EventTypes.NOTIFICATION_DELIVERED, {
                "telegram_id": tg, "notification_id": nid,
            }))
        except Exception as e:
            logger.error("Send to %s failed: %s", tg, e)
            nid = event.data.get("notification_id")
            if nid:
                from src.db.repository import NotificationRepository
                await NotificationRepository().mark_failed(nid, str(e))
            await bus.publish(Event(EventTypes.NOTIFICATION_FAILED, {
                "telegram_id": tg, "notification_id": nid, "error": str(e),
            }))

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, notif_handler)

    # Подписка на SYSTEM_DLQ_ALERT — уведомление координатора
    async def dlq_alert_handler(event: Event):
        if not can_send("coordinator"):
            return
        d = event.data
        try:
            await app.bot.send_message(
                chat_id="coordinator_1",
                text=f"⚠️ *Системный алерт:* необработанное событие\n"
                     f"Тип: `{d.get('event_type')}`\n"
                     f"Хендлер: `{d.get('handler')}`\n"
                     f"Ошибка: {d.get('error', 'неизвестна')[:200]}",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error("DLQ alert send failed: %s", e)

    bus.subscribe(EventTypes.SYSTEM_DLQ_ALERT, dlq_alert_handler)

    logger.info("Bot handlers registered (kill_switch=%d)", _kill_switch_level)
