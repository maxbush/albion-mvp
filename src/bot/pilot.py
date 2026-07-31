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
    MeritHubStudentRepository, MeritHubClassRepository, MeritHubContactRepository,
    MeritHubEnrollmentRepository,
)
from src.workflows.engine import engine
from src.workflows.lesson_ops import LessonOpsWorkflow
from src.bot.roles import is_admin, ROLE_EMOJI

logger = logging.getLogger(__name__)


def _esc_md(text) -> str:
    """Экранирует спецсимволы Telegram Markdown V1 в динамических данных
    (имена, email, phone) перед вставкой в сообщения с parse_mode="Markdown".
    Без этого имя вида 'Anna_Maria' ломает отправку всего сообщения
    (BadRequest: can't parse entities)."""
    if text is None:
        return ""
    s = str(text)
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


PILOT_LESSON_REF = "pilot_lesson_1"
PILOT_STUDENT_ID = "pilot_student_1"
NOTIFY_DELAY_SECONDS = 10  # быстрое уведомление для живого демо


async def _pilot_roster(db_path: str | None = None):
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
        return ", ".join(f"{u['telegram_id']} {u['name']}" for u in users) or "нет"

    lines = ["🧪 Предполётная проверка пилота\n"]
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
            "Назначьте их владельцам: /role <TG_ID> <роль> (список: /roles)."
        )
    await upd.message.reply_text("\n".join(lines))


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
        f"🚀 Пилотный сценарий запущен (ситуация #{inc_id}).\n\n"
        f"Через ~{NOTIFY_DELAY_SECONDS} сек родитель {parent['name']} "
        f"({parent['telegram_id']}) получит уведомление о неявке ученика "
        f"«{student_name}» с кнопками ответа.\n\n"
        f"Если родитель не ответит — через {settings.albion_escalate_delay_min} мин "
        f"пойдёт эскалация координатору (management by exception)."
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
            "4) если MeritHub шлёт подпись — задан ли MERITHUB_WEBHOOK_SECRET в .env;\n"
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
    """Связывает ученика MeritHub с TG родителя: /mh_user <clientUserId> <parentTG> <имя>.

    Опционально можно указать email и телефон родителя:
      /mh_user s01 333333333 Алиса Джонс
      /mh_user s01 333333333 Алиса Джонс email=parent@ex.com
      /mh_user s01 333333333 Алиса Джонс phone=+447493994501
      /mh_user s01 333333333 Алиса Джонс email=parent@ex.com phone=+447493994501
    """
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return
    args = ctx.args or []
    if len(args) < 3:
        await upd.message.reply_text(
            "Использование: `/mh_user <clientUserId> <parentTG> <имя> [email=...] [phone=...]`\n\n"
            "Создаёт ученика в MeritHub (если заданы credentials) и связывает с TG родителя.\n"
            "Контакты родителя (email, phone) хранятся в ALBION — в MeritHub нет полноценных карточек родителей.\n\n"
            "Примеры:\n"
            "  `/mh_user s01 333333333 Алиса Джонс`\n"
            "  `/mh_user s01 333333333 Алиса email=p@ex.com phone=+44123`",
            parse_mode="Markdown",
        )
        return

    cuid, parent_tg = args[0], args[1]

    # Извлекаем опциональные параметры email= и phone= из аргументов
    extra_email = None
    extra_phone = None
    name_parts = []
    for arg in args[2:]:
        if arg.startswith("email="):
            extra_email = arg[6:]
        elif arg.startswith("phone="):
            extra_phone = arg[6:]
        else:
            name_parts.append(arg)
    name = " ".join(name_parts)

    if not name:
        await upd.message.reply_text("Укажите имя ученика.")
        return

    # Родитель обязан быть зарегистрирован, иначе уведомление уйдёт в эскалацию.
    await UserRepository().set_role_by_telegram(parent_tg, "parent", name=f"Родитель: {name}")
    # Проверяем существующий merithub_user_id (для upsert при duplicate)
    existing_student = await MeritHubStudentRepository().get_by_client_id(cuid)
    existing_mh_id = (existing_student or {}).get("merithub_user_id")
    mh_id = None
    api_note = ""

    # 1. Сначала MeritHub API
    if settings.merithub_use_real:
        try:
            from src.integrations.factory import get_merithub_service
            from src.integrations.merithub_client import MeritHubClient
            client = get_merithub_service()
            if existing_mh_id and not existing_mh_id.startswith("mh_"):
                await client.update_user(existing_mh_id, name=name, email=extra_email or f"{cuid}@albion.local")
                mh_id = existing_mh_id
                api_note = f" MeritHub userId={mh_id} (обновлён)."
                logger.info("MH_SYNC: update_user %s (%s)", cuid, mh_id)
            else:
                resp = await client.add_user(client_user_id=cuid, name=name, role="M", email=extra_email or f"{cuid}@albion.local")
                mh_id = MeritHubClient._extract_id(resp, "userId", "id", "UserId", "userID")
                api_note = f" MeritHub userId={mh_id}." if mh_id else " (userId не распознан в ответе)"
                logger.info("MH_SYNC: add_user %s -> %s", cuid, mh_id)
        except Exception as e:
            await upd.message.reply_text(
                f"❌ Ошибка MeritHub API: {str(e)[:200]}\n"
                f"Запись НЕ создана. Проверьте данные и попробуйте снова.")
            return

    if not mh_id:
        mh_id = existing_mh_id or f"mh_{cuid}"

    # 2. Локальная БД (только если MeritHub OK)
    await MeritHubStudentRepository().upsert(
        cuid, merithub_user_id=mh_id, name=name, parent_telegram_id=parent_tg, role="student")
    logger.info("MH_SYNC: DB upsert merithub_students %s", cuid)

    # Сохраняем контакты родителя (email, phone) в merithub_contacts
    contact_note = ""
    if extra_email or extra_phone:
        contact_repo = MeritHubContactRepository()
        await contact_repo.upsert(
            cuid,
            telegram_id=parent_tg,
            role="parent",
            name=f"Родитель: {name}",
            phone=extra_phone,
            email=extra_email,
        )
        parts = []
        if extra_phone:
            parts.append(f"📱 {_esc_md(extra_phone)}")
        if extra_email:
            parts.append(f"📧 {_esc_md(extra_email)}")
        contact_note = f"\nКонтакты родителя: {' | '.join(parts)}"

    await upd.message.reply_text(
        f"✅ Ученик привязан: `{cuid}` → родитель `{parent_tg}` ({_esc_md(name)}).{api_note}{contact_note}\n"
        f"Зачислите в класс: `/mh_enroll <classId> {cuid}`",
        parse_mode="Markdown",
    )


async def cmd_mh_enroll(upd: Update, ctx) -> None:
    """Зачисляет учеников в класс: /mh_enroll <classId> <cuid1> [cuid2 ...].

    Если класс был создан через /mh_schedule и у нас есть commonParticipantLink,
    то при включённом real MeritHub сделаем и РЕАЛЬНЫЙ add_users_to_class.
    Иначе команда честно выполнит локальную синхронизацию зачисления для webhook attendance.
    """
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return
    args = ctx.args or []
    if len(args) < 2:
        await upd.message.reply_text(
            "Использование: `/mh_enroll <classId> <clientUserId> [...]`", parse_mode="Markdown")
        return
    class_id, cuids = args[0], args[1:]
    srepo = MeritHubStudentRepository()
    crepo = MeritHubClassRepository()
    erepo = MeritHubEnrollmentRepository()

    students, missing = [], []
    for cuid in cuids:
        s = await srepo.get_by_client_id(cuid)
        if not s or not s.get("merithub_user_id"):
            missing.append(cuid)
            continue
        students.append(s)

    remote_added = 0
    remote_note = ""
    class_meta = await crepo.get(class_id)
    if settings.merithub_use_real and students and class_meta and class_meta.get("participant_link"):
        try:
            from src.integrations.factory import get_merithub_service
            from src.integrations.merithub_client import MeritHubClient
            client = get_merithub_service()
            users = [{
                "userId": s["merithub_user_id"],
                "userLink": class_meta["participant_link"],
                "userType": "su",
            } for s in students]
            resp = await client.add_users_to_class(class_id, users)
            remote_added = len(users)
            try:
                unique_links = client.parse_user_links(resp)
                if unique_links:
                    remote_note = f"\n🌐 В MeritHub реально добавлено: {remote_added} (уникальных ссылок: {len(unique_links)})"
                else:
                    remote_note = f"\n🌐 В MeritHub реально добавлено: {remote_added}"
            except Exception:
                remote_note = f"\n🌐 В MeritHub реально добавлено: {remote_added}"
        except Exception as e:
            await upd.message.reply_text(
                f"❌ Ошибка MeritHub API при зачислении в класс {class_id}: {str(e)[:200]}",
                parse_mode="Markdown",
            )
            return
    elif settings.merithub_use_real and students:
        remote_note = (
            "\nℹ️ Реальный API add_users_to_class не вызывался: у класса нет сохранённого "
            "commonParticipantLink. Если класс создан вне /mh_schedule, эта команда работает как локальная синхронизация."
        )

    added = 0
    for s in students:
        await erepo.add(
            class_id,
            s["merithub_user_id"],
            client_user_id=s["client_user_id"],
            parent_telegram_id=s.get("parent_telegram_id"),
            student_name=s.get("name"),
            role=s.get("role") or "student",
        )
        added += 1

    msg = f"✅ В класс {class_id} зачислено локально: {added}."
    if remote_note:
        msg += remote_note
    if missing:
        msg += f"\n⚠️ Пропущены (нет привязки/MeritHub id): {', '.join(missing)} — сначала /mh_user ..."
    await upd.message.reply_text(msg)


async def cmd_mh_tutor(upd: Update, ctx) -> None:
    """Создаёт репетитора в MeritHub (role C).

    Синтаксис:
      /mh_tutor <clientUserId> <имя>
      /mh_tutor <clientUserId> <tutorTG> <имя>

    Во втором варианте дополнительно связывает tutor с его Telegram для
    pre-lesson reminders и стартовых кнопок.
    """
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return
    args = ctx.args or []
    if len(args) < 2:
        await upd.message.reply_text(
            "Использование: /mh_tutor <clientUserId> [<tutorTG>] <имя>")
        return
    cuid = args[0]
    tutor_tg = args[1] if len(args) >= 3 and args[1].isdigit() else None
    name = " ".join(args[2:] if tutor_tg else args[1:])
    # Проверяем существующий merithub_user_id
    existing_tutor = await MeritHubStudentRepository().get_by_client_id(cuid)
    existing_mh_id = (existing_tutor or {}).get("merithub_user_id")
    mh_id, api_note = None, ""

    # 1. Сначала MeritHub API
    if settings.merithub_use_real:
        try:
            from src.integrations.factory import get_merithub_service
            from src.integrations.merithub_client import MeritHubClient
            client = get_merithub_service()
            if existing_mh_id and not existing_mh_id.startswith("mh_"):
                await client.update_user(existing_mh_id, name=name)
                mh_id = existing_mh_id
                api_note = f" MeritHub userId={mh_id} (обновлён)."
                logger.info("MH_SYNC: update_user %s (%s)", cuid, mh_id)
            else:
                resp = await client.add_user(client_user_id=cuid, name=name, role="C")
                mh_id = MeritHubClient._extract_id(resp, "userId", "id", "UserId", "userID")
                api_note = f" MeritHub userId={mh_id}." if mh_id else " (userId не распознан)"
                logger.info("MH_SYNC: add_user %s -> %s", cuid, mh_id)
        except Exception as e:
            await upd.message.reply_text(
                f"❌ Ошибка MeritHub API: {str(e)[:200]}\n"
                f"Запись НЕ создана. Проверьте данные и попробуйте снова.")
            return

    if not mh_id:
        mh_id = existing_mh_id or f"mh_{cuid}"

    # 2. Локальная БД (только если MeritHub OK или mock)
    await MeritHubStudentRepository().upsert(
        cuid, merithub_user_id=mh_id, name=name, parent_telegram_id=None, role="tutor")
    logger.info("MH_SYNC: DB upsert merithub_students %s", cuid)
    if tutor_tg:
        await MeritHubContactRepository().upsert(cuid, tutor_tg, "tutor", name=name)
    tg_note = f" TG{tutor_tg}." if tutor_tg else ""
    await upd.message.reply_text(
        f"✅ Репетитор привязан: {cuid} ({name}).{tg_note}{api_note}")


async def cmd_mh_schedule(upd: Update, ctx) -> None:
    """Создаёт класс + зачисляет: /mh_schedule <tutorCuid> <startRFC3339> <durationMin> <studentCuid> [...]."""
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return
    args = ctx.args or []
    if len(args) < 4:
        await upd.message.reply_text(
            "Использование: `/mh_schedule <tutorCuid> <startRFC3339> <durationMin> <studentCuid> [...]`",
            parse_mode="Markdown",
        )
        return
    tutor_cuid, start, duration, student_cuids = args[0], args[1], args[2], args[3:]
    srepo = MeritHubStudentRepository()
    erepo = MeritHubEnrollmentRepository()
    contact_repo = MeritHubContactRepository()
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
        # Канон расписания — зона ОРГАНИЗАЦИИ (решение владельца, H4/P4.1).
        # Ученики/репетиторы видят dual-time display в своей зоне.
        sched = await client.schedule_class(
            tutor["merithub_user_id"], title=f"Занятие {start}",
            start_time=start, duration=int(duration),
            timezone=settings.albion_org_timezone)
        info = MeritHubClient.parse_schedule(sched)
        class_id = info["class_id"]
        if not class_id:
            await upd.message.reply_text(
                f"❌ Не получен classId. Ответ MeritHub: {str(sched)[:300]}", parse_mode="Markdown")
            return

        await MeritHubClassRepository().upsert(
            class_id,
            host_link=info.get("host_link"),
            participant_link=info.get("participant_link"),
            title=f"Занятие {start}",
            start_time=start,
            tutor_client_user_id=tutor_cuid,
            tutor_merithub_user_id=tutor["merithub_user_id"],
        )

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
        user_links = {}
        if users:
            resp_links = await client.add_users_to_class(class_id, users)
            user_links = client.parse_user_links(resp_links)
            logger.info("MH_SYNC: add_users_to_class %s (%d users)", class_id, len(users))

        # Отправляем ссылку tutor'у
        tutor_contact_row = await contact_repo.get(tutor_cuid)
        tutor_tg = (tutor_contact_row or {}).get("telegram_id")
        if tutor_tg:
            tutor_link = user_links.get(tutor.get("merithub_user_id", ""))
            if tutor_link:
                tutor_room = client.room_url(tutor_link)
                await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                    "telegram_id": tutor_tg,
                    "message": (
                        f"📎 Ссылка на урок:\n"
                        f"🕐 {start}\n"
                        f"👥 Ученики: {', '.join(s.get('name','') for s in student_rows)}\n"
                        f"🔗 {tutor_room}"
                    ),
                }))

        # Отправляем ссылки parent'ам
        for s in student_rows:
            parent_tg = s.get("parent_telegram_id")
            s_link = user_links.get(s.get("merithub_user_id", ""))
            if parent_tg and s_link:
                s_room = client.room_url(s_link)
                await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
                    "telegram_id": parent_tg,
                    "message": (
                        f"📎 Ссылка для подключения:\n"
                        f"Ученик: {s.get('name')}\n"
                        f"🕐 {start}\n"
                        f"🔗 {s_room}"
                    ),
                }))

        # Сохраняем зачисление — по нему webhook attendance посчитает неявки.
        await erepo.add(class_id, tutor["merithub_user_id"], client_user_id=tutor_cuid,
                        parent_telegram_id=None, student_name=tutor.get("name"), role="tutor")
        for s in student_rows:
            await erepo.add(class_id, s["merithub_user_id"], client_user_id=s["client_user_id"],
                            parent_telegram_id=s.get("parent_telegram_id"),
                            student_name=s.get("name"), role="student")

        tutor_contact = await contact_repo.get(tutor_cuid)
        await LessonOpsWorkflow().schedule_class_coordination(
            class_id=class_id,
            start_time=start,
            tutor_name=tutor.get("name") or tutor_cuid,
            tutor_telegram_id=(tutor_contact or {}).get("telegram_id"),
            tutor_timezone=tutor.get("timezone"),
            student_rows=student_rows,
        )
    except Exception as e:
        await upd.message.reply_text(f"❌ Ошибка MeritHub API: {str(e)[:200]}")
        return

    host_url = client.room_url(info["host_link"]) if info["host_link"] else "—"
    tutor_note = f"\n🧑‍🏫 Tutor TG: {(tutor_contact or {}).get('telegram_id')}" if (tutor_contact or {}).get("telegram_id") else "\nℹ️ Tutor TG не привязан: используйте /mh_tutor <cuid> <tutorTG> <имя>"
    msg = (f"✅ Класс создан: {class_id}\n🔗 Комната репетитора: {host_url}\n"
           f"👥 Зачислено учеников: {len(student_rows)}"
           + tutor_note
           + (f"\n⚠️ Пропущено (нет привязки): {', '.join(missing)}" if missing else ""))
    await upd.message.reply_text(msg)


async def cmd_mh_students(upd: Update, _ctx) -> None:
    """Список привязок MeritHub ↔ родитель."""
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return
    rows = await MeritHubStudentRepository().list_all()
    if not rows:
        await upd.message.reply_text("Пока нет учеников. Используйте /mh_user или /seed10.")
        return

    contact_repo = MeritHubContactRepository()
    lines = [f"🔗 Ученики MeritHub ({len(rows)}):\n"]
    for r in rows:
        tz = _esc_md(r.get("timezone") or "—")
        country = _esc_md(r.get("country") or "")
        tz_info = f"🕐 {tz}" + (f" ({country})" if country else "")
        base = (
            f"• *{_esc_md(r['name'])}* `{r['client_user_id'][:12]}...` ({r['role']})\n"
            f"  {tz_info}"
        )
        if r.get("parent_telegram_id"):
            base += f" | parent TG: `{r['parent_telegram_id']}`"
        if r.get("email"):
            base += f" | 📧 {_esc_md(r['email'])}"
        # Добавляем контакты родителя если есть
        contact = await contact_repo.get(r["client_user_id"])
        if contact:
            extras = []
            if contact.get("phone"):
                extras.append(f"📱 {_esc_md(contact['phone'])}")
            if contact.get("email"):
                extras.append(f"📧 {_esc_md(contact['email'])}")
            if contact.get("name"):
                extras.append(f"👤 {_esc_md(contact['name'])}")
            if extras:
                base += f"\n  Parent: {' | '.join(extras)}"
        lines.append(base)
    await upd.message.reply_text("\n".join(lines), parse_mode="Markdown")


# =====================================================================
# DEMO TOOLS: bulk seed, reset, incidents, today
# =====================================================================

_SEED_STUDENTS = [
    ("s01", "Алиса Джонс", "English, Mathematics", "Europe/London", "United Kingdom"),
    ("s02", "Бен Смит", "Physics", "Europe/London", "United Kingdom"),
    ("s03", "София Гарсия", "Chemistry", "Europe/Paris", "France"),
    ("s04", "Лиам Уильямс", "Mathematics", "Asia/Almaty", "Kazakhstan"),
    ("s05", "Эмма Браун", "English Literature", "Europe/London", "United Kingdom"),
    ("s06", "Ноа Дэвис", "Biology", "Europe/Moscow", "Russia"),
    ("s07", "Оливия Уилсон", "History", "Asia/Dubai", "UAE"),
    ("s08", "Джеймс Тейлор", "Computer Science", "Europe/London", "United Kingdom"),
    ("s09", "Ава Андерсон", "Mathematics, Physics", "Europe/Vienna", "Austria"),
    ("s10", "Уильям Томас", "Chemistry, Biology", "Europe/London", "United Kingdom"),
]

_SEED_TUTORS = [
    ("t01", "Анна Петрова", "English, English Literature"),
    ("t02", "Иван Сидоров", "Mathematics, Physics"),
    ("t03", "Мария Козлова", "Chemistry, Biology"),
]


async def cmd_seed10(upd: Update, ctx) -> None:
    """Создаёт 10 тестовых учеников и 3 репетиторов одной командой.

    Использование:
      /seed10 <parentTG1> [<parentTG2>]

    Ученики s01–s05 привязываются к parentTG1, s06–s10 к parentTG2 (или тоже к parentTG1).
    Репетиторы создаются без TG-привязки (используйте /mh_tutor <cuid> <tg> <имя> для привязки).
    """
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return

    args = ctx.args or []
    if not args:
        await upd.message.reply_text(
            "Использование: `/seed10 <parentTG1> [<parentTG2>]`\n\n"
            "Создаёт 10 учеников и 3 репетиторов.\n"
            "Ученики s01–s05 → parentTG1, s06–s10 → parentTG2.\n"
            "Если один TG — все 10 к нему.",
            parse_mode="Markdown",
        )
        return

    parent_tg1 = args[0]
    parent_tg2 = args[1] if len(args) > 1 else parent_tg1

    # Регистрируем родителей
    urepo = UserRepository()
    existing1 = await urepo.get_by_telegram_id(parent_tg1)
    if not existing1:
        await urepo.set_role_by_telegram(parent_tg1, "parent", name="Parent Group 1")
    if parent_tg2 != parent_tg1:
        existing2 = await urepo.get_by_telegram_id(parent_tg2)
        if not existing2:
            await urepo.set_role_by_telegram(parent_tg2, "parent", name="Parent Group 2")

    srepo = MeritHubStudentRepository()
    mh_api_note = ""

    # Создаём репетиторов
    for cuid, name, _subjects in _SEED_TUTORS:
        mh_id = None
        if settings.merithub_use_real:
            try:
                from src.integrations.factory import get_merithub_service
                from src.integrations.merithub_client import MeritHubClient
                client = get_merithub_service()
                resp = await client.add_user(client_user_id=cuid, name=name, role="C")
                mh_id = MeritHubClient._extract_id(resp, "userId", "id")
            except Exception as e:
                mh_api_note = f"\n⚠️ MeritHub API: {str(e)[:100]}"
        if not mh_id:
            mh_id = f"mh_{cuid}"
        await srepo.upsert(cuid, merithub_user_id=mh_id, name=name, role="tutor")

    # Создаём учеников
    created_students = []
    for i, (cuid, name, subjects, tz, country) in enumerate(_SEED_STUDENTS):
        parent_tg = parent_tg1 if i < 5 else parent_tg2
        mh_id = None
        if settings.merithub_use_real:
            try:
                from src.integrations.factory import get_merithub_service
                from src.integrations.merithub_client import MeritHubClient
                client = get_merithub_service()
                resp = await client.add_user(client_user_id=cuid, name=name, role="M")
                mh_id = MeritHubClient._extract_id(resp, "userId", "id")
            except Exception:
                pass
        if not mh_id:
            mh_id = f"mh_{cuid}"
        await srepo.upsert(
            cuid, merithub_user_id=mh_id, name=name,
            parent_telegram_id=parent_tg, role="student",
            timezone=tz, country=country,
        )
        created_students.append((cuid, name, parent_tg, subjects, tz))

    # Формируем ответ
    lines = [f"✅ Создано 10 учеников и 3 репетитора{mh_api_note}\n"]
    lines.append(f"👨‍👩‍👦 Parent 1 (`{parent_tg1}`): s01–s05")
    if parent_tg2 != parent_tg1:
        lines.append(f"👨‍👩‍👦 Parent 2 (`{parent_tg2}`): s06–s10")
    lines.append("")
    lines.append("🧑‍🏫 Репетиторы:")
    for cuid, name, subjects in _SEED_TUTORS:
        lines.append(f"  `{cuid}` — {name} ({subjects})")
    lines.append("")
    lines.append("🎓 Ученики:")
    for cuid, name, ptg, subjects, tz in created_students:
        lines.append(f"  `{cuid}` — {name} ({subjects}) 🕐 {tz}")
    lines.append("")
    lines.append("Далее: `/mh_schedule t01 <start> 60 s01 s02 s04`")

    await upd.message.reply_text("\n".join(lines), parse_mode="Markdown")


_DEMO_RESET_TABLES = [
    "dead_letter_queue",
    "scheduled_actions",
    "notifications",
    "incidents",
    "workflow_instances",
    "merithub_class_status",
]


async def _demo_reset_counts() -> dict:
    """Считает записи в таблицах демо-сброса (если таблицы нет — 0)."""
    import aiosqlite
    counts = {}
    async with aiosqlite.connect(settings.database_path) as db:
        for t in _DEMO_RESET_TABLES:
            try:
                row = await (await db.execute(f"SELECT COUNT(*) FROM {t}")).fetchone()
                counts[t] = row[0] if row else 0
            except Exception:
                counts[t] = 0
    return counts


async def perform_demo_reset() -> dict:
    """Фактический сброс; возвращает {таблица: сколько было до удаления}."""
    import aiosqlite
    counts = await _demo_reset_counts()
    async with aiosqlite.connect(settings.database_path) as db:
        for t in _DEMO_RESET_TABLES:
            try:
                await db.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        await db.commit()
    logger.info("Demo reset performed: %s", counts)
    return counts


def format_demo_reset_result(counts: dict) -> str:
    lines = ["🗑 Демо-сброс выполнен:\n"]
    for t, c in counts.items():
        lines.append(f"  {t}: удалено {c} записей")
    lines.append("\nПользователи, ученики MeritHub и зачисления сохранены.")
    lines.append("Готово к чистому прогону: `/pilot_absent` или `/mh_schedule ...`")
    return "\n".join(lines)


async def cmd_demo_reset(upd: Update, _ctx) -> None:
    """Сброс демо-данных — только с подтверждением (UX U3: опасное действие).

    Раньше команда мгновенно стирала 6 таблиц — один случайный ввод на живом
    демо = потеря состояния. Теперь: превью последствий + confirm-кнопки."""
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    counts = await _demo_reset_counts()
    lines = ["🗑 *Сбросить демо-данные?*\n"]
    nonempty = {t: c for t, c in counts.items() if c}
    if nonempty:
        for t, c in nonempty.items():
            lines.append(f"  {t}: {c} записей")
    else:
        lines.append("  (таблицы уже пустые)")
    lines.append("\nПользователи, ученики MeritHub и зачисления *сохранятся*.")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, сбросить", callback_data="demo_reset:confirm"),
        InlineKeyboardButton("✖️ Отмена", callback_data="demo_reset:cancel"),
    ]])
    await upd.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb)


async def cmd_incidents(upd: Update, _ctx) -> None:
    """Показывает активные инциденты для координатора."""
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return

    from src.db.repository import IncidentRepository
    repo = IncidentRepository()

    # Активные (pending + escalated)
    active = await repo._fetchall(
        "SELECT * FROM incidents WHERE status IN ('pending', 'escalated', 'open') "
        "ORDER BY created_at DESC LIMIT 20"
    )
    # Последние закрытые
    closed = await repo._fetchall(
        "SELECT * FROM incidents WHERE status='resolved' "
        "ORDER BY resolved_at DESC LIMIT 5"
    )
    # Статистика
    stats = await repo._fetchone(
        "SELECT "
        "  COUNT(CASE WHEN status='pending' THEN 1 END) as pending, "
        "  COUNT(CASE WHEN status='escalated' THEN 1 END) as escalated, "
        "  COUNT(CASE WHEN status='resolved' THEN 1 END) as resolved, "
        "  COUNT(*) as total "
        "FROM incidents"
    )

    lines = [f"📋 Инциденты\n"]
    if stats:
        lines.append(
            f"⏳ Ожидают: {stats['pending']}  |  "
            f"🚨 Эскалации: {stats['escalated']}  |  "
            f"✅ Закрыто: {stats['resolved']}  |  "
            f"Всего: {stats['total']}\n"
        )

    if active:
        lines.append("─── Активные ───")
        for inc in active:
            status_emoji = {"pending": "⏳", "escalated": "🚨", "open": "📌"}.get(inc["status"], "❓")
            lines.append(
                f"{status_emoji} #{inc['id']} [{inc['type']}] урок: `{inc.get('lesson_ref') or '—'}` "
                f"| статус: {inc['status']} | {inc.get('created_at', '')[:16]}"
            )
    else:
        lines.append("✅ Активных инцидентов нет.")

    if closed:
        lines.append("\n─── Последние закрытые ───")
        for inc in closed:
            lines.append(
                f"✅ #{inc['id']} | `{inc.get('lesson_ref') or '—'}` "
                f"| {inc.get('resolution') or '—'} | {(inc.get('resolved_at') or '')[:16]}"
            )

    await upd.message.reply_text("\n".join(lines))


async def cmd_today(upd: Update, _ctx) -> None:
    """Обзор на сегодня: классы, записи, ожидающие действия."""
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return

    from src.db.repository import (
        MeritHubClassRepository, ScheduledActionRepository,
        MeritHubEnrollmentRepository, MeritHubClassStatusRepository,
        WorkflowRepository,
    )
    from datetime import datetime as _dt

    # Классы
    classes = await MeritHubClassRepository().list_all()
    # Ожидающие действия
    sched = ScheduledActionRepository()
    pending_actions = await sched._fetchall(
        "SELECT * FROM scheduled_actions WHERE status='pending' ORDER BY execute_at LIMIT 20"
    )
    # Активные workflow'ы
    wf_repo = WorkflowRepository()
    active_wf = await wf_repo._fetchall(
        "SELECT * FROM workflow_instances WHERE state='running' ORDER BY id DESC LIMIT 10"
    )
    # Инциденты за сегодня
    from src.db.repository import IncidentRepository
    inc_repo = IncidentRepository()
    today_str = _dt.now().strftime("%Y-%m-%d")
    today_incidents = await inc_repo._fetchall(
        "SELECT * FROM incidents WHERE created_at LIKE ? ORDER BY created_at",
        (f"{today_str}%",),
    )

    lines = ["📅 Обзор системы\n"]

    # Классы — фильтруем сегодняшние по локальной дате старта
    today_classes = []
    for c in classes:
        start_str = c.get("start_time", "")
        if start_str and start_str[:10] == today_str:
            today_classes.append(c)

    if today_classes:
        lines.append(f"📚 Занятия сегодня ({len(today_classes)}):")
        for c in today_classes:
            status_row = await MeritHubClassStatusRepository().get(c["class_id"])
            live_status = status_row["last_status"] if status_row else "—"
            live_emoji = {"lv": "🟢", "cp": "✅", "cl": "❌", "ex": "⌛"}.get(live_status, "⚪")
            enr = await MeritHubEnrollmentRepository().list_by_class(c["class_id"])
            student_count = sum(1 for e in enr if (e.get("role") or "student") == "student")
            student_names = [e.get("student_name") or e.get("client_user_id") for e in enr
                            if (e.get("role") or "student") == "student"]
            lines.append(
                f"  {live_emoji} `{c['class_id']}` | {c.get('start_time', '—')[11:16]} "
                f"| 👥 {student_count}: {', '.join(student_names[:3])}"
                + (f" +{len(student_names)-3}" if len(student_names) > 3 else "")
            )
    elif classes:
        lines.append(f"📚 Сегодня занятий нет. Всего классов: {len(classes)}")
    else:
        lines.append("📚 Классов пока нет. Создайте: `/mh_schedule ...`")

    # Инциденты за сегодня
    lines.append("")
    if today_incidents:
        pending_cnt = sum(1 for i in today_incidents if i["status"] in ("pending", "open"))
        resolved_cnt = sum(1 for i in today_incidents if i["status"] == "resolved")
        escalated_cnt = sum(1 for i in today_incidents if i["status"] == "escalated")
        lines.append(
            f"📋 Инциденты сегодня: {len(today_incidents)} "
            f"(⏳ {pending_cnt} | 🚨 {escalated_cnt} | ✅ {resolved_cnt})"
        )
    else:
        lines.append("📋 Инцидентов сегодня нет.")

    # Ожидающие действия
    lines.append("")
    if pending_actions:
        lines.append(f"⏰ Ожидающие действия ({len(pending_actions)}):")
        for a in pending_actions[:10]:
            execute_str = a.get("execute_at", "—")[:19]
            lines.append(f"  [{a['action']}] → {execute_str} | wf#{a['workflow_id']}")
    else:
        lines.append("⏰ Нет ожидающих действий.")

    # Активные workflow'ы
    if active_wf:
        lines.append("")
        lines.append(f"⚙️ Активные workflow ({len(active_wf)}):")
        for w in active_wf[:5]:
            lines.append(f"  #{w['id']} [{w['workflow_type']}]")

    await upd.message.reply_text("\n".join(lines))


async def cmd_morning_digest(upd: Update, _ctx) -> None:
    """Утренняя сводка: какие сегодня занятия, кто задействован, что нужно подтвердить.

    Для прода: запускать по cron/scheduler каждое утро и рассылать координаторам.
    Для демо: вызывается командой.
    """
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return

    from src.db.repository import (
        MeritHubClassRepository, MeritHubEnrollmentRepository,
        MeritHubClassStatusRepository, IncidentRepository,
    )
    from datetime import datetime as _dt

    today_str = _dt.now().strftime("%Y-%m-%d")
    classes = await MeritHubClassRepository().list_all()

    # Фильтруем занятия на сегодня
    today_classes = []
    for c in classes:
        start_str = c.get("start_time", "")
        if start_str and start_str[:10] == today_str:
            today_classes.append(c)

    # Сортируем по времени
    today_classes.sort(key=lambda c: c.get("start_time", ""))

    if not today_classes:
        await upd.message.reply_text(
            f"☀️ Доброе утро!\n\n"
            f"📅 На сегодня ({today_str}) занятий не запланировано.\n\n"
            f"Создать занятие: `/mh_schedule <tutor> <start> <min> <students...>`",
            parse_mode="Markdown",
        )
        return

    lines = [f"☀️ *Доброе утро!*\n📅 Расписание на {today_str}\n"]
    lines.append(f"Занятий сегодня: *{len(today_classes)}*\n")

    total_students = 0
    total_tutors = set()

    for c in today_classes:
        class_id = c["class_id"]
        start_time = c.get("start_time", "—")
        time_str = start_time[11:16] if len(start_time) > 16 else start_time

        enr = await MeritHubEnrollmentRepository().list_by_class(class_id)
        students = [e for e in enr if (e.get("role") or "student") == "student"]
        tutors = [e for e in enr if (e.get("role") or "student") in ("tutor", "teacher")]

        total_students += len(students)
        for t in tutors:
            total_tutors.add(t.get("student_name") or t.get("client_user_id"))

        tutor_name = tutors[0].get("student_name") if tutors else "—"
        student_names = [s.get("student_name") or s.get("client_user_id") for s in students]

        lines.append(
            f"🕐 *{time_str}* — {tutor_name}\n"
            f"   👥 {', '.join(student_names)}\n"
        )

    lines.append(
        f"───\n"
        f"📊 Итого: {len(today_classes)} занятий, "
        f"{total_students} учеников, "
        f"{len(total_tutors)} репетиторов\n\n"
        f"Система автоматически напомнит всех за "
        f"{settings.albion_prelesson_reminder_min} мин до урока."
    )

    await upd.message.reply_text("\n".join(lines))


async def cmd_mh_contact(upd: Update, ctx) -> None:
    """Добавить/обновить контакт ученика или репетитора.

    Использование:
      /mh_contact <clientUserId> phone <номер>
      /mh_contact <clientUserId> email <email>
      /mh_contact <clientUserId> tg <telegram_id>
      /mh_contact <clientUserId> all <phone> <email> [<telegram_id>]

    Примеры:
      /mh_contact s01 phone +447493994501
      /mh_contact s01 email parent@example.com
      /mh_contact s01 tg 333333333
      /mh_contact s01 all +447493994501 parent@example.com 333333333
    """
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return
    args = ctx.args or []
    if len(args) < 3:
        await upd.message.reply_text(
            "Использование: `/mh_contact <clientUserId> <phone|email|tg|all> <значение>`\n\n"
            "Примеры:\n"
            "  `/mh_contact s01 phone +447493994501`\n"
            "  `/mh_contact s01 email parent@example.com`\n"
            "  `/mh_contact s01 tg 333333333`\n"
            "  `/mh_contact s01 all +447493994501 parent@ex.com 333333333`",
            parse_mode="Markdown",
        )
        return

    cuid = args[0]
    field = args[1].lower()
    contact_repo = MeritHubContactRepository()
    srepo = MeritHubStudentRepository()

    # Проверяем, существует ли ученик/репетитор
    student = await srepo.get_by_client_id(cuid)
    if not student:
        await upd.message.reply_text(f"❌ {cuid} не найден. Сначала создайте: /mh_user или /mh_tutor.")
        return

    existing = await contact_repo.get(cuid) or {}
    phone = existing.get("phone")
    email = existing.get("email")
    tg = existing.get("telegram_id")
    role = student.get("role", "student")
    name = student.get("name", cuid)

    if field == "phone":
        phone = " ".join(args[2:])
    elif field == "email":
        email = " ".join(args[2:])
    elif field == "tg":
        tg = args[2]
    elif field == "all":
        if len(args) >= 3:
            phone = args[2]
        if len(args) >= 4:
            email = args[3]
        if len(args) >= 5:
            tg = args[4]
    else:
        await upd.message.reply_text(f"Неизвестное поле {field}. Используйте: phone, email, tg, all")
        return

    await contact_repo.upsert(cuid, telegram_id=tg, role=role, name=name, phone=phone, email=email)

    parts = []
    if phone:
        parts.append(f"📱 {_esc_md(phone)}")
    if email:
        parts.append(f"📧 {_esc_md(email)}")
    if tg:
        parts.append(f"💬 TG `{tg}`")

    await upd.message.reply_text(
        f"✅ Контакт `{cuid}` ({_esc_md(name)}) обновлён:\n" + "\n".join(parts),
        parse_mode="Markdown",
    )


async def cmd_mh_contacts(upd: Update, _ctx) -> None:
    """Список всех контактов."""
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return
    contact_repo = MeritHubContactRepository()
    rows = await contact_repo.list_all()
    if not rows:
        await upd.message.reply_text("Пока нет контактов. Используйте /mh_contact <cuid> phone <номер>")
        return
    lines = ["📇 Контакты:\n"]
    for r in rows:
        parts = [f"• `{r['client_user_id']}` ({_esc_md(r['name'] or '—')}) [{r['role']}]"]
        if r.get("telegram_id"):
            parts.append(f"TG: `{r['telegram_id']}`")
        if r.get("phone"):
            parts.append(f"📱 {_esc_md(r['phone'])}")
        if r.get("email"):
            parts.append(f"📧 {_esc_md(r['email'])}")
        lines.append(" | ".join(parts))
    await upd.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_import_learners(upd: Update, ctx) -> None:
    """Импорт учеников из текстового дампа MeritHub Learners export.

    Формат (TSV — tab-separated, как при копировании из таблицы):
    UserId\\tName\\tRole\\tEmail\\tJoining Time\\tCountry\\tTimezone\\tTags

    Пример использования:
    1. Скопируйте таблицу Learners из MeritHub
    2. Сохраните в файл learners.txt
    3. Отправьте боту: /import_learners (и приложите файл, или вставьте текст)

    Или через reply на сообщение с данными.
    """
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return

    # Получаем текст из reply или из аргументов
    text = ""
    if upd.message.reply_to_message and upd.message.reply_to_message.text:
        text = upd.message.reply_to_message.text
    elif ctx.args:
        text = " ".join(ctx.args)

    if not text or len(text) < 20:
        await upd.message.reply_text(
            "📋 *Импорт учеников из MeritHub*\n\n"
            "1. Скопируйте таблицу Learners из MeritHub (выделите → Ctrl+C)\n"
            "2. Отправьте данные как сообщение боту\n"
            "3. Ответьте на это сообщение командой `/import_learners`\n\n"
            "Формат: UserId, Name, Role, Email, Joining Time, Country, Timezone, Tags",
            parse_mode="Markdown",
        )
        return

    lines = text.strip().split("\n")
    srepo = MeritHubStudentRepository()
    imported = 0
    skipped = 0

    for line in lines[1:]:  # пропускаем header
        parts = line.split("\t")
        if len(parts) < 3:
            skipped += 1
            continue

        mh_user_id = parts[0].strip() if len(parts) > 0 else ""
        name = parts[1].strip() if len(parts) > 1 else ""
        # role = parts[2]  # всегда "Learner"
        email = parts[3].strip() if len(parts) > 3 else None
        # joining_time = parts[4]  # не сохраняем
        country = parts[5].strip() if len(parts) > 5 else None
        timezone = parts[6].strip() if len(parts) > 6 else "Europe/London"
        # tags = parts[7] if len(parts) > 7 else None

        if not mh_user_id or not name:
            skipped += 1
            continue

        # Используем merithub_user_id как client_user_id если нет отдельного
        await srepo.upsert(
            mh_user_id,
            merithub_user_id=mh_user_id,
            name=name,
            email=email,
            timezone=timezone,
            country=country,
            role="student",
        )
        imported += 1

    await upd.message.reply_text(
        f"✅ Импорт завершён\n\n"
        f"Импортировано: {imported} учеников\n"
        f"Пропущено: {skipped} строк\n\n"
        f"Проверить: `/mh_students`\n"
        f"Привязать родителей: `/mh_user <id> <parentTG> <имя>`",
        parse_mode="Markdown",
    )


async def cmd_import_customers(upd: Update, ctx) -> None:
    """Импорт привязок ученик→родитель из MeritHub 'Learner's customers' export.

    Формат (TSV):
    LearnerName\\tLearnerEmail\\tLearnerId\\tCustomerName\\tCustomerEmail\\tCustomerId\\tCustomerPhoneNumber\\t...
    """
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return

    text = ""
    if upd.message.reply_to_message and upd.message.reply_to_message.text:
        text = upd.message.reply_to_message.text
    elif ctx.args:
        text = " ".join(ctx.args)

    if not text or len(text) < 20:
        await upd.message.reply_text(
            "📋 *Импорт привязок ученик→родитель*\n\n"
            "1. Скопируйте таблицу «Learner's customers» из MeritHub\n"
            "2. Отправьте данные как сообщение боту\n"
            "3. Ответьте на сообщение командой `/import_customers`",
            parse_mode="Markdown",
        )
        return

    lines = text.strip().split("\n")
    contact_repo = MeritHubContactRepository()
    imported = 0
    skipped = 0

    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < 5:
            skipped += 1
            continue

        learner_name = parts[0].strip()
        # learner_email = parts[1]
        learner_id = parts[2].strip()
        customer_name = parts[3].strip() if len(parts) > 3 else ""
        customer_email = parts[4].strip() if len(parts) > 4 else None
        # customer_id = parts[5]
        customer_phone = parts[6].strip() if len(parts) > 6 else None
        customer_country = parts[8].strip() if len(parts) > 8 else None
        customer_city = parts[9].strip() if len(parts) > 9 else None

        if not learner_id or not customer_name:
            skipped += 1
            continue

        await contact_repo.upsert(
            learner_id,
            name=customer_name,
            email=customer_email,
            phone=customer_phone,
            country=customer_country,
            city=customer_city,
            role="parent",
        )
        imported += 1

    await upd.message.reply_text(
        f"✅ Импорт привязок завершён\n\n"
        f"Импортировано: {imported} родительских контактов\n"
        f"Пропущено: {skipped} строк\n\n"
        f"Проверить: `/mh_contacts`",
        parse_mode="Markdown",
    )


async def cmd_mh_delete_user(upd: Update, ctx) -> None:
    """Удаляет пользователя из MeritHub: /mh_delete_user <clientUserId>"""
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ.")
        return
    args = ctx.args or []
    if len(args) < 1:
        await upd.message.reply_text("Использование: /mh_delete_user <clientUserId>")
        return
    cuid = args[0]
    srepo = MeritHubStudentRepository()
    student = await srepo.get_by_client_id(cuid)
    if not student:
        await upd.message.reply_text(f"❌ Пользователь {cuid} не найден в локальной БД.")
        return
    mh_id = student.get("merithub_user_id")

    # 1. Сначала удаляем из MeritHub API
    if settings.merithub_use_real and mh_id and not mh_id.startswith("mh_"):
        try:
            from src.integrations.factory import get_merithub_service
            client = get_merithub_service()
            await client.delete_user(mh_id)
            logger.info("MH_SYNC: delete_user %s (%s)", cuid, mh_id)
        except Exception as e:
            await upd.message.reply_text(
                f"❌ Ошибка удаления из MeritHub: {str(e)[:200]}\n"
                f"Локальная запись НЕ удалена. Попробуйте ещё раз.")
            return

    # 2. Удаляем из локальных таблиц (только если MeritHub OK или mock)
    from src.db.repository import MeritHubContactRepository
    await MeritHubStudentRepository()._execute(
        "DELETE FROM merithub_students WHERE client_user_id=?", (cuid,))
    await MeritHubContactRepository()._execute(
        "DELETE FROM merithub_contacts WHERE client_user_id=?", (cuid,))
    await srepo._execute("DELETE FROM merithub_enrollments WHERE client_user_id=?", (cuid,))
    logger.info("MH_SYNC: DB delete merithub_students %s", cuid)

    await upd.message.reply_text(f"✅ Пользователь {cuid} удалён из MeritHub и из базы ALBION.")


def register_pilot_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("pilot_seed", cmd_pilot_seed))
    app.add_handler(CommandHandler("pilot_absent", cmd_pilot_absent))
    app.add_handler(CommandHandler("mh_events", cmd_mh_events))
    app.add_handler(CommandHandler("mh_user", cmd_mh_user))
    app.add_handler(CommandHandler("mh_tutor", cmd_mh_tutor))
    app.add_handler(CommandHandler("mh_enroll", cmd_mh_enroll))
    app.add_handler(CommandHandler("mh_schedule", cmd_mh_schedule))
    app.add_handler(CommandHandler("mh_students", cmd_mh_students))
    app.add_handler(CommandHandler("mh_contact", cmd_mh_contact))
    app.add_handler(CommandHandler("mh_contacts", cmd_mh_contacts))
    app.add_handler(CommandHandler("mh_delete_user", cmd_mh_delete_user))
    app.add_handler(CommandHandler("import_learners", cmd_import_learners))
    app.add_handler(CommandHandler("import_customers", cmd_import_customers))
    # Demo tools
    app.add_handler(CommandHandler("seed10", cmd_seed10))
    app.add_handler(CommandHandler("demo_reset", cmd_demo_reset))
    app.add_handler(CommandHandler("incidents", cmd_incidents))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("morning", cmd_morning_digest))
    logger.info("Pilot handlers registered (/pilot_* /mh_* /seed10 /demo_reset /incidents /today /morning)")
