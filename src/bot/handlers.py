"""Telegram bot — команды, inline кнопки, kill switch, demo-data seed."""

import asyncio
import json
import logging
import re
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
from src.integrations.factory import get_airtable_service
from src.workflows.engine import engine
from src.workflows.absence import AbsenceWorkflow
from src.workflows.lesson_ops import LessonOpsWorkflow
from src.bot.roles import register_role_handlers, get_coordinator_ids, is_admin, is_coordinator_or_admin, apply_command_menu
from src.bot.pilot import register_pilot_handlers
from src.bot.wizard import (
    cmd_schedule, cmd_add_student, cmd_add_tutor, handle_wz_callback, try_handle_wz_text,
)

logger = logging.getLogger(__name__)

_kill_switch_level = 2

# Храним ID сообщения "Ждём ответ..." для демо-сценария (chat_id -> message_id)
_demo_waiting_messages: dict[int, int] = {}


async def can_send_async(telegram_id: str) -> bool:
    """Проверка с доступом к БД и kill switch."""
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


async def _forward_to_coordinator(upd: Update, text: str, tg_id: str, user_rec: dict | None = None) -> None:
    """При ошибке обработки free-text — пересылаем координатору, уведомляем родителя."""
    from src.bot.roles import notify_all_coordinators
    name = (user_rec or {}).get("name") or upd.effective_user.full_name or tg_id
    await notify_all_coordinators(
        f"💬 Сообщение от {name} (tg {tg_id})\n\n«{text[:500]}»\n\n(Автоматическая обработка не удалась)",
        notification_type="ops_alert",
    )
    await upd.message.reply_text(
        "💬 Спасибо! Ваше сообщение передано координатору для ручной обработки.")


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

def _coordinator_help_text(admin: bool = False) -> str:
    """Единый список команд координатора (было два расходящихся списка —
    в /start и в регистрации по кнопке). Underscore в командах экранируем
    для Markdown V1, иначе парные '_' ломают отображение.

    Порядок — по частоте использования (UX-аудит П2): сначала ежедневные
    визарды и обзор, технические mh_* — свёрнуты в «Служебные».

    R9-4: секции «Демо»/«Владельцу»/«Служебные» показываются ТОЛЬКО админам —
    иначе UI обещает команды, которые бэкенд отвергает («⛔ Только владелец/админ»)."""
    text = (
        "📋 *Ваши команды:*\n\n"
        "*Расписание:*\n"
        "/schedule — новое занятие (пошагово)\n"
        "/add\\_student — новый ученик\n"
        "/add\\_tutor — новый репетитор\n\n"
        "*Обзор:*\n"
        "/today — занятия сегодня\n"
        "/morning — утренняя сводка (приходит автоматически в 09:00)\n"
        "/incidents — инциденты и статистика\n"
        "/lessons — ближайшие занятия и ссылки\n"
        "/status — состояние системы\n\n"
        "*Инциденты:*\n"
        "/ok <ID> — закрыть инцидент\n"
        "/cancel\\_lesson <ID> — отмена по ID (обычно не нужно — родители отменяют сами)\n"
    )
    if admin:
        text += (
            "\n*Демо:*\n"
            "/pilot\\_absent — тест: сценарий неявки\n"
            "/demo\\_reset — сброс между прогонами\n\n"
            "*Владельцу:*\n"
            "/kill\\_switch — аварийный стоп/ограничение рассылок\n"
            "/roles — участники и роли\n"
            "/leads — заявки (последние 10 + счётчик)\n\n"
            "*Служебные (MeritHub):*\n"
            "/seed10 <parentTG> — создать 10 учеников\n"
            "/mh\\_schedule <tutor> <start> <min> <students...>\n"
            "/mh\\_tutor <cuid> <tg> <имя>\n"
            "/mh\\_students — список учеников\n"
            "/mh\\_events — последние webhook'и"
        )
    else:
        text += "\nПолный список команд владельца — в его меню."
    return text


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
            "Ближайшие уроки и ссылки: /lessons\n"
            "Отменить урок: /cancel_lesson"
        )
    # parent (и дефолт)
    return (
        "Дальше ничего настраивать не нужно:\n"
        "• перед занятием напомню и уточню статус;\n"
        "• если ученик не придёт — спрошу у вас.\n\n"
        "Мои занятия и ссылки: /lessons\n"
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
            f"Ваша роль: {emoji} {role}{admin_mark}\n\n"
            f"{_role_expectations(role)}",
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
        "👋 *Добро пожаловать в ALBION!*\n\n"
        "Я помогаю координировать занятия: напоминания, ссылки, статусы.\n\n"
        "Пожалуйста, выберите вашу роль:",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def cmd_status(upd: Update, _ctx) -> None:
    labels = {0: "🔴 Всё остановлено", 1: "🟡 Только алерты координаторам", 2: "🟢 Всё работает"}
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
    text = (
        f"✅ *ALBION*\nВремя: {datetime.now():%H:%M:%S}\nРоль: {role}\nAI: {ai}\nОжидает: {cnt} задач{ks_info}"
    )
    # Техсекция для владельца (перенесена из /today — П5 аудита: интерьеры
    # движка не должны мешать продуктовому обзору занятий).
    if is_admin(upd.effective_user.id):
        pending = await sched._fetchall(
            "SELECT * FROM scheduled_actions WHERE status='pending' ORDER BY execute_at LIMIT 8")
        if pending:
            text += "\n\n⏰ *Ожидающие действия:*"
            for a in pending:
                text += f"\n  [{a['action']}] → {(a.get('execute_at') or '—')[:19]} | wf#{a['workflow_id']}"
        active_wf = await WorkflowRepository()._fetchall(
            "SELECT * FROM workflow_instances WHERE state='running' ORDER BY id DESC LIMIT 5")
        if active_wf:
            text += "\n\n⚙️ *Активные workflow:*"
            for w in active_wf:
                text += f"\n  #{w['id']} [{w['workflow_type']}]"
    await upd.message.reply_text(text, parse_mode="Markdown")


def _next_occurrence_in_days(class_row: dict, days: int, now) -> tuple | None:
    """Ближайший occurrence класса в окне `days` дней → (date, 'HH:MM') или None.

    Общий сканер для веток /lessons (tutor/coordinator): учитывает, что
    сегодняшнее занятие, которое уже началось, пропускается."""
    from src.utils.recurrence import class_occurs_on
    hhmm = (class_row.get("start_time") or "")[11:16] or "00:00"
    today = now.date()
    for i in range(days):
        d = today + timedelta(days=i)
        if not class_occurs_on(class_row, d):
            continue
        if i == 0 and hhmm <= now.strftime("%H:%M"):
            continue
        return d, hhmm
    return None


async def cmd_lessons(upd: Update, _ctx) -> None:
    """«Мои занятия»: ближайшие занятия + постоянные ссылки на комнату (П4 аудита).

    parent — по своим зачислениям (participant_link, occurrence-aware, 14 дней).
    tutor  — по карточке контакта TG→client_user_id (host_link).
    Ссылки MeritHub постоянные для класса, поэтому перевыдача = просто показать."""
    tg = str(upd.effective_user.id)
    user_rec = await UserRepository().get_by_telegram_id(tg)
    role = (user_rec or {}).get("role", "parent")

    from src.db.repository import (
        MeritHubClassRepository, MeritHubContactRepository, MeritHubEnrollmentRepository,
    )
    from src.integrations.factory import get_merithub_service
    from src.utils.i18n import lang_of, tr
    from src.utils.recurrence import org_now, org_zone_label
    from src.workflows.cancellation import upcoming_lessons_for_parent

    client = get_merithub_service()
    lang = await lang_of(tg) if role == "tutor" else "ru"

    if role == "tutor":
        contact = await MeritHubContactRepository().get_by_telegram(tg)
        if not contact:
            await upd.message.reply_text(tr("lessons_empty", lang))
            return
        crepo = MeritHubClassRepository()
        classes = await crepo._fetchall(
            "SELECT * FROM merithub_classes WHERE tutor_client_user_id=?",
            (contact["client_user_id"],))
        now = org_now()
        items = []
        for c in classes:
            occ = _next_occurrence_in_days(c, 14, now)
            if occ:
                items.append((occ[0], occ[1], c))
        items.sort(key=lambda x: (x[0], x[1]))
        if not items:
            await upd.message.reply_text(tr("lessons_empty", lang))
            return
        from src.utils.recurrence import WD_EN
        erepo = MeritHubEnrollmentRepository()
        by_cid = await erepo.list_by_classes([c["class_id"] for _, _, c in items[:10]])
        lines = [tr("lessons_header_tutor", lang, org=org_zone_label())]
        buttons = []
        for d, hhmm, c in items[:10]:
            enr = [e for e in by_cid.get(c["class_id"], [])
                   if (e.get("role") or "student") == "student"]
            names = ", ".join(e.get("student_name") or "" for e in enr[:3]) or "—"
            wd = WD_EN[(d.weekday() + 1) % 7]
            lines.append(f"• {wd} {d:%d.%m}, {hhmm} — {names}")
            link = c.get("host_link")
            if link:
                buttons.append([InlineKeyboardButton(
                    f"🔗 {wd} {d:%d.%m}, {hhmm}"[:64], url=client.room_url(link))])
        lines.append("")
        lines.append(tr("lessons_link_hint", lang))
        await upd.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
        return

    if role == "coordinator" or is_admin(upd.effective_user.id):
        # Координатору — вся организация (его «личных» зачислений нет:
        # parent-ветка вводила бы в заблуждение «вас не добавили»).
        from src.utils.recurrence import WD_RU, mh_weekday
        crepo = MeritHubClassRepository()
        now = org_now()
        items = []
        for c in await crepo.list_all():
            occ = _next_occurrence_in_days(c, 7, now)
            if occ:
                items.append((occ[0], occ[1], c))
        items.sort(key=lambda x: (x[0], x[1]))
        if not items:
            await upd.message.reply_text(
                "📚 Ближайших занятий нет (7 дней). Создать: /schedule")
            return
        erepo = MeritHubEnrollmentRepository()
        by_cid = await erepo.list_by_classes([c["class_id"] for _, _, c in items[:12]])
        lines = [f"📚 Ближайшие занятия организации (7 дней, время — {org_zone_label()}):"]
        buttons = []
        for d, hhmm, c in items[:12]:
            enr = [e for e in by_cid.get(c["class_id"], [])
                   if (e.get("role") or "student") == "student"]
            names = ", ".join(e.get("student_name") or "" for e in enr[:3]) or "—"
            wd = WD_RU[mh_weekday(d)]
            title = c.get("title") or c["class_id"]
            lines.append(f"• {wd} {d:%d.%m}, {hhmm} — {title} · 👥 {names}")
            link = c.get("participant_link")
            if link:
                buttons.append([InlineKeyboardButton(
                    f"🔗 {wd} {d:%d.%m}, {hhmm} — {title}"[:64], url=client.room_url(link))])
        lines.append("")
        lines.append("Ссылки — участнические (для пересылки родителям при потере).")
        await upd.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)
        return

    # parent (и любая другая роль — безопасный дефолт)
    lessons = await upcoming_lessons_for_parent(tg, limit=10)
    if not lessons:
        await upd.message.reply_text(tr("lessons_empty", "ru"))
        return
    crepo = MeritHubClassRepository()
    class_map = await crepo.get_many([l["class_id"] for l in lessons])
    lines = [tr("lessons_header_parent", "ru")]
    buttons = []
    for l in lessons:
        lines.append(f"• {l['label']} — {l['student_name']}")
        c = class_map.get(l["class_id"])
        link = (c or {}).get("participant_link")
        if link:
            buttons.append([InlineKeyboardButton(
                f"🔗 {l['student_name']} — {l['label']}"[:64], url=client.room_url(link))])
    lines.append("")
    lines.append(tr("lessons_link_hint", "ru"))
    await upd.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


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
    at = get_airtable_service()
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
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ может менять kill switch.")
        return
    if not _ctx.args:
        # UX U3: уровни кнопками вместо запоминания 0|1|2 (recognition, не recall).
        # П10: единые человеческие подписи (как в /status и callback-кнопках).
        labels = {0: "🔴 Всё остановлено", 1: "🟡 Только алерты координаторам", 2: "🟢 Всё работает"}
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 Всё остановлено", callback_data="killswitch:0"),
        ], [
            InlineKeyboardButton("🟡 Только алерты координаторам", callback_data="killswitch:1"),
        ], [
            InlineKeyboardButton("🟢 Всё работает", callback_data="killswitch:2"),
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
    labels = {0: "Всё остановлено", 1: "Только алерты координаторам", 2: "Всё работает"}
    await upd.message.reply_text(f"🔌 Kill Switch: {labels[lvl]}")
    logger.info("Kill switch set to %d", lvl)


async def cmd_cancel_lesson(upd: Update, _ctx) -> None:
    """Отмена урока: /cancel_lesson — персональный список кнопками (основной путь
    для родителей), /cancel_lesson <ID> [причина...] — legacy-путь по голому ID
    (для координаторов и критических случаев из прошлых инструкций).

    Персональный список — только занятия этого родителя (audit П1: раньше
    показывались первые 5 классов ВСЕЙ организации)."""
    if not _ctx.args:
        from src.workflows.cancellation import upcoming_lessons_for_parent
        tg = str(upd.effective_user.id)
        await _ensure_user(upd, "parent")
        lessons = await upcoming_lessons_for_parent(tg, limit=5)
        if not lessons:
            await upd.message.reply_text(
                "Не вижу ваших ближайших занятий.\n"
                "Чтобы отменить — напишите координатору, пожалуйста.")
            return
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"❌ {l['student_name']} — {l['label']}"[:64],
                callback_data=f"cancel_class:{l['class_id']}:{l['date']}",
            )
        ] for l in lessons])
        await upd.message.reply_text(
            "Какое занятие отменяем?\n\n"
            "Если его нет в списке — напишите координатору.",
            reply_markup=kb)
        return
    lid = _ctx.args[0]
    reason = " ".join(_ctx.args[1:]) or "не указана"
    await _ensure_user(upd, "parent")

    # Проверяем, что урок существует: merithub_classes (реальные занятия)
    # или airtable (демо-уроки). Ветки merithub.get_lesson больше нет (R7-10).
    from src.db.repository import MeritHubClassRepository
    class_row = await MeritHubClassRepository().get(lid)
    if not class_row:
        from src.workflows.cancellation import CancellationWorkflow
        lesson = await CancellationWorkflow().airtable.get_lesson(lid)
        if not lesson:
            await upd.message.reply_text(
                f"❌ Урок {lid} не найден в расписании. "
                "Проверьте ID (/today) или напишите координатору."
            )
            return

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
    if not await is_coordinator_or_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только координатор/админ может закрывать ситуации.")
        return
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
    # R9-5: закрыл координатор — история должна говорить «координатором»,
    # а не дефолтным «подтверждено родителем» (семантическая ложь в /incidents).
    await wf.resolve_absence(iid, str(upd.effective_user.id), resolution="coordinator_closed")
    await upd.message.reply_text(f"Спасибо! Ситуация #{iid} закрыта!")


# =====================================================================
# CALLBACK QUERY HANDLER
# =====================================================================

async def handle_callback(upd: Update, _ctx) -> None:
    query = upd.callback_query
    # Визарды координатора (/schedule, /add_student, /add_tutor) обрабатывают
    # свои callback'и сами — включая ответ на query (answer с toast-обратной связью).
    if query.data and query.data.startswith("wz:"):
        await handle_wz_callback(upd, _ctx)
        return
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
            # Уведомляем координаторов о новом пользователе
            try:
                from src.bot.roles import notify_all_coordinators
                ru_role = {"parent": "родитель", "tutor": "репетитор", "coordinator": "координатор"}.get(role, role)
                uname = f"Username: @{user.username}\n" if user.username else ""
                await notify_all_coordinators(
                    "👋 Новый пользователь зарегистрировался\n"
                    f"Имя: {user.full_name or '—'}\n"
                    f"Роль: {ru_role}\n"
                    f"TG ID: {user.id}\n"
                    f"{uname}",
                    notification_type="ops_alert",
                )
            except Exception as _e:
                logger.warning("Coordinator notify on register failed: %s", _e)
        # Язык интерфейса по роли (i18n): тьюторы — англоговорящие, остальные — RU.
        await repo.set_language(str(user.id), "en" if role == "tutor" else "ru")
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
        # П10: человеческое имя роли вместо внутреннего кода (parent/coordinator)
        role_ru = {"parent": "родитель", "tutor": "репетитор", "coordinator": "координатор"}.get(role, role)
        await query.edit_message_text(
            f"✅ Вы зарегистрированы как {emoji} {role_ru}{admin_mark}.\n\n"
            f"{_role_expectations(role)}",
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
                    _coordinator_help_text(is_admin(query.from_user.id)),
                    parse_mode="Markdown", reply_markup=back_kb)
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
            f"Ваша роль: {emoji} {role}{admin_mark}\n\n"
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
        labels = {0: "🔴 Всё остановлено", 1: "🟡 Только алерты координаторам", 2: "🟢 Всё работает"}
        await query.edit_message_text(f"🔌 Kill Switch: {labels[lvl]}")
        logger.info("Kill switch set to %d via button by %s", lvl, query.from_user.id)
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
    # callback формата cancel_class:{class_id}:{occ_date} — дата опциональна.
    if data.startswith("cancel_class:"):
        # Отмена необратима для родителя (уведомление уже уйдёт репетитору),
        # поэтому сначала явное подтверждение — случайный тап ≠ отмена.
        parts = data.split(":")
        class_id = parts[1] if len(parts) > 1 else ""
        occ_date = parts[2] if len(parts) > 2 else ""
        if not class_id:
            await query.edit_message_text("Не смог прочитать нажатие — попробуйте ещё раз.")
            return
        from src.db.repository import MeritHubClassRepository
        from src.workflows.lesson_ops import _format_class_label
        cls = await MeritHubClassRepository().get(class_id)
        label = _format_class_label(class_id, (cls or {}).get("start_time"))
        date_note = f" {occ_date}" if occ_date else ""
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, отменить", callback_data=f"cancel_yes:{class_id}:{occ_date}"),
            InlineKeyboardButton("◀️ Не надо", callback_data="cancel_x"),
        ]])
        await query.edit_message_text(
            f"Отменяем занятие {label}{date_note}?\n\n"
            "Репетитор и координаторы получат уведомление.",
            reply_markup=kb,
        )
        return

    if data == "cancel_x":
        await query.edit_message_text("Хорошо, занятие остаётся в расписании 👌")
        return

    if data.startswith("cancel_yes:"):
        parts = data.split(":")
        class_id = parts[1] if len(parts) > 1 else ""
        occ_date = parts[2] if len(parts) > 2 else ""
        if not class_id:
            await query.edit_message_text("Не смог прочитать нажатие — попробуйте ещё раз.")
            return
        await _ensure_user(upd, "parent")
        reason = "Отмена родителем через бота"
        if occ_date:
            reason += f" (занятие {occ_date})"
        await bus.publish(Event(EventTypes.LESSON_CANCELLED, {
            "lesson_id": class_id,
            "reason": reason,
            "occurrence_date": occ_date or None,
            "reported_by": str(query.from_user.id),
        }))
        from src.db.repository import MeritHubClassRepository
        from src.workflows.lesson_ops import _format_class_label
        cls = await MeritHubClassRepository().get(class_id)
        label = _format_class_label(class_id, (cls or {}).get("start_time"))
        date_note = f" ({occ_date})" if occ_date else ""
        await query.edit_message_text(
            f"🔄 Отмена {label}{date_note} передана репетитору и координаторам.")
        logger.info("Cancel via button: class=%s date=%s by=%s", class_id, occ_date, query.from_user.id)
        return

    # --- П1: координатор решает судьбу занятия после «не придём»/«can't teach» ---
    if data.startswith("coord_cancel_class:"):
        if not await is_coordinator_or_admin(query.from_user.id):
            await query.answer("⛔ Только координатор/админ", show_alert=True)
            return
        parts = data.split(":")
        class_id = parts[1] if len(parts) > 1 else ""
        occ_date = parts[2] if len(parts) > 2 and parts[2] != "None" else None
        if not class_id:
            await query.edit_message_text("Не смог прочитать нажатие — попробуйте ещё раз.")
            return
        await bus.publish(Event(EventTypes.LESSON_CANCELLED, {
            "lesson_id": class_id,
            "reason": "Отмена координатором после уведомления о неявке",
            "occurrence_date": occ_date,
            "reported_by": str(query.from_user.id),
        }))
        await query.edit_message_text("✅ Отмена передана: репетитор и родители уведомлены.")
        logger.info("Coordinator cancelled class %s (date=%s) via no-show decision", class_id, occ_date)
        return

    if data.startswith("coord_keep_class:"):
        if not await is_coordinator_or_admin(query.from_user.id):
            await query.answer("⛔ Только координатор/админ", show_alert=True)
            return
        parts = data.split(":")
        class_id = parts[1] if len(parts) > 1 else ""
        occ_date = parts[2] if len(parts) > 2 and parts[2] != "None" else None
        if not class_id:
            await query.edit_message_text("Не смог прочитать нажатие — попробуйте ещё раз.")
            return
        ops = LessonOpsWorkflow()
        await ops._keep_class_notify(class_id, occ_date)
        await query.edit_message_text("✅ Ок, занятие остаётся — репетитор и родители уведомлены.")
        logger.info("Coordinator kept class %s (date=%s) after no-show decision", class_id, occ_date)
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
        if not await is_coordinator_or_admin(query.from_user.id):
            await query.answer("⛔ Закрывать ситуации может только координатор/админ", show_alert=True)
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

    # --- R9-14: выбор «на сколько минут» после «⏰ Опоздаем» родителя ---
    if data.startswith("resolve_late_time:"):
        parts = data.split(":")
        try:
            inc_id = int(parts[1])
            nonce = parts[2]
            mins_str = parts[3]
        except (IndexError, ValueError):
            await query.edit_message_text("Не смог прочитать нажатие.")
            return
        idem_key = f"tg_callback:{data}"
        idem = IdempotencyRepository()
        if await idem.exists(idem_key):
            await query.answer("✅ Уже обработано", show_alert=False)
            return

        inc_repo = IncidentRepository()
        inc = await inc_repo.get(inc_id)
        if not inc:
            await query.edit_message_text("Ситуация не найдена.")
            return
        if inc["status"] == "resolved":
            await query.answer("ℹ️ Эта ситуация уже закрыта", show_alert=False)
            try:
                await query.edit_message_reply_markup(None)
            except Exception:
                pass
            return
        was_escalated = inc["status"] == "escalated"

        wf_repo = WorkflowRepository()
        wf_rows = await wf_repo.find_by_json("incident_id", inc_id, limit=1)
        wf_row = wf_rows[0] if wf_rows else None
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
        expected_parent = wf_data.get("parent_telegram_id")
        if expected_parent and str(expected_parent) != str(query.from_user.id):
            await query.answer("⛔ Это сообщение не для вас", show_alert=True)
            return

        wf = AbsenceWorkflow()
        await wf.resolve_absence(inc_id, str(query.from_user.id), resolution="parent_late")
        await wf.notify_coordinators_parent_reply(
            inc_id, "late", late_minutes=mins_str,
            parent_telegram_id=str(query.from_user.id),
        )

        await idem.save(idem_key, "telegram_callback", response="resolved_late")
        # Блокируем остальные кнопки этого инцидента (включая другие интервалы)
        for other_action in ("ok", "no", "late"):
            other_key = f"tg_callback:resolve:{inc_id}:{nonce}:{other_action}"
            await idem.save(other_key, "telegram_callback_blocked", response="blocked_by_resolve_late")
        for other_mins in ("5", "15", "30+"):
            if other_mins != mins_str:
                other_key = f"tg_callback:resolve_late_time:{inc_id}:{nonce}:{other_mins}"
                await idem.save(other_key, "telegram_callback_blocked", response="blocked_by_resolve_late")

        from src.utils.i18n import lang_of, tr
        lang = await lang_of(str(query.from_user.id))
        # '15' → '15 мин', '30+' → '30+ мин' (без «на» — его добавляет шаблон)
        mins_label = f"{mins_str} мин"
        parent_ack = tr("ack_late_detail_parent", lang, mins=mins_label)
        if was_escalated:
            parent_ack += "\n\nℹ️ Координатор уже был уведомлён об отсутствии ответа. Ваш ответ передан — инцидент закрыт."
        student_label = wf_data.get("student_name") or "Ученик"
        parent_ack += "\n\nОшиблись? Напишите текстом — координатор поможет."
        await query.edit_message_text(f"{parent_ack}\nОтметили: {student_label} · вопрос закрыт.")
        logger.info("Incident %d resolved via late_time=%s (parent %s)", inc_id, mins_str, query.from_user.id)
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
        wf_rows = await wf_repo.find_by_json("incident_id", inc_id, limit=1)
        wf_row = wf_rows[0] if wf_rows else None
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
        # Самоаудит: кнопку уведомления о неявке может нажать только родитель
        expected_parent = wf_data.get("parent_telegram_id")
        if expected_parent and str(expected_parent) != str(query.from_user.id):
            await query.answer("⛔ Это сообщение не для вас", show_alert=True)
            return

        # R9-14: «⏰ Опоздаем» — сначала уточняем, НА СКОЛЬКО минут (тот же
        # микро-шаг, что в prelesson-checkin R8-10). Инцидент не резолвим,
        # пока родитель не выбрал интервал (эскалация по таймеру — страховка).
        if action == "late":
            from src.utils.i18n import lang_of, tr
            lang = await lang_of(str(query.from_user.id))
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("на 5 мин", callback_data=f"resolve_late_time:{inc_id}:{nonce}:5"),
                InlineKeyboardButton("на 15 мин", callback_data=f"resolve_late_time:{inc_id}:{nonce}:15"),
                InlineKeyboardButton("на 30+ мин", callback_data=f"resolve_late_time:{inc_id}:{nonce}:30+"),
            ]])
            await query.edit_message_text(tr("ack_late_ask_mins_parent", lang), reply_markup=kb)
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

        # UX U5 + П10: имя ученика, без внутреннего номера инцидента и без
        # серверного времени (номер нужен координатору, не родителю).
        student_label = wf_data.get("student_name") or "Ученик"
        # П8: подсказка на случай случайного нажатия
        parent_ack += "\n\nОшиблись? Напишите текстом — координатор поможет."
        await query.edit_message_text(f"{parent_ack}\nОтметили: {student_label} · вопрос закрыт.")
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
        # Самоаудит: кнопку может нажать только адресат (пересланные кнопки
        # не должны работать от чужого аккаунта)
        expected_actor = wf_data.get("actor_telegram_id")
        if expected_actor and str(expected_actor) != str(query.from_user.id):
            await query.answer("⛔ Это сообщение не для вас", show_alert=True)
            return

        ops = LessonOpsWorkflow()
        actor_type = wf_data.get("actor_type")
        # R10 (П5): для «⏰ Опоздаю» НЕ шлём мгновенный алерт координатору —
        # дождёмся выбора минут и отправим ОДНО итоговое сообщение. Пока выбор
        # не сделан, workflow остаётся running; fallback-алерт через 10 минут —
        # страховка, что факт опоздания не потеряется.
        if action == "late":
            wf_data["response_status"] = "late"
            wf_data["responded_at"] = datetime.now(timezone.utc).isoformat()
            await repo.update_data(wid, wf_data)
            # Самоаудит R10: отменяем остальные будущие действия workflow
            # (parent/tutor_prelesson_no_reply и т.п.) — иначе координатор
            # получит ложное «Родитель/репетитор не ответил» в добавок к
            # «опоздает». Fallback создаём ПОСЛЕ отмены, чтобы он остался.
            await ScheduledActionRepository().cancel_by_workflow(wid)
            await ScheduledActionRepository().create(
                wid,
                (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                "checkin_late_fallback", {"workflow_id": wid})
            await idem.save(idem_key, "telegram_checkin", response=action)
            from src.utils.i18n import lang_of, tr
            lang = await lang_of(str(query.from_user.id))
            # П3: родителю — «на сколько минут УЧЕНИК опоздает», а не «задержитесь ВЫ»
            msg_key = "ack_late_ask_mins_parent" if actor_type == "parent" else "ack_late_ask_mins"
            msg_text = tr(msg_key, lang)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(tr("tutor_btn_late_5", lang), callback_data=f"checkin_late_time:{wid}:{nonce}:5"),
                InlineKeyboardButton(tr("tutor_btn_late_15", lang), callback_data=f"checkin_late_time:{wid}:{nonce}:15"),
                InlineKeyboardButton(tr("tutor_btn_late_30", lang), callback_data=f"checkin_late_time:{wid}:{nonce}:30+"),
            ]])
            await query.edit_message_text(msg_text, reply_markup=kb)
            return

        await ops.record_checkin_response(wid, actor_tg=str(query.from_user.id), action=action)
        await idem.save(idem_key, "telegram_checkin", response=action)
        # Подтверждение — на языке нажавшего (тьюторы — EN).
        from src.utils.i18n import lang_of, tr
        lang = await lang_of(str(query.from_user.id))
        ack = tr(f"ack_{action}", lang)
        await query.edit_message_text(ack if ack != f"ack_{action}" else "✅ Ответ принят.")
        return

    if data.startswith("checkin_late_time:"):
        parts = data.split(":")
        try:
            wid = int(parts[1])
            nonce = parts[2]
            mins_str = parts[3]
        except (IndexError, ValueError):
            await query.edit_message_text("Не смог прочитать нажатие.")
            return
        idem_key = f"tg_callback:{data}"
        idem = IdempotencyRepository()
        if await idem.exists(idem_key):
            await query.answer("✅ Уже обработано", show_alert=False)
            return
        from src.utils.i18n import lang_of, tr
        lang = await lang_of(str(query.from_user.id))
        ops = LessonOpsWorkflow()
        # R9-13: координаторам передаём raw-минуты (текст формирует workflow
        # по actor_type); пользователю — локализованный ack.
        # R10 (П5): одно итоговое сообщение координатору; fallback отменяем,
        # workflow завершаем.
        wf_row2 = await WorkflowRepository().get(wid)
        try:
            wf_data2 = json.loads(wf_row2.get("data") or "{}") if wf_row2 else {}
        except Exception:
            wf_data2 = {}
        actor_type = wf_data2.get("actor_type")
        await ScheduledActionRepository().cancel_by_workflow(wid)
        await ops.notify_late_detail(wid, mins_str)
        await WorkflowRepository().update_state(
            wid, "completed",
            {**wf_data2, "response_status": "late", "late_minutes": mins_str})
        await idem.save(idem_key, "telegram_checkin_late_time", response=mins_str)
        mins_label = f"{mins_str} мин"
        ack_key = "ack_late_detail_parent" if actor_type == "parent" else "ack_late_detail"
        ack = tr(ack_key, lang, mins=mins_label)
        await query.edit_message_text(ack)
        return

    await query.edit_message_text("Не понял действие — попробуйте ещё раз или напишите текстом.")


# =====================================================================
# MESSAGE HANDLER
# =====================================================================

async def handle_message(upd: Update, _ctx) -> None:
    # Free-text шаги визардов координатора (поиск репетитора, своё время и т.п.) —
    # если активен сценарий, сообщение обрабатывает он, диалог не доходит до NLU.
    if await try_handle_wz_text(upd, _ctx):
        return
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
            try:
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
            except Exception as e:
                logger.error("Parent checkin processing failed: %s", e, exc_info=True)
                await _forward_to_coordinator(upd, text, tg_id, user_rec)
            return

    if role == "tutor":
        active_checkin = await ops.find_active_checkin(tg_id, ("tutor", "tutor_start"))
        if active_checkin:
            try:
                wid, data, _wf_type = active_checkin
                actor_type = data.get("actor_type")
                from src.utils.i18n import lang_of, tr
                lang = await lang_of(tg_id)
                if actor_type == "tutor_start":
                    await ops.record_checkin_response(wid, actor_tg=tg_id, action="other", free_text=text)
                    await upd.message.reply_text(tr("ft_tutor_start_hint", lang))
                    return
                else:
                    interpreted = await llm_client.interpret_tutor_reply(text)
                    action = interpreted.get("status", "other")
                await ops.record_checkin_response(wid, actor_tg=tg_id, action=action, free_text=text)
                key = f"ft_tutor_{action}" if action in ("ready", "late", "no_show", "tech") else "ft_tutor_other"
                reply = tr(key, lang)
                await upd.message.reply_text(reply if reply != key else "✅ Ответ принят.")
            except Exception as e:
                logger.error("Tutor checkin processing failed: %s", e, exc_info=True)
                await _forward_to_coordinator(upd, text, tg_id, user_rec)
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
            # R9-14: из свободного текста «опоздаем на 15 минут» вытаскиваем минуты
            late_minutes = None
            if status == "late":
                m = re.search(r"(\d{1,3})\s*мин", text)
                if m:
                    late_minutes = m.group(1)
            if status == "other":
                # П4: непонятный ответ НЕ закрывает инцидент — он остаётся
                # активным (review) до разбора координатором. Эскалация по
                # таймеру отменяется (мы уже эскалируем вручную), workflow
                # остаётся running, чтобы поздние ответы родителя перехватывались.
                await wf.incidents.update_status(inc_id, "review")
                wf_rows_r = await WorkflowRepository().find_by_json(
                    "incident_id", inc_id, limit=1)
                if wf_rows_r:
                    await ScheduledActionRepository().cancel_by_workflow(wf_rows_r[0]["id"])
                await wf.notify_coordinators_parent_reply(
                    inc_id, "free_text", parent_text=text,
                    parent_telegram_id=tg_id, review=True)
                await upd.message.reply_text(reply_map["other"])
                return
            await wf.resolve_absence(inc_id, tg_id, resolution=resolution_map.get(status, "parent_text_reply"))
            await wf.notify_coordinators_parent_reply(
                inc_id,
                status if status in {"ok", "no_show", "late"} else "free_text",
                parent_text=text,
                parent_telegram_id=tg_id,
                late_minutes=late_minutes,
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

    # Классификация → конкретные workflow отвечают пользователю сами
    # (отмена / заявка / неявка / fallback). Промежуточного «Обрабатываю...»
    # больше нет (R7-1): ин-мемори bus выполняет цепочку синхронно, поэтому
    # настоящий ответ приходит в рамках того же запроса — без лишнего шума.
    await bus.publish(Event(EventTypes.MESSAGE_INCOMING, {
        "text": text, "telegram_id": tg_id, "chat_id": str(upd.effective_chat.id),
    }))


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
    app.add_handler(CommandHandler("lessons", cmd_lessons))
    app.add_handler(CommandHandler("ok", cmd_ok))
    # Кнопочные сценарии координатора (визарды) — создание без ручного ввода ID.
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("add_student", cmd_add_student))
    app.add_handler(CommandHandler("add_tutor", cmd_add_tutor))
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
