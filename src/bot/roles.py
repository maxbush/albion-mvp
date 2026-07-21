"""Управление ролями участников пилота по TG-аккаунтам.

Команды бота:
    /whoami                — показать свой TG ID, username и текущую роль
    /role <target> <role>  — назначить роль (только владельцы/админы)
    /roles                 — список всех участников и их ролей (только админы)

Роли: coordinator | tutor | parent | student
Админы (владельцы) задаются в .env:  ALBION_ADMIN_TELEGRAM_IDS=111,222,333

Типовой сценарий пилота:
    1. Каждый владелец пишет боту /start, затем /whoami — узнаёт свой TG ID.
    2. В .env эти TG ID вписаны в ALBION_ADMIN_TELEGRAM_IDS (хотя бы один).
    3. Админ раздаёт роли: /role <TG_ID> coordinator|tutor|parent
"""

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler

from src.config import settings
from src.db.repository import UserRepository

logger = logging.getLogger(__name__)

VALID_ROLES = ("coordinator", "tutor", "parent", "student")
ROLE_EMOJI = {
    "coordinator": "👨‍💼",
    "tutor": "🧑‍🏫",
    "parent": "👨‍👩‍👦",
    "student": "🎓",
}


# =====================================================================
# Хелперы
# =====================================================================

def parse_admin_ids(raw: str | None = None) -> set[str]:
    """Разбирает ALBION_ADMIN_TELEGRAM_IDS ('111, 222,333') → {'111','222','333'}."""
    raw = raw if raw is not None else settings.albion_admin_telegram_ids
    return {p.strip() for p in (raw or "").split(",") if p.strip()}


def is_admin(telegram_id: str | int) -> bool:
    return str(telegram_id) in parse_admin_ids()


async def get_coordinator_ids(db_path: str = "albion.db") -> list[str]:
    """TG ID всех пользователей с ролью coordinator.

    Фолбэк на 'coordinator_1' (демо-сид), чтобы не ломать старый демо-режим."""
    coords = await UserRepository(db_path).list_by_role("coordinator")
    ids = [c["telegram_id"] for c in coords if c.get("telegram_id")]
    if not ids:
        fallback = await UserRepository(db_path).get_by_telegram_id("coordinator_1")
        if fallback:
            ids = ["coordinator_1"]
    return ids


# =====================================================================
# Команды
# =====================================================================

async def cmd_whoami(upd: Update, _ctx) -> None:
    user = upd.effective_user
    rec = await UserRepository().get_by_telegram_id(str(user.id))
    if rec:
        role, emoji = rec["role"], ROLE_EMOJI.get(rec["role"], "")
    else:
        role, emoji = "не назначена", ""
    admin = "✅ да" if is_admin(user.id) else "нет"
    uname = f"@{user.username}" if user.username else "—"
    await upd.message.reply_text(
        "🪪 *Ваш профиль*\n\n"
        f"TG ID: `{user.id}`\n"
        f"Username: {uname}\n"
        f"Имя: {user.full_name or '—'}\n"
        f"Роль: {emoji} {role}\n"
        f"Админ: {admin}\n\n"
        "_Сообщите свой TG ID владельцу — он назначит роль командой /role._",
        parse_mode="Markdown",
    )


async def cmd_role(upd: Update, ctx) -> None:
    actor = upd.effective_user
    if not is_admin(actor.id):
        await upd.message.reply_text(
            "⛔ Только владелец/админ может раздавать роли.\n"
            "Админы задаются в `ALBION_ADMIN_TELEGRAM_IDS` (.env).",
            parse_mode="Markdown",
        )
        return

    args = ctx.args or []
    if len(args) != 2:
        await upd.message.reply_text(
            "Использование: `/role <TG_ID или @username> <роль>`\n"
            f"Роли: {', '.join(VALID_ROLES)}\n"
            "Пример: `/role 123456789 tutor`",
            parse_mode="Markdown",
        )
        return

    target, role = args[0], args[1].lower()
    if role not in VALID_ROLES:
        await upd.message.reply_text(
            f"Неизвестная роль `{role}`. Доступны: {', '.join(VALID_ROLES)}",
            parse_mode="Markdown",
        )
        return

    repo = UserRepository()
    target_clean = target.lstrip("@")

    if target.startswith("@"):
        # По username — только уже зарегистрированный пользователь (у него есть TG ID).
        rec = await repo.get_by_username(target_clean)
        if not rec:
            await upd.message.reply_text(
                f"Пользователь @{target_clean} ещё не заходил в бота.\n"
                "Пусть сначала напишет /start и пришлёт свой TG ID (команда /whoami), "
                f"затем назначьте роль по ID: `/role <TG_ID> {role}`",
                parse_mode="Markdown",
            )
            return
        await repo.update_role(rec["id"], role)
        name, created = rec["name"], False
    elif target_clean.isdigit():
        uid, created = await repo.set_role_by_telegram(target_clean, role)
        name = (await repo.get(uid))["name"]
    else:
        await upd.message.reply_text(
            "Target должен быть числовым TG ID или @username.",
            parse_mode="Markdown",
        )
        return

    verb = "назначена" if not created else "создан пользователь и назначена"
    await upd.message.reply_text(
        f"✅ Роль {ROLE_EMOJI.get(role, '')} *{role}* {verb} для {name} (`{target_clean}`).",
        parse_mode="Markdown",
    )
    logger.info("Role set: %s -> %s by admin %s", target_clean, role, actor.id)


async def cmd_roles(upd: Update, _ctx) -> None:
    if not is_admin(upd.effective_user.id):
        await upd.message.reply_text("⛔ Только владелец/админ может смотреть список ролей.")
        return
    users = await UserRepository().list_all()
    if not users:
        await upd.message.reply_text(
            "Пока никто не зарегистрирован. Пусть участники напишут /start, затем /whoami.")
        return
    lines = ["👥 *Участники и роли*\n"]
    for u in users:
        uname = f" @{u['username']}" if u.get("username") else ""
        star = " ★" if is_admin(u["telegram_id"]) else ""
        lines.append(
            f"{ROLE_EMOJI.get(u['role'], '•')} `{u['telegram_id']}`{uname} — "
            f"*{u['role']}*{star} — {u['name']}"
        )
    await upd.message.reply_text("\n".join(lines), parse_mode="Markdown")


# =====================================================================
# Регистрация
# =====================================================================

def register_role_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("role", cmd_role))
    app.add_handler(CommandHandler("roles", cmd_roles))
    logger.info("Role handlers registered (/whoami /role /roles)")
