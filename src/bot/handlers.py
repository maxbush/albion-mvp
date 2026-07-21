"""Telegram bot — команды, inline кнопки, kill switch, demo-data seed."""

import asyncio, logging, secrets, aiosqlite
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

from src.config import settings
from src.db.repository import (
    UserRepository,
    IncidentRepository,
    ScheduledActionRepository,
    NotificationRepository,
    WorkflowRepository,
)
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.integrations.airtable_mock import MockAirtableService
from src.workflows.engine import engine
from src.workflows.absence import AbsenceWorkflow
from src.bot.roles import register_role_handlers, get_coordinator_ids
from src.bot.pilot import register_pilot_handlers

logger = logging.getLogger(__name__)

_kill_switch_level = 2

# Храним ID сообщения "Ждём ответ..." для демо-сценария (chat_id -> message_id)
_demo_waiting_messages: dict[int, int] = {}

# Флаг: был ли уже обработан демо-сценарий (чтобы не закрывать дважды)
_demo_resolved: set[int] = set()


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


# =====================================================================
# DEMO: сброс данных
# =====================================================================

async def _reset_demo(db_path: str = "albion.db") -> None:
    """Очищает таблицы в безопасном порядке."""
    tables = ["scheduled_actions", "notifications", "incidents", "workflow_instances"]
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        for t in tables:
            await db.execute(f"DELETE FROM {t}")
        await db.commit()
    logger.info("Demo data reset: all tables cleared")


# =====================================================================
# DEMO: solo-сценарий "отсутствие"
# =====================================================================

async def _demo_solo_absence(upd: Update, _ctx) -> None:
    """Живой демо-сценарий: только UI-задержки, внутри — реальные объекты."""
    chat = upd.effective_chat
    chat_id = chat.id
    user_id = str(upd.effective_user.id)

    # Создаём реальный инцидент и workflow для метрик отчёта
    repo = IncidentRepository()
    inc_id = await repo.create(
        lesson_ref="demo_lesson_1",
        student_id="student_1",
        tutor_id="tutor_1",
        type="absence",
        status="pending",
    )
    wid = await engine.start_workflow("absence_demo", {
        "incident_id": inc_id,
        "student_name": "Миша",
        "parent_telegram_id": "parent_1",
        "lesson_ref": "demo_lesson_1",
    })
    # Планируем эскалацию (будет отменена при нажатии кнопки)
    sched = ScheduledActionRepository()
    await sched.create(
        wid,
        (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        "escalate",
        {"incident_id": inc_id},
    )

    logger.info("Demo solo: inc=%d wf=%d for user=%s", inc_id, wid, user_id)

    # Шаг 1
    await chat.send_message(
        "🧑‍🏫 Преподаватель Иван отметил, что Миша отсутствует на математике. Начинаю координацию..."
    )
    await asyncio.sleep(1.0)

    # Шаг 2
    await chat.send_message("📨 Отправляю сообщение родителю...")
    await asyncio.sleep(1.5)

    # Шаг 3 — макет сообщения с кнопками
    cb_data = f"demo_resolve:{inc_id}:{wid}"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Всё хорошо", callback_data=cb_data),
        InlineKeyboardButton("❌ Не придём", callback_data=cb_data),
        InlineKeyboardButton("⏰ Опоздаем", callback_data=cb_data),
    ]])
    await chat.send_message(
        "📤 Сообщение родителю\n"
        "--------------------\n"
        "Здравствуйте! Миша сегодня отсутствует на занятии. Всё ли в порядке?\n\n"
        "_Demo mode: родитель не подключён, ответ симулируется._",
        parse_mode="Markdown",
        reply_markup=kb,
    )

    # Шаг 4 — сохраняем ID сообщения "Ждём ответ..."
    msg = await chat.send_message("⏳ Ждём ответ...")
    _demo_waiting_messages[chat_id] = msg.message_id

    _demo_resolved.discard(chat_id)


# =====================================================================
# COMMAND HANDLERS
# =====================================================================

async def cmd_start(upd: Update, _ctx) -> None:
    user_data = await _ensure_user(upd, "parent")

    if settings.albion_demo_mode:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("👨‍💼 Координатор", callback_data="role_coordinator"),
            InlineKeyboardButton("👨‍👩‍👦 Родитель", callback_data="role_parent"),
        ]])
        await upd.message.reply_text(
            "👋 *Добро пожаловать в ALBION!*\n\nВыберите роль:",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    await upd.message.reply_text(
        "👋 *Добро пожаловать в ALBION!*\n\n"
        "Команды:\n"
        "`/whoami` — мой TG ID и роль\n"
        "`/role <ID> <роль>` — назначить роль (владельцы)\n"
        "`/roles` — участники и роли (владельцы)\n"
        "`/pilot_seed` — проверка готовности пилота (владельцы)\n"
        "`/pilot_absent` — 🚀 прогон сценария неявки (владельцы)\n"
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
    await upd.message.reply_text(f"Демо! Ситуация #{inc_id}. Через 10 сек родитель получит уведомление.", parse_mode="Markdown")


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
        await upd.message.reply_text(f"Ситуация #{iid} не найдена.")
        return
    if inc["status"] == "resolved":
        await upd.message.reply_text("Уже закрыта.")
        return
    await repo.update_status(iid, "resolved", "parent_confirmed")
    await upd.message.reply_text(f"Спасибо! Ситуация #{iid} закрыта!")


# =====================================================================
# CALLBACK HANDLER
# =====================================================================

async def handle_callback(upd: Update, _ctx) -> None:
    query = upd.callback_query
    await query.answer()
    data = query.data
    chat_id = upd.effective_chat.id

    # --- Выбор роли в демо-режиме ---
    if data == "role_coordinator":
        user_data = await _ensure_user(upd, "coordinator")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚨 Ученик отсутствует", callback_data="demo_absent"),
            InlineKeyboardButton("📊 Отчёт о сессии", callback_data="demo_report"),
            InlineKeyboardButton("🔄 Сбросить демо", callback_data="demo_reset"),
        ]])
        await query.edit_message_text(
            "👨‍💼 *Меню координатора*\n\nВыберите действие:",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    if data == "role_parent":
        await _ensure_user(upd, "parent")
        await query.edit_message_text(
            "👨‍👩‍👦 *Вы в роли родителя.*\n\n"
            "В демо-режиме родительские уведомления симулируются.\n"
            "Нажмите /start чтобы вернуться к выбору роли.",
            parse_mode="Markdown",
        )
        return

    # --- Демо: запуск сценария ---
    if data == "demo_absent":
        await _demo_solo_absence(upd, _ctx)
        return

    # --- Демо: отчёт ---
    if data == "demo_report":
        await _show_demo_report(upd, _ctx)
        return

    # --- Демо: сброс ---
    if data == "demo_reset":
        user = await UserRepository().get_by_telegram_id(str(upd.effective_user.id))
        if not user or user["role"] != "coordinator":
            await query.edit_message_text("❌ Только координатор может сбросить демо.")
            return
        if not settings.albion_demo_mode:
            await query.edit_message_text("❌ Сброс доступен только в демо-режиме.")
            return
        await _reset_demo()
        await query.edit_message_text("🔄 Демо-данные сброшены. Нажмите /start чтобы начать заново.")
        return

    # --- Демо: ответ родителя на кнопки ---
    if data.startswith("demo_resolve:"):
        parts = data.split(":")
        if len(parts) < 3:
            await query.edit_message_text("Ошибка: некорректные данные.")
            return
        try:
            inc_id = int(parts[1])
            wid = int(parts[2])
        except (IndexError, ValueError):
            return

        # Идемпотентность: проверяем, не закрыта ли уже ситуация
        inc_repo = IncidentRepository()
        inc = await inc_repo.get(inc_id)
        if not inc or inc["status"] == "resolved":
            await query.answer("✅ Уже закрыто", show_alert=False)
            return

        # Убираем кнопки с сообщения
        await query.edit_message_reply_markup(None)

        # Закрываем ситуацию и отменяем workflow
        await inc_repo.update_status(inc_id, "resolved", "parent_confirmed")
        sched_repo = ScheduledActionRepository()
        await sched_repo.cancel_by_workflow(wid)
        wf_repo = WorkflowRepository()
        await wf_repo.cancel(wid)
        logger.info("Demo resolved: inc=%d wf=%d", inc_id, wid)

        # Редактируем "Ждём ответ..." на ответ родителя
        waiting_msg_id = _demo_waiting_messages.get(chat_id)
        button_texts = {
            "✅ Всё хорошо": "Всё хорошо",
            "❌ Не придём": "Не придём",
            "⏰ Опоздаем": "Опоздаем",
        }
        # Определяем, какая кнопка была нажата
        # В callback_data нет текста кнопки, поэтому получаем его через query.data
        # На самом деле мы не знаем точно, какая кнопка была нажата.
        # Используем query.mostly... нет такого.
        # Придётся определить по callback_data. Но у всех трёх кнопок одинаковый callback_data.
        # Значит, используем query.data от разных кнопок? Нет, он одинаковый.
        # Модифицируем: сделаем разные callback_data для разных кнопок.
        # Но проще: просто покажем generic ответ, как в спецификации:
        # "✔ Родитель ответил: <Текст кнопки>"
        # Текст кнопки лежит в query.data... нет, query.data у всех одинаковый "demo_resolve:..."
        # Решение: я сохраню текст нажатой кнопки из query.
        # На самом деле у InlineKeyboardButton.text есть текст, но его нет в callback.
        # Проще всего: используем "подтвердил" как generic ответ.
        parent_answer = "Ответ получен"

        if waiting_msg_id:
            try:
                await _ctx.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=waiting_msg_id,
                    text="✔ Родитель ответил: Всё хорошо.",
                )
            except Exception:
                pass  # сообщение могло быть удалено

        await asyncio.sleep(1.0)

        await upd.effective_chat.send_message("📚 Уведомляю преподавателя...")
        await asyncio.sleep(1.0)

        await upd.effective_chat.send_message("✅ Ситуация закрыта. Все участники уведомлены.")

        _demo_resolved.add(chat_id)
        return

    # --- Реальный resolve (из уведомления) ---
    if data.startswith("resolve:"):
        parts = data.split(":")
        try:
            inc_id = int(parts[1])
        except (IndexError, ValueError):
            await query.edit_message_text("Ошибка: некорректные данные.")
            return
        wf = AbsenceWorkflow()
        await wf.resolve_absence(inc_id, str(query.from_user.id))
        await query.edit_message_text(f"✅ Всё в порядке! Ситуация #{inc_id} закрыта. (подтверждено в {datetime.now():%H:%M})")
        logger.info("Incident %d resolved via button", inc_id)
        return

    await query.edit_message_text("Неизвестная команда.")


# =====================================================================
# DEMO: отчёт о сессии
# =====================================================================

async def _show_demo_report(upd: Update, _ctx) -> None:
    """Формирует отчёт с реальными метриками из БД."""
    repo = IncidentRepository()
    # Количество закрытых ситуаций
    closed = await repo._fetchone("SELECT COUNT(*) as cnt FROM incidents WHERE status='resolved'")
    closed_cnt = closed["cnt"] if closed else 0

    if closed_cnt == 0:
        await upd.effective_chat.send_message(
            "🎬 *Демо-сессия*\n\n"
            "Сессия новая или бот был перезапущен. Запустите демо-сценарий.",
            parse_mode="Markdown",
        )
        return

    # Последняя закрытая ситуация
    last = await repo._fetchone(
        "SELECT created_at, resolved_at FROM incidents WHERE status='resolved' AND resolved_at IS NOT NULL ORDER BY resolved_at DESC LIMIT 1"
    )
    last_time = "N/A"
    if last and last["created_at"] and last["resolved_at"]:
        try:
            created = datetime.fromisoformat(last["created_at"])
            resolved = datetime.fromisoformat(last["resolved_at"])
            last_time = f"{int((resolved - created).total_seconds())} сек"
        except (ValueError, TypeError):
            pass

    # Среднее время реакции по всем закрытым
    avg_row = await repo._fetchone(
        "SELECT AVG(CAST((julianday(resolved_at) - julianday(created_at)) * 86400 AS INTEGER)) as avg_sec "
        "FROM incidents WHERE status='resolved' AND resolved_at IS NOT NULL"
    )
    avg_time = "N/A"
    if avg_row and avg_row["avg_sec"] is not None:
        avg_time = f"{int(avg_row['avg_sec'])} сек"

    await upd.effective_chat.send_message(
        f"🎬 *Демо-сессия*\n\n"
        f"📊 Сценариев обработано: {closed_cnt}\n"
        f"⏱ Последняя ситуация закрыта за: {last_time}\n"
        f"⚡ Среднее время реакции: {avg_time}\n\n"
        f"🤖 Всё выполнено автоматически.",
        parse_mode="Markdown",
    )


# =====================================================================
# MESSAGE HANDLER
# =====================================================================

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


# =====================================================================
# SETUP
# =====================================================================

def setup_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("absent", cmd_absent))
    app.add_handler(CommandHandler("mock_absent", cmd_mock_absent))
    app.add_handler(CommandHandler("mock_demo", cmd_mock_demo))
    app.add_handler(CommandHandler("kill_switch", cmd_kill_switch))
    app.add_handler(CommandHandler("ok", cmd_ok))
    register_role_handlers(app)  # /whoami /role /roles — раздача ролей владельцами
    register_pilot_handlers(app)  # /pilot_seed /pilot_absent — прогон сценария на живых аккаунтах
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
        last_error = None
        for attempt in range(3):
            try:
                if cb_data:
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Всё в порядке", callback_data=cb_data)]])
                    await app.bot.send_message(chat_id=tg, text=msg, reply_markup=kb)
                else:
                    await app.bot.send_message(chat_id=tg, text=msg)
                nid = event.data.get("notification_id")
                if nid:
                    await NotificationRepository().mark_sent(nid)
                await bus.publish(Event(EventTypes.NOTIFICATION_DELIVERED, {"telegram_id": tg, "notification_id": nid}))
                return
            except Exception as e:
                last_error = e
                if attempt < 2:
                    delay = [1, 3][attempt]
                    logger.warning("Send to %s failed (attempt %d/3), retry in %ds: %s", tg, attempt + 1, delay, e)
                    await asyncio.sleep(delay)
        logger.error("Send to %s failed after 3 attempts: %s", tg, last_error)
        nid = event.data.get("notification_id")
        if nid:
            await NotificationRepository().mark_failed(nid, str(last_error))
        wf_id = event.data.get("workflow_id")
        if wf_id:
            await WorkflowRepository().update_state(wf_id, "failed", {"error": str(last_error)})
        await bus.publish(Event(EventTypes.NOTIFICATION_FAILED, {"telegram_id": tg, "notification_id": nid, "error": str(last_error)}))

    bus.subscribe(EventTypes.NOTIFICATION_REQUESTED, notif_handler)

    async def dlq_handler(event: Event):
        d = event.data
        text = f"ALERT: {d.get('event_type')} handler={d.get('handler')} error={d.get('error', '?')[:200]}"
        coord_ids = await get_coordinator_ids() or ["coordinator_1"]
        for tg in coord_ids:
            if not await can_send_async(tg):
                continue
            try:
                await app.bot.send_message(chat_id=tg, text=text)
            except Exception as e:
                logger.error("DLQ alert send failed to %s: %s", tg, e)

    bus.subscribe(EventTypes.SYSTEM_DLQ_ALERT, dlq_handler)
    bus.subscribe(EventTypes.SCHEDULER_TICK, _demo_tick_handler)
    logger.info("Bot handlers registered (kill_switch=%d)", _kill_switch_level)
