"""Демо-пилот: прогон сценария неявки на реальных TG-аккаунтах владельцев.

Команды (только владельцы/админы из ALBION_ADMIN_TELEGRAM_IDS):
    /pilot_seed   — предполётная проверка: кто какую роль играет, готов ли пилот
    /pilot_absent — запустить сценарий неявки на живых аккаунтах

Пилот опирается на РОЛИ, назначенные через /role: нужны хотя бы один `parent`
и один `coordinator`. Имя ученика и TG родителя передаются через данные workflow,
поэтому сценарий работает на реальных аккаунтах и НЕ зависит от mock-данных.
Когда подключим реальный MeritHub API, ученики будут браться уже оттуда.
"""

import logging
from datetime import datetime, timezone, timedelta

from telegram import Update
from telegram.ext import Application, CommandHandler

from src.config import settings
from src.db.repository import (
    UserRepository, IncidentRepository, ScheduledActionRepository, WebhookEventRepository,
    MeritHubStudentRepository, MeritHubEnrollmentRepository,
)
from src.workflows.engine import engine
from src.bot.roles import is_admin, ROLE_EMOJI

logger = logging.getLogger(__name__)

PILOT_LESSON_REF = "pilot_lesson_1"
PILOT_STUDENT_ID = "pilot_student_1"
NOTIFY_DELAY_SECONDS = 10  # быстрое уведомление для живого демо


async def _pilot_roster(db_path: str = "albion.db"):
    repo = UserRepository(db_path)
    parents = await repo.list_by_role("parent")
    tutors = await repo.list_by_role("tutor")
    coords = await repo.list_by_role("coordinator")
    return parents, tutors, coords


async def cmd_pilot_seed(upd: Update, _ctx) -> None:
    """Предполётная проверка пилота: показывает распределение ролей и готовность."""
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ может готовить пилот.")
        return

    parents, tutors, coords = await _pilot_roster()

    def fmt(users) -> str:
        return ", ".join(f"`{u['telegram_id']}` {u['name']}" for u in users) or "_нет_"

    lines = ["🧪 *Предполётная проверка пилота*\n"]
    lines.append(f"{ROLE_EMOJI['parent']} Родители: {fmt(parents)}")
    lines.append(f"{ROLE_EMOJI['tutor']} Репетиторы: {fmt(tutors)}")
    lines.append(f"{ROLE_EMOJI['coordinator']} Координаторы: {fmt(coords)}")
    lines.append("")

    if parents and coords:
        lines.append("✅ Пилот готов. Запустите сценарий неявки: /pilot_absent")
    else:
        missing = []
        if not parents:
            missing.append("parent")
        if not coords:
            missing.append("coordinator")
        lines.append(
            "❌ Не хватает ролей: " + ", ".join(missing) + ".\n"
            "Назначьте их владельцам: `/role <TG_ID> <роль>` (список: /roles).",
        )
    await upd.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def trigger_absence(
    *,
    lesson_ref: str,
    student_id: str,
    student_name: str,
    parent_telegram_id: str,
    tutor_id: str = "pilot_tutor",
    delay_seconds: int = NOTIFY_DELAY_SECONDS,
    source: str = "pilot",
) -> tuple[int, int]:
    """Создаёт инцидент + workflow неявки и планирует уведомление родителя.

    Реальный TG родителя передаётся в данных workflow — `_notify_parent` берёт
    его оттуда. Используется и командой /pilot_absent, и webhook attendance.
    Возвращает (incident_id, workflow_id)."""
    inc_id = await IncidentRepository().create(
        lesson_ref=lesson_ref, student_id=student_id, tutor_id=tutor_id,
        type="absence", status="pending",
    )
    wid = await engine.start_workflow("absence_notification", {
        "incident_id": inc_id,
        "student_id": student_id,
        "student_name": student_name,
        "parent_telegram_id": parent_telegram_id,
        "lesson_ref": lesson_ref,
        "source": source,
    })
    await ScheduledActionRepository().create(
        wid,
        (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat(),
        "notify_parent",
        {"incident_id": inc_id},
    )
    logger.info("Absence triggered (%s): inc=%d wf=%d parent=%s", source, inc_id, wid, parent_telegram_id)
    return inc_id, wid


async def cmd_pilot_absent(upd: Update, _ctx) -> None:
    """Запускает сценарий неявки: родитель получит уведомление с кнопкой."""
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ может запускать пилот.")
        return

    parents, tutors, coords = await _pilot_roster()
    if not parents or not coords:
        await upd.message.reply_text(
            "❌ Нужны хотя бы один `parent` и один `coordinator`. Проверьте: /pilot_seed",
            parse_mode="Markdown",
        )
        return

    parent = parents[0]
    student_name = settings.albion_pilot_student_name
    tutor_id = tutors[0]["telegram_id"] if tutors else "pilot_tutor"

    inc_id, wid = await trigger_absence(
        lesson_ref=PILOT_LESSON_REF,
        student_id=PILOT_STUDENT_ID,
        student_name=student_name,
        parent_telegram_id=parent["telegram_id"],
        tutor_id=tutor_id,
        source="pilot_command",
    )

    await upd.message.reply_text(
        f"🚀 *Пилотный сценарий запущен* (ситуация #{inc_id}).\n\n"
        f"Через ~{NOTIFY_DELAY_SECONDS} сек родитель {parent['name']} "
        f"(`{parent['telegram_id']}`) получит уведомление о неявке ученика "
        f"*«{student_name}»* с кнопкой «✅ Всё в порядке».\n\n"
        f"Если родитель не ответит — через {settings.albion_escalate_delay_min} мин "
        f"пойдёт эскалация координатору (management by exception).",
        parse_mode="Markdown",
    )


async def cmd_mh_events(upd: Update, ctx) -> None:
    """Показывает последние захваченные вебхуки MeritHub (для настройки авто-обработчиков)."""
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return
    try:
        limit = int((ctx.args or ["5"])[0])
    except ValueError:
        limit = 5
    limit = max(1, min(limit, 20))
    rows = await WebhookEventRepository().list_recent(limit)
    if not rows:
        await upd.message.reply_text(
            "🛰 Пока нет захваченных событий MeritHub.\n\n"
            "Чек-лист:\n"
            "1) запущен ли `uvicorn src.api.webhook:app --port 8000`;\n"
            "2) поднят ли туннель (ngrok/cloudflared) на :8000;\n"
            "3) в MeritHub → Webhook Url вставлен ли публичный URL + /merithub/webhook;\n"
            "4) задан ли MERITHUB_WEBHOOK_SECRET в .env;\n"
            "5) включены ли чекбоксы (Attendance и др.) и дёрнуто ли событие в MeritHub.")
        return
    blocks = ["🛰 Последние события MeritHub (захват для авто-обработчиков):\n"]
    for r in rows:
        ok = "✅" if r["signature_ok"] else "⛔(bad sig)"
        blocks.append(
            f"{ok} #{r['id']} [{r['received_at']}] type={r['event_type'] or '?'}\n"
            f"headers: {r['headers'][:200]}\n"
            f"raw: {r['raw'][:300]}\n"
        )
    await upd.message.reply_text("\n".join(blocks))


async def cmd_mh_user(upd: Update, ctx) -> None:
    """Связывает ученика MeritHub с TG родителя: /mh_user <clientUserId> <parentTG> <имя>."""
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return
    args = ctx.args or []
    if len(args) < 3:
        await upd.message.reply_text(
            "Использование: `/mh_user <clientUserId> <parentTG> <имя_ученика>`\n"
            "Создаёт ученика в MeritHub (если заданы credentials) и связывает с TG родителя.",
            parse_mode="Markdown",
        )
        return
    cuid, parent_tg, name = args[0], args[1], " ".join(args[2:])
    # Родитель обязан быть зарегистрирован, иначе уведомление уйдёт в эскалацию.
    await UserRepository().set_role_by_telegram(parent_tg, "parent", name=f"Родитель: {name}")
    mh_id = None
    api_note = ""
    if settings.merithub_use_real:
        try:
            from src.integrations.factory import get_merithub_service
            from src.integrations.merithub_client import MeritHubClient
            client = get_merithub_service()
            resp = await client.add_user(client_user_id=cuid, name=name, role="M")
            mh_id = MeritHubClient._extract_id(resp, "userId", "id", "UserId", "userID")
            api_note = f" MeritHub userId=`{mh_id}`." if mh_id else " (userId не распознан в ответе)"
        except Exception as e:
            api_note = f" ⚠️ MeritHub API: {str(e)[:120]} (локальная связь сохранена)"
    await MeritHubStudentRepository().upsert(
        cuid, merithub_user_id=mh_id, name=name, parent_telegram_id=parent_tg, role="student")
    await upd.message.reply_text(
        f"✅ Ученик привязан: `{cuid}` → родитель `{parent_tg}` ({name}).{api_note}\n"
        f"Зачислите в класс: `/mh_enroll <classId> {cuid}`",
        parse_mode="Markdown",
    )


async def cmd_mh_enroll(upd: Update, ctx) -> None:
    """Зачисляет учеников в класс: /mh_enroll <classId> <cuid1> [cuid2 ...]."""
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return
    args = ctx.args or []
    if len(args) < 2:
        await upd.message.reply_text(
            "Использование: `/mh_enroll <classId> <clientUserId> [...]`", parse_mode="Markdown")
        return
    class_id, cuids = args[0], args[1:]
    srepo, erepo = MeritHubStudentRepository(), MeritHubEnrollmentRepository()
    added, missing = 0, []
    for cuid in cuids:
        s = await srepo.get_by_client_id(cuid)
        if not s or not s.get("merithub_user_id"):
            missing.append(cuid)
            continue
        await erepo.add(
            class_id, s["merithub_user_id"], client_user_id=cuid,
            parent_telegram_id=s.get("parent_telegram_id"),
            student_name=s.get("name"), role=s.get("role") or "student",
        )
        added += 1
    msg = f"✅ В класс `{class_id}` зачислено: {added}."
    if missing:
        msg += f"\n⚠️ Пропущены (нет привязки/MeritHub id): {', '.join(missing)} — сначала `/mh_user ...`"
    await upd.message.reply_text(msg, parse_mode="Markdown")


async def cmd_mh_students(upd: Update, _ctx) -> None:
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return
    rows = await MeritHubStudentRepository().list_all()
    if not rows:
        await upd.message.reply_text("Пока нет привязок MeritHub. Добавьте: `/mh_user ...`")
        return
    lines = ["🔗 Привязки MeritHub ↔ родитель (TG):"]
    for r in rows:
        lines.append(
            f"• `{r['client_user_id']}` mh=`{r['merithub_user_id'] or '-'}` "
            f"parent=`{r['parent_telegram_id'] or '-'}` {r['role']} — {r['name']}")
    await upd.message.reply_text("\n".join(lines))


async def cmd_mh_tutor(upd: Update, ctx) -> None:
    """Создаёт репетитора в MeritHub (role C) и сохраняет маппинг: /mh_tutor <cuid> <имя>."""
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return
    args = ctx.args or []
    if len(args) < 2:
        await upd.message.reply_text("Использование: `/mh_tutor <clientUserId> <имя>`", parse_mode="Markdown")
        return
    cuid, name = args[0], " ".join(args[1:])
    mh_id, api_note = None, ""
    if settings.merithub_use_real:
        try:
            from src.integrations.factory import get_merithub_service
            from src.integrations.merithub_client import MeritHubClient
            resp = await get_merithub_service().add_user(client_user_id=cuid, name=name, role="C")
            mh_id = MeritHubClient._extract_id(resp, "userId", "id", "UserId", "userID")
            api_note = f" MeritHub userId=`{mh_id}`." if mh_id else " (userId не распознан)"
        except Exception as e:
            api_note = f" ⚠️ MeritHub API: {str(e)[:120]}"
    await MeritHubStudentRepository().upsert(
        cuid, merithub_user_id=mh_id, name=name, parent_telegram_id=None, role="tutor")
    await upd.message.reply_text(f"✅ Репетитор привязан: `{cuid}` ({name}).{api_note}", parse_mode="Markdown")


async def cmd_mh_schedule(upd: Update, ctx) -> None:
    """Создаёт класс в MeritHub и зачисляет репетитора+учеников одной командой:
    /mh_schedule <tutorCuid> <startRFC3339> <durationMin> <studentCuid> [...]"""
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return
    if not settings.merithub_use_real:
        await upd.message.reply_text(
            "❌ Для /mh_schedule нужны `MERITHUB_CLIENT_ID` + `MERITHUB_CLIENT_SECRET` в .env.",
            parse_mode="Markdown")
        return
    args = ctx.args or []
    if len(args) < 4:
        await upd.message.reply_text(
            "Использование: `/mh_schedule <tutorCuid> <startRFC3339> <durationMin> <studentCuid> [...]`\n"
            "Создаёт класс, зачисляет участников и сохраняет зачисление для авто-неявок.",
            parse_mode="Markdown")
        return
    tutor_cuid, start, duration, student_cuids = args[0], args[1], args[2], args[3:]
    srepo, erepo = MeritHubStudentRepository(), MeritHubEnrollmentRepository()
    tutor = await srepo.get_by_client_id(tutor_cuid)
    if not tutor or not tutor.get("merithub_user_id"):
        await upd.message.reply_text(
            f"❌ Репетитор `{tutor_cuid}` не найден в MeritHub. Сначала `/mh_tutor {tutor_cuid} <имя>`.",
            parse_mode="Markdown")
        return

    from src.integrations.factory import get_merithub_service
    from src.integrations.merithub_client import MeritHubClient
    client = get_merithub_service()
    try:
        sched = await client.schedule_class(
            tutor["merithub_user_id"], title=f"Занятие {start}",
            start_time=start, duration=int(duration))
        info = MeritHubClient.parse_schedule(sched)
        class_id = info["class_id"]
        if not class_id:
            await upd.message.reply_text(
                f"❌ Не получен classId. Ответ MeritHub: `{str(sched)[:300]}`", parse_mode="Markdown")
            return

        users = []
        if info["host_link"]:
            users.append({"userId": tutor["merithub_user_id"], "userLink": info["host_link"], "userType": "su"})
        student_rows, missing = [], []
        for cuid in student_cuids:
            s = await srepo.get_by_client_id(cuid)
            if not s or not s.get("merithub_user_id"):
                missing.append(cuid)
                continue
            student_rows.append(s)
            if info["participant_link"]:
                users.append({"userId": s["merithub_user_id"], "userLink": info["participant_link"], "userType": "su"})
        if users:
            await client.add_users_to_class(class_id, users)

        # Сохраняем зачисление — по нему webhook attendance посчитает неявки.
        await erepo.add(class_id, tutor["merithub_user_id"], client_user_id=tutor_cuid,
                        parent_telegram_id=None, student_name=tutor.get("name"), role="tutor")
        for s in student_rows:
            await erepo.add(class_id, s["merithub_user_id"], client_user_id=s["client_user_id"],
                            parent_telegram_id=s.get("parent_telegram_id"),
                            student_name=s.get("name"), role="student")
    except Exception as e:
        await upd.message.reply_text(f"❌ Ошибка MeritHub API: {str(e)[:200]}", parse_mode="Markdown")
        return

    host_url = client.room_url(info["host_link"]) if info["host_link"] else "—"
    msg = (f"✅ Класс создан: `{class_id}`\n🔗 Комната репетитора: {host_url}\n"
           f"👥 Зачислено учеников: {len(student_rows)}"
           + (f"\n⚠️ Пропущено (нет привязки): {', '.join(missing)}" if missing else ""))
    await upd.message.reply_text(msg, parse_mode="Markdown")


def register_pilot_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("pilot_seed", cmd_pilot_seed))
    app.add_handler(CommandHandler("pilot_absent", cmd_pilot_absent))
    app.add_handler(CommandHandler("mh_events", cmd_mh_events))
    app.add_handler(CommandHandler("mh_user", cmd_mh_user))
    app.add_handler(CommandHandler("mh_tutor", cmd_mh_tutor))
    app.add_handler(CommandHandler("mh_enroll", cmd_mh_enroll))
    app.add_handler(CommandHandler("mh_schedule", cmd_mh_schedule))
    app.add_handler(CommandHandler("mh_students", cmd_mh_students))
    logger.info("Pilot handlers registered (/pilot_* /mh_*)")
