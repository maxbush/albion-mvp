"""Telegram bot — команды, inline кнопки, kill switch, demo-data seed."""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

from src.ai.client import llm_client
from src.config import settings
from src.db.repository import (
    IncidentRepository,
    UserRepository,
    ScheduledActionRepository,
    NotificationRepository,
    WorkflowRepository,
    IdempotencyRepository,
)
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.integrations.airtable_mock import MockAirtableService
from src.workflows.engine import engine
from src.workflows.absence import AbsenceWorkflow
from src.workflows.lesson_ops import LessonOpsWorkflow
from src.bot.roles import register_role_handlers, get_coordinator_ids, is_admin, apply_command_menu
from src.bot.pilot import register_pilot_handlers

logger = logging.getLogger(__name__)

_kill_switch_level = 2

# Храним ID сообщения "Ждём ответ..." для демо-сценария (chat_id -> message_id)
_demo_waiting_messages: dict[int, int] = {}


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


def get_kill_switch_level() -> int:
    """Возвращает текущий уровень kill switch (для внешних модулей)."""
    return _kill_switch_level


def set_kill_switch_level(level: int) -> None:
    """Устанавливает уровень kill switch (0=off, 1=coordinators only, 2=full).

    NOTE: значение хранится только в памяти. После рестарта возвращается к дефолту (2).
    Для продакшена нужно персистить в БД или env."""
    global _kill_switch_level
    _kill_switch_level = level


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


async def _ensure_role(upd: Update, role: str) -> dict:
    user = upd.effective_user
    repo = UserRepository()
    uid, _ = await repo.set_role_by_telegram(
        str(user.id),
        role,
        name=user.full_name or str(user.id),
        username=user.username,
    )
    return await repo.get(uid)


# =====================================================================
# DEMO: solo-сценарий "отсутствие" (только при ALBION_DEMO_MODE=true)
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
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Всё хорошо", callback_data=f"demo_resolve:{inc_id}:{wid}:ok"),
        InlineKeyboardButton("❌ Не придём", callback_data=f"demo_resolve:{inc_id}:{wid}:no"),
        InlineKeyboardButton("⏰ Опоздаем", callback_data=f"demo_resolve:{inc_id}:{wid}:late"),
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


# =====================================================================
# COMMAND HANDLERS
# =====================================================================

def _coordinator_help_text() -> str:
    """Единый список команд координатора (было два расходящихся списка —
    в /start и в регистрации по кнопке). Underscore в командах экранируем
    для Markdown V1, иначе парные '_' ломают отображение."""
    return (
        "📋 *Ваши команды:*\n\n"
        "*Обзор:*\n"
        "/today — занятия сегодня\n"
        "/morning — утренняя сводка\n"
        "/incidents — инциденты и статистика\n"
        "/status — состояние системы\n\n"
        "*Управление:*\n"
        "/pilot\\_absent — тест: сценарий неявки\n"
        "/demo\\_reset — сброс между прогонами\n"
        "/cancel\\_lesson <ID> — отмена урока\n"
        "/ok <ID> — закрыть инцидент\n\n"
        "*MeritHub:*\n"
        "/seed10 <parentTG> — создать 10 учеников\n"
        "/mh\\_schedule <tutor> <start> <min> <students...>\n"
        "/mh\\_tutor <cuid> <tg> <имя>\n"
        "/mh\\_students — список учеников\n"
        "/mh\\_events — последние webhook'и"
    )


def _role_expectations(role: str) -> str:
    """Честные ожидания по роли — только то, что бот реально умеет (UX U4).

    Никаких обещаний AI-магии и несуществующих механик: для родителя/репетитора
    бот в первую очередь реактивный (напоминания и вопросы придут сами)."""
    if role == "coordinator":
        return (
            "Я присылаю эскалации и алерты — обычно реакция занимает одну кнопку.\n"
            "Все инструменты — по кнопке «Команды» ниже или в меню «/»."
        )
    if role == "tutor":
        return (
            "Как это работает:\n"
            "• перед уроком напомню и попрошу подтвердить готовность;\n"
            "• в начале урока попрошу отметить старт.\n\n"
            "Отменить урок: /cancel_lesson"
        )
    # parent (и дефолт)
    return (
        "Дальше ничего настраивать не нужно:\n"
        "• перед занятием напомню и уточню статус;\n"
        "• если ученик не придёт — спрошу у вас.\n\n"
        "Отменить занятие: /cancel_lesson"
    )


async def cmd_start(upd: Update, _ctx) -> None:
    user = upd.effective_user
    tg_id = str(user.id)
    repo = UserRepository()
    existing = await repo.get_by_telegram_id(tg_id)

    if existing:
        role = existing["role"]
        emoji = {"parent": "👨‍👩‍👦", "tutor": "🧑‍🏫", "coordinator": "👨‍💼", "student": "🎓"}.get(role, "")
        admin_mark = " ★" if is_admin(tg_id) else ""
        # Меню «/» под роль (UX U1): идемпотентно, обновляем при каждом /start.
        await apply_command_menu(getattr(_ctx, "bot", None), tg_id, role)
        # Одно сообщение вместо двух (UX U4): помощь открывается по кнопке,
        # а не дублируется в чат при каждом /start.
        buttons = [InlineKeyboardButton("🔄 Сменить роль", callback_data="change_role")]
        if role == "coordinator":
            buttons.insert(0, InlineKeyboardButton("📋 Команды", callback_data="help_commands"))
        await upd.message.reply_text(
            f"👋 С возвращением, {user.full_name or '—'}!\n\n"
            f"Ваша роль: {emoji} {role}{admin_mark}\n"
            f"TG ID: `{tg_id}`\n\n"
            f"{_role_expectations(role)}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([buttons]),
        )
        return

    # Новый пользователь — показываем выбор роли
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👨‍👩‍👦 Я родитель", callback_data="register_parent"),
            InlineKeyboardButton("🧑‍🏫 Я репетитор", callback_data="register_tutor"),
        ],
        [
            InlineKeyboardButton("👨‍💼 Я координатор", callback_data="register_coordinator"),
        ],
    ])
    await upd.message.reply_text(
        f"👋 *Добро пожаловать в ALBION!*\n\n"
        f"Меня зовут ALBION AI — я помогаю координировать занятия.\n\n"
        f"Пожалуйста, выберите вашу роль:",
        parse_mode="Markdown",
        reply_markup=kb,
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
    # AI-инфу показываем только админам (не сливаем модель обычным пользователям)
    if is_admin(upd.effective_user.id):
        ai = f"Mock ({settings.llm_cheap_model})" if not settings.openrouter_api_key else settings.llm_model
        ks_info = f"\nKill Switch: {labels.get(_kill_switch_level, '?')}"
    else:
        ai = "работает" if settings.openrouter_api_key else "демо-режим"
        ks_info = ""
    await upd.message.reply_text(
        f"✅ *ALBION*\nВремя: {datetime.now():%H:%M:%S}\nРоль: {role}\nAI: {ai}\nОжидает: {cnt} задач{ks_info}",
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
    if not settings.albion_demo_mode:
        await upd.message.reply_text("Демо-режим выключен. Установите ALBION_DEMO_MODE=true.")
        return
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
    if not settings.albion_demo_mode:
        await upd.message.reply_text("Демо-режим выключен. Установите ALBION_DEMO_MODE=true.")
        return
    await _demo_solo_absence(upd, _ctx)


async def cmd_kill_switch(upd: Update, _ctx) -> None:
    global _kill_switch_level
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ может менять kill switch.")
        return
    if not _ctx.args:
        # UX U3: уровни кнопками вместо запоминания 0|1|2 (recognition, не recall).
        labels = {0: "ВСЁ ВЫКЛ", 1: "Только координаторам", 2: "Полностью"}
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 Всё выкл", callback_data="killswitch:0"),
        ], [
            InlineKeyboardButton("🟡 Только координаторам", callback_data="killswitch:1"),
        ], [
            InlineKeyboardButton("🟢 Полностью", callback_data="killswitch:2"),
        ]])
        await upd.message.reply_text(
            f"🔌 Kill Switch. Сейчас: *{labels.get(_kill_switch_level, '?')}*",
            parse_mode="Markdown", reply_markup=kb,
        )
        return
    try:
        lvl = int(_ctx.args[0])
        if lvl not in (0, 1, 2):
            raise ValueError
    except ValueError:
        await upd.message.reply_text("Уровень: 0, 1 или 2")
        return
    set_kill_switch_level(lvl)
    labels = {0: "ВСЁ ВЫКЛ", 1: "Только координаторам", 2: "Полностью"}
    await upd.message.reply_text(f"🔌 Kill Switch: {labels[lvl]}")
    logger.info("Kill switch set to %d", lvl)
    await bus.publish(Event(EventTypes.SYSTEM_KILL_SWITCH, {"level": lvl}))


async def cmd_cancel_lesson(upd: Update, _ctx) -> None:
    """Отмена урока: /cancel_lesson <ID урока> [причина...]

    Команда, на которую ссылается подсказка бота при интенте «отмена»
    (cancellation.handle_classified). Раньше отсылала в никуда — команды не было.
    """
    if not _ctx.args:
        await upd.message.reply_text("Используйте: /cancel_lesson <ID урока> [причина...]")
        return
    lid = _ctx.args[0]
    reason = " ".join(_ctx.args[1:]) or "не указана"
    await _ensure_user(upd, "parent")
    await bus.publish(Event(EventTypes.LESSON_CANCELLED, {
        "lesson_id": lid,
        "reason": reason,
        "reported_by": str(upd.effective_user.id),
    }))
    await upd.message.reply_text(
        f"🔄 Отмена урока `{lid}` передана репетитору и координаторам.",
        parse_mode="Markdown",
    )


async def cmd_ok(upd: Update, _ctx) -> None:
    if not _ctx.args:
        await upd.message.reply_text("Используйте: /ok <ID ситуации>")
        return
    try:
        iid = int(_ctx.args[0])
    except ValueError:
        await upd.message.reply_text("ID должен быть числом.")
        return
    repo = IncidentRepository()
    inc = await repo.get(iid)
    if not inc:
        await upd.message.reply_text("Ситуация не найдена.")
        return
    if inc["status"] == "resolved":
        await upd.message.reply_text("Уже закрыта.")
        return
    wf = AbsenceWorkflow()
    await wf.resolve_absence(iid, str(upd.effective_user.id))
    await upd.message.reply_text(f"Спасибо! Ситуация #{iid} закрыта!")


# =====================================================================
# CALLBACK QUERY HANDLER
# =====================================================================

async def handle_callback(upd: Update, _ctx) -> None:
    query = upd.callback_query
    await query.answer()
    data = query.data
    chat_id = upd.effective_chat.id

    # --- Регистрация: выбор роли новым пользователем ---
    if data.startswith("register_"):
        role = data.replace("register_", "")
        if role not in ("parent", "tutor", "coordinator"):
            await query.edit_message_text("Неизвестная роль.")
            return
        user = upd.effective_user
        repo = UserRepository()
        existing = await repo.get_by_telegram_id(str(user.id))
        if existing:
            # Уже зарегистрирован — меняем роль
            await repo.update_role(existing["id"], role)
        else:
            await repo.create(str(user.id), role, user.full_name or str(user.id), username=user.username)
        # Меню «/» под выбранную роль (UX U1).
        await apply_command_menu(getattr(_ctx, "bot", None), user.id, role)
        emoji = {"parent": "👨‍👩‍👦", "tutor": "🧑‍🏫", "coordinator": "👨‍💼"}.get(role, "")
        admin_mark = " ★" if is_admin(str(user.id)) else ""
        # Честные ожидания по роли (UX U4) — вместо «напишите что-нибудь» в пустоту.
        markup = None
        if role == "coordinator":
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Команды", callback_data="help_commands"),
                InlineKeyboardButton("🔄 Сменить роль", callback_data="change_role"),
            ]])
        await query.edit_message_text(
            f"✅ Вы зарегистрированы как {emoji} *{role}*{admin_mark}.\n\n"
            f"{_role_expectations(role)}\n\n"
            f"TG ID: `{user.id}` · Имя: {user.full_name or '—'}",
            parse_mode="Markdown",
            reply_markup=markup,
        )
        logger.info("User %s registered as %s", user.id, role)
        return

    if data == "change_role":
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👨‍👩‍👦 Родитель", callback_data="register_parent"),
                InlineKeyboardButton("🧑‍🏫 Репетитор", callback_data="register_tutor"),
            ],
            [
                InlineKeyboardButton("👨‍💼 Координатор", callback_data="register_coordinator"),
            ],
        ])
        await query.edit_message_text("Выберите новую роль:", reply_markup=kb)
        return

    # --- Помощь «📋 Команды» по требованию (UX U4) ---
    if data in ("help_commands", "help_back"):
        user_rec = await UserRepository().get_by_telegram_id(str(query.from_user.id))
        role = (user_rec or {}).get("role", "parent")
        back_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("« Назад", callback_data="help_back"),
        ]])
        if data == "help_commands":
            if role == "coordinator":
                await query.edit_message_text(
                    _coordinator_help_text(), parse_mode="Markdown", reply_markup=back_kb)
            else:
                await query.edit_message_text(
                    f"Ваши возможности:\n\n{_role_expectations(role)}\n\n"
                    "Команды также доступны в меню «/» слева от поля ввода.",
                    reply_markup=back_kb,
                )
            return
        # help_back — возвращаем приветствие
        user = query.from_user
        emoji = {"parent": "👨‍👩‍👦", "tutor": "🧑‍🏫", "coordinator": "👨‍💼", "student": "🎓"}.get(role, "")
        admin_mark = " ★" if is_admin(user.id) else ""
        buttons = [InlineKeyboardButton("🔄 Сменить роль", callback_data="change_role")]
        if role == "coordinator":
            buttons.insert(0, InlineKeyboardButton("📋 Команды", callback_data="help_commands"))
        await query.edit_message_text(
            f"👋 С возвращением, {user.full_name or '—'}!\n\n"
            f"Ваша роль: {emoji} {role}{admin_mark}\n"
            f"TG ID: `{user.id}`\n\n"
            f"{_role_expectations(role)}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([buttons]),
        )
        return

    # --- Подтверждение опасных действий (UX U3) ---
    if data in ("demo_reset:confirm", "demo_reset:cancel"):
        if not is_admin(query.from_user.id):
            await query.answer("⛔ Только владелец/админ", show_alert=True)
            return
        if data.endswith(":cancel"):
            await query.edit_message_text("✖️ Сброс отменён — данные на месте.")
            return
        from src.bot.pilot import perform_demo_reset, format_demo_reset_result
        counts = await perform_demo_reset()
        await query.edit_message_text(format_demo_reset_result(counts))
        return

    if data.startswith("killswitch:"):
        if not is_admin(query.from_user.id):
            await query.answer("⛔ Только владелец/админ", show_alert=True)
            return
        try:
            lvl = int(data.split(":", 1)[1])
            if lvl not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await query.edit_message_text("Не смог прочитать нажатие — попробуйте ещё раз.")
            return
        set_kill_switch_level(lvl)
        labels = {0: "🔴 ВСЁ ВЫКЛ", 1: "🟡 Только координаторам", 2: "🟢 Полностью"}
        await query.edit_message_text(f"🔌 Kill Switch: {labels[lvl]}")
        logger.info("Kill switch set to %d via button by %s", lvl, query.from_user.id)
        await bus.publish(Event(EventTypes.SYSTEM_KILL_SWITCH, {"level": lvl}))
        return

    # --- Выбор роли в демо-режиме ---
    if data == "role_coordinator":
        await _ensure_role(upd, "coordinator")
        await apply_command_menu(getattr(_ctx, "bot", None), upd.effective_user.id, "coordinator")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚨 Ученик отсутствует", callback_data="demo_absent"),
            InlineKeyboardButton("📊 Отчёт о сессии", callback_data="demo_report"),
        ]])
        await query.edit_message_text(
            "👨‍💼 *Вы в роли координатора.*\n\n"
            "Нажмите кнопку, чтобы запустить демо-сценарий.",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    if data == "role_parent":
        await _ensure_role(upd, "parent")
        await apply_command_menu(getattr(_ctx, "bot", None), upd.effective_user.id, "parent")
        await query.edit_message_text(
            "👨‍👩‍👦 *Вы в роли родителя.*\n\n"
            "В демо-режиме родительские уведомления симулируются.\n"
            "Попросите координатора запустить сценарий.",
            parse_mode="Markdown",
        )
        return

    if data == "demo_absent":
        await _demo_solo_absence(upd, _ctx)
        return

    if data == "demo_report":
        if not settings.albion_demo_mode:
            await query.edit_message_text("Демо-режим выключен.")
            return
        await _show_demo_report(upd, _ctx)
        return

    # --- Демо: ответ родителя на кнопки ---
    if data.startswith("demo_resolve:"):
        parts = data.split(":")
        if len(parts) < 4:
            await query.edit_message_text("Не смог прочитать нажатие — попробуйте ещё раз или напишите текстом.")
            return
        try:
            inc_id = int(parts[1])
            wid = int(parts[2])
        except (IndexError, ValueError):
            await query.edit_message_text("Не смог прочитать нажатие — попробуйте ещё раз или напишите текстом.")
            return
        action = parts[3]
        demo_labels = {
            "ok": ("parent_ok", "Всё хорошо"),
            "no": ("parent_not_coming", "Не придём"),
            "late": ("parent_late", "Опоздаем"),
        }
        resolution, parent_answer = demo_labels.get(action, ("parent_confirmed", "Ответ получен"))

        # Убираем кнопки с сообщения
        await query.edit_message_reply_markup(None)

        # Используем основную логику workflow, чтобы не дублировать отмену задач.
        wf = AbsenceWorkflow()
        await wf.resolve_absence(inc_id, str(query.from_user.id), resolution=resolution)
        logger.info("Demo resolved: inc=%d wf=%d action=%s", inc_id, wid, action)

        # Редактируем "Ждём ответ..." на ответ родителя
        waiting_msg_id = _demo_waiting_messages.get(chat_id)
        if waiting_msg_id:
            try:
                await _ctx.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=waiting_msg_id,
                    text=f"✔ Родитель ответил: {parent_answer}.",
                )
            except Exception:
                pass  # сообщение могло быть удалено

        await asyncio.sleep(1.0)
        await upd.effective_chat.send_message("📚 Уведомляю преподавателя и координатора...")
        await asyncio.sleep(1.0)
        await upd.effective_chat.send_message(f"✅ Ситуация закрыта. Ответ родителя: {parent_answer}.")
        return

    # --- Отмена занятия кнопкой со списка (UX U6) ---
    if data.startswith("cancel_class:"):
        class_id = data.split(":", 1)[1]
        if not class_id:
            await query.edit_message_text("Не смог прочитать нажатие — попробуйте ещё раз.")
            return
        await _ensure_user(upd, "parent")
        await bus.publish(Event(EventTypes.LESSON_CANCELLED, {
            "lesson_id": class_id,
            "reason": "не указана (отмена кнопкой)",
            "reported_by": str(query.from_user.id),
        }))
        from src.db.repository import MeritHubClassRepository
        from src.workflows.lesson_ops import _format_class_label
        cls = await MeritHubClassRepository().get(class_id)
        label = _format_class_label(class_id, (cls or {}).get("start_time"))
        await query.edit_message_text(
            f"🔄 Отмена {label} передана репетитору и координаторам.")
        logger.info("Cancel via button: class=%s by=%s", class_id, query.from_user.id)
        return

    # --- Координатор закрывает ситуацию прямо с эскалации (UX U2) ---
    if data.startswith("coord_resolve:"):
        parts = data.split(":")
        try:
            inc_id = int(parts[1])
        except (IndexError, ValueError):
            await query.edit_message_text("Не смог прочитать нажатие — попробуйте ещё раз или напишите текстом.")
            return
        # Guard: закрывать может только координатор (callback_data виден всем,
        # кто перешлёт сообщение — проверяем роль из БД, а не доверяем кнопке).
        user_rec = await UserRepository().get_by_telegram_id(str(query.from_user.id))
        if not user_rec or user_rec.get("role") != "coordinator":
            await query.answer("⛔ Закрывать ситуации может только координатор", show_alert=True)
            return
        inc_repo = IncidentRepository()
        inc = await inc_repo.get(inc_id)
        if not inc:
            await query.answer("Ситуация не найдена", show_alert=True)
            return
        if inc["status"] == "resolved":
            await query.answer("ℹ️ Уже закрыта", show_alert=False)
            try:
                await query.edit_message_reply_markup(None)
            except Exception:
                pass
            return
        wf = AbsenceWorkflow()
        await wf.resolve_absence(inc_id, str(query.from_user.id), resolution="coordinator_closed")
        # Редактируем исходное сообщение, сохраняя контекст + отметку кто закрыл.
        base_text = getattr(getattr(query, "message", None), "text", "") or ""
        suffix = f"\n\n✅ Закрыто ({query.from_user.full_name or query.from_user.id})"
        try:
            await query.edit_message_text((base_text + suffix)[:4000])
        except Exception:
            await query.answer("✅ Закрыто", show_alert=False)
        logger.info("Incident %d closed by coordinator %s via escalation button", inc_id, query.from_user.id)
        return

    # --- Реальный resolve (из уведомления) ---
    if data.startswith("resolve:"):
        parts = data.split(":")
        try:
            inc_id = int(parts[1])
            nonce = parts[2]
        except (IndexError, ValueError):
            await query.edit_message_text("Не смог прочитать нажатие — попробуйте ещё раз или напишите текстом.")
            return
        action = parts[3] if len(parts) > 3 else "ok"

        idem_key = f"tg_callback:{data}"
        idem = IdempotencyRepository()
        if await idem.exists(idem_key):
            await query.answer("✅ Уже обработано", show_alert=False)
            return

        # Проверяем статус инцидента ДО обработки
        inc_repo = IncidentRepository()
        inc = await inc_repo.get(inc_id)
        if not inc:
            await query.edit_message_text("Ситуация не найдена.")
            return

        if inc["status"] == "resolved":
            # Инцидент уже закрыт (через другую кнопку, /ok или free text)
            await query.answer("ℹ️ Эта ситуация уже закрыта", show_alert=False)
            try:
                await query.edit_message_reply_markup(None)
            except Exception:
                pass
            return

        was_escalated = inc["status"] == "escalated"

        wf_repo = WorkflowRepository()
        wf_row = await wf_repo._fetchone(
            "SELECT * FROM workflow_instances WHERE data LIKE ? ORDER BY id DESC LIMIT 1",
            (f'%\"incident_id\": {inc_id}%',),
        )
        if not wf_row:
            await query.edit_message_text("Ситуация уже закрыта или workflow не найден.")
            return
        try:
            wf_data = json.loads(wf_row.get("data") or "{}")
        except Exception:
            wf_data = {}
        expected_nonce = wf_data.get("parent_callback_nonce")
        if expected_nonce and expected_nonce != nonce:
            await query.answer("⛔ Кнопка устарела", show_alert=True)
            return

        action_map = {
            "ok": ("parent_ok", "✅ Всё в порядке! Спасибо."),
            "no": ("parent_not_coming", "❌ Спасибо! Отметили, что сегодня занятия не будет."),
            "late": ("parent_late", "⏰ Спасибо! Отметили, что ученик опоздает."),
        }
        resolution, parent_ack = action_map.get(action, ("parent_confirmed", "✅ Ответ получен."))

        wf = AbsenceWorkflow()
        await wf.resolve_absence(inc_id, str(query.from_user.id), resolution=resolution)
        outcome = "ok" if action == "ok" else ("no_show" if action == "no" else ("late" if action == "late" else "free_text"))
        await wf.notify_coordinators_parent_reply(
            inc_id,
            outcome,
            parent_telegram_id=str(query.from_user.id),
        )

        # Если эскалация уже ушла координатору — сообщаем об этом родителю
        if was_escalated:
            parent_ack += "\n\nℹ️ Координатор уже был уведомлён об отсутствии ответа. Ваш ответ передан — инцидент закрыт."

        await idem.save(idem_key, "telegram_callback", response="resolved")
        # Также сохраняем idempotency для ВСЕХ кнопок этого инцидента,
        # чтобы другие кнопки не сработали повторно
        for other_action in ("ok", "no", "late"):
            if other_action != action:
                other_key = f"tg_callback:resolve:{inc_id}:{nonce}:{other_action}"
                await idem.save(other_key, "telegram_callback_blocked", response="blocked_by_resolve")

        # UX U5: имя ученика вместо голого номера + без серверного времени
        # (родителю важно ЧТО он подтвердил, а не id инцидента и чужой часовой пояс).
        student_label = wf_data.get("student_name") or "Ученик"
        await query.edit_message_text(f"{parent_ack}\nОтметили: {student_label} · ситуация #{inc_id} закрыта.")
        logger.info("Incident %d resolved via button action=%s (was_escalated=%s)", inc_id, action, was_escalated)
        return

    if data.startswith("checkin:"):
        parts = data.split(":")
        try:
            wid = int(parts[1])
            nonce = parts[2]
            action = parts[3]
        except (IndexError, ValueError):
            await query.edit_message_text("Не смог прочитать нажатие — попробуйте ещё раз или напишите текстом.")
            return
        idem_key = f"tg_callback:{data}"
        idem = IdempotencyRepository()
        if await idem.exists(idem_key):
            await query.answer("✅ Уже обработано", show_alert=False)
            return

        repo = WorkflowRepository()
        wf_row = await repo.get(wid)
        if not wf_row or wf_row.get("state") != "running":
            await query.edit_message_text("Этот сценарий уже завершён.")
            return
        try:
            wf_data = json.loads(wf_row.get("data") or "{}")
        except Exception:
            wf_data = {}
        expected_nonce = wf_data.get("nonce")
        if expected_nonce and expected_nonce != nonce:
            await query.answer("⛔ Кнопка устарела", show_alert=True)
            return

        ops = LessonOpsWorkflow()
        await ops.record_checkin_response(wid, actor_tg=str(query.from_user.id), action=action)
        await idem.save(idem_key, "telegram_checkin", response=action)
        ack_map = {
            "ready": "✅ Статус принят.",
            "late": "⏰ Спасибо, отметили опоздание.",
            "no_show": "❌ Спасибо, отметили отсутствие.",
            "tech": "🛠 Спасибо, отметили техпроблему.",
            "class_started": "✅ Отлично, старт урока зафиксирован.",
            "student_absent": "👤 Спасибо, отметили отсутствие ученика.",
        }
        await query.edit_message_text(ack_map.get(action, "✅ Ответ принят."))
        return

    await query.edit_message_text("Не понял действие — попробуйте ещё раз или напишите текстом.")


# =====================================================================
# DEMO: отчёт о сессии
# =====================================================================

async def _show_demo_report(upd: Update, _ctx) -> None:
    """Формирует отчёт с реальными метриками из БД."""
    repo = IncidentRepository()
    closed = await repo._fetchone("SELECT COUNT(*) as cnt FROM incidents WHERE status='resolved'")
    closed_cnt = closed["cnt"] if closed else 0

    if closed_cnt == 0:
        await upd.effective_chat.send_message(
            "🎬 *Демо-сессия*\n\n"
            "Сессия новая или бот был перезапущен. Запустите демо-сценарий.",
            parse_mode="Markdown",
        )
        return

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
    text = upd.message.text or ""
    tg_id = str(upd.effective_user.id)
    if not text.strip():
        return

    # UX U7: незнакомцу — выбор роли, а не тихая регистрация «в родители».
    # Иначе репетитор, начавший с текста, получал parent-роль, и его ответы
    # интерпретировались чужой эвристикой (невидимое системное состояние).
    user_rec = await UserRepository().get_by_telegram_id(tg_id)
    if not user_rec:
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👨‍👩‍👦 Я родитель", callback_data="register_parent"),
                InlineKeyboardButton("🧑‍🏫 Я репетитор", callback_data="register_tutor"),
            ],
            [
                InlineKeyboardButton("👨‍💼 Я координатор", callback_data="register_coordinator"),
            ],
        ])
        await upd.message.reply_text(
            "👋 Вы здесь впервые! Выберите вашу роль — и повторите сообщение.",
            reply_markup=kb,
        )
        return

    logger.info("Msg from %s: %s", upd.effective_user.id, text[:100])

    # Parent/tutor pre-lesson check-ins: сначала пытаемся понять, не ответ ли это
    # на reminder/start-check workflow.
    ops = LessonOpsWorkflow()
    role = (user_rec or {}).get("role")

    if role == "parent":
        active_checkin = await ops.find_active_checkin(tg_id, ("parent",))
        if active_checkin:
            wid, _data, _wf_type = active_checkin
            interpreted = await llm_client.interpret_parent_reply(text)
            status = interpreted.get("status", "other")
            action = "ready" if status == "ok" else (status if status in {"late", "no_show"} else "other")
            await ops.record_checkin_response(wid, actor_tg=tg_id, action=action, free_text=text)
            reply_map = {
                "ready": "✅ Спасибо! Отметили, что всё в порядке.",
                "no_show": "❌ Спасибо! Отметили, что сегодня занятия не будет. Координатор уведомлён.",
                "late": "⏰ Спасибо! Отметили, что ученик опоздает. Координатор уведомлён.",
                "other": "💬 Спасибо! Передали ответ координатору для ручной обработки.",
            }
            await upd.message.reply_text(reply_map.get(action, reply_map["other"]))
            return

    if role == "tutor":
        active_checkin = await ops.find_active_checkin(tg_id, ("tutor", "tutor_start"))
        if active_checkin:
            wid, data, _wf_type = active_checkin
            actor_type = data.get("actor_type")
            if actor_type == "tutor_start":
                # Для tutor_start используем кнопки (callback), а не free-text.
                # Free-text сюда попадает только если репетитор пишет вместо нажатия кнопки.
                # Не делаем auto-detect student_absent из текста — слишком опасно (ложные срабатывания).
                # Передаём в координатор для ручной обработки.
                await ops.record_checkin_response(wid, actor_tg=tg_id, action="other", free_text=text)
                await upd.message.reply_text(
                    "💬 Спасибо! Передали ответ координатору для ручной обработки.\n"
                    "Для быстрого ответа используйте кнопки из предыдущего сообщения."
                )
                return
            else:
                interpreted = await llm_client.interpret_tutor_reply(text)
                action = interpreted.get("status", "other")
            await ops.record_checkin_response(wid, actor_tg=tg_id, action=action, free_text=text)
            reply_map = {
                "ready": "✅ Спасибо! Отметили, что вы готовы.",
                "late": "⏰ Спасибо! Отметили, что вы опоздаете. Координатор уведомлён.",
                "no_show": "❌ Спасибо! Отметили, что урок не состоится. Координатор уведомлён.",
                "tech": "🛠 Спасибо! Техпроблема зафиксирована. Координатор уведомлён.",
                "other": "💬 Спасибо! Передали ответ координатору для ручной обработки.",
            }
            await upd.message.reply_text(reply_map.get(action, "✅ Ответ принят."))
            return

    # Если это родитель и у него есть активный инцидент по неявке — считаем,
    # что это ответ на отсутствие. Пытаемся понять его через LLM/эвристику.
    if role == "parent":
        wf = AbsenceWorkflow()
        active = await wf.find_active_incident_for_parent(tg_id)
        if active:
            inc_id, _wf_data = active
            interpreted = await llm_client.interpret_parent_reply(text)
            status = interpreted.get("status", "other")
            resolution_map = {
                "ok": "parent_ok",
                "no_show": "parent_not_coming",
                "late": "parent_late",
                "other": "parent_text_reply",
            }
            reply_map = {
                "ok": "✅ Спасибо! Отметили, что всё в порядке. Координатор уведомлён.",
                "no_show": "❌ Спасибо! Отметили, что сегодня занятия не будет. Координатор уведомлён.",
                "late": "⏰ Спасибо! Отметили, что ученик опоздает. Координатор уведомлён.",
                "other": "💬 Спасибо! Передали ответ координатору для ручной обработки.",
            }
            await wf.resolve_absence(inc_id, tg_id, resolution=resolution_map.get(status, "parent_text_reply"))
            await wf.notify_coordinators_parent_reply(
                inc_id,
                status if status in {"ok", "no_show", "late"} else "free_text",
                parent_text=text,
                parent_telegram_id=tg_id,
            )
            await upd.message.reply_text(reply_map.get(status, reply_map["other"]))
            return

        # Нет активного инцидента, но может быть недавний эскалированный?
        # Если да — всё равно передаём текст координатору (поздний ответ).
        escalated = await wf.find_escalated_incident_for_parent(tg_id)
        if escalated:
            inc_id, _wf_data = escalated
            await wf.notify_coordinators_parent_reply(
                inc_id,
                "free_text",
                parent_text=text,
                parent_telegram_id=tg_id,
            )
            await upd.message.reply_text(
                "💬 Спасибо за ответ! Координатор уже был уведомлён ранее, "
                "но ваш ответ передан для учёта."
            )
            return

    await bus.publish(Event(EventTypes.MESSAGE_INCOMING, {
        "text": text, "telegram_id": tg_id, "chat_id": str(upd.effective_chat.id),
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
    app.add_handler(CommandHandler("cancel_lesson", cmd_cancel_lesson))
    app.add_handler(CommandHandler("ok", cmd_ok))
    register_role_handlers(app)  # /whoami /role /roles — раздача ролей владельцами
    register_pilot_handlers(app)  # /pilot_seed /pilot_absent — прогон сценария на живых аккаунтах
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    async def notif_handler(event: Event):
        tg = event.data.get("telegram_id")
        msg = event.data.get("message", "")
        cb_data = event.data.get("callback_data")
        buttons = event.data.get("buttons") or []
        if not tg or not msg:
            return
        if not await can_send_async(tg):
            logger.info("Kill switch blocked msg to %s", tg)
            return
        last_error = None
        for attempt in range(3):
            try:
                reply_markup = None
                if buttons:
                    # Кнопка бывает двух видов: callback (действие) и url (ссылка,
                    # например tg://user?id= «написать родителю»). Ровно одно из двух.
                    reply_markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            btn["text"],
                            callback_data=btn.get("callback_data"),
                            url=btn.get("url"),
                        )] for btn in buttons
                    ])
                elif cb_data:
                    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Всё в порядке", callback_data=cb_data)]])
                if reply_markup:
                    await app.bot.send_message(chat_id=tg, text=msg, reply_markup=reply_markup)
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
    if settings.albion_demo_mode:
        bus.subscribe(EventTypes.SCHEDULER_TICK, _demo_tick_handler)
    logger.info("Bot handlers registered (kill_switch=%d, demo=%s)", _kill_switch_level, settings.albion_demo_mode)
