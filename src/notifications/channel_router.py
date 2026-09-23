"""Маршрутизатор каналов уведомлений (Channel Router).

Реализует мультиканальную доставку: Telegram Bot + WhatsApp Business (Meta Cloud API).
Выбирает оптимальный канал доставки на основе:
  1. Явно запрошенного канала (channel='whatsapp' / channel='telegram')
  2. Настроек пользователя (preferred_channel в users / merithub_contacts)
  3. Доступных идентификаторов (наличие phone vs telegram_id)
  4. Авто-фолбэка при недоступности основного канала.
"""

import asyncio
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.config import settings
from src.db.repository import (
    MeritHubContactRepository, NotificationRepository, UserRepository,
    WorkflowRepository,
)
from src.integrations.factory import get_whatsapp_service
from src.integrations.whatsapp_client import normalize_phone

logger = logging.getLogger(__name__)


class ChannelRouter:
    """Интеллектуальный маршрутизатор уведомлений между Telegram и WhatsApp."""

    def __init__(self, bot_app=None, db_path: str | None = None):
        self.bot_app = bot_app
        self.db_path = db_path
        self.users = UserRepository(db_path)
        self.contacts = MeritHubContactRepository(db_path)
        self.notif_repo = NotificationRepository(db_path)

    async def resolve_target(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Определяет каналы, телефоны и TG ID для получателя."""
        explicit_channel = payload.get("channel")  # 'telegram', 'whatsapp', 'auto'
        tg = str(payload.get("telegram_id") or "").strip() or None
        phone = str(payload.get("phone") or "").strip() or None
        cuid = payload.get("client_user_id")

        user_row = None
        contact_row = None

        # Ищем по telegram_id
        if tg and not phone:
            user_row = await self.users.get_by_telegram_id(tg)
            if user_row and user_row.get("phone"):
                phone = user_row.get("phone")
            else:
                crows = await self.contacts._fetchall(
                    "SELECT * FROM merithub_contacts WHERE telegram_id=?", (tg,)
                )
                if crows and crows[0].get("phone"):
                    phone = crows[0].get("phone")

        # Ищем по phone
        if phone and not tg:
            clean = normalize_phone(phone)
            # Ищем в contacts
            crows = await self.contacts._fetchall(
                "SELECT * FROM merithub_contacts WHERE phone LIKE ?", (f"%{clean[-10:]}%",)
            )
            if crows:
                contact_row = crows[0]
                if contact_row.get("telegram_id"):
                    tg = contact_row.get("telegram_id")

            # Ищем в users
            urows = await self.users._fetchall(
                "SELECT * FROM users WHERE phone LIKE ?", (f"%{clean[-10:]}%",)
            )
            if urows:
                user_row = urows[0]
                if user_row.get("telegram_id"):
                    tg = user_row.get("telegram_id")

        # Ищем по cuid
        if cuid and (not tg or not phone):
            c = await self.contacts.get(cuid)
            if c:
                contact_row = c
                if not tg and c.get("telegram_id"):
                    tg = c.get("telegram_id")
                if not phone and c.get("phone"):
                    phone = c.get("phone")

        preferred = None
        if user_row and user_row.get("preferred_channel"):
            preferred = user_row.get("preferred_channel")
        elif contact_row and contact_row.get("preferred_channel"):
            preferred = contact_row.get("preferred_channel")

        # Окончательный выбор канала
        chosen_channel = "telegram"
        if explicit_channel in ("whatsapp", "telegram"):
            chosen_channel = explicit_channel
        elif preferred in ("whatsapp", "telegram"):
            chosen_channel = preferred
        elif phone and not tg:
            chosen_channel = "whatsapp"
        elif tg:
            chosen_channel = "telegram"

        return {
            "channel": chosen_channel,
            "telegram_id": tg,
            "phone": normalize_phone(phone) if phone else None,
            "user_row": user_row,
            "contact_row": contact_row,
        }

    async def send(self, payload: dict[str, Any]) -> bool:
        """Маршрутизирует и отправляет сообщение."""
        msg = payload.get("message") or payload.get("content") or ""
        if not msg:
            logger.warning("ChannelRouter.send: empty message, skipping")
            return False

        buttons = payload.get("buttons") or []
        cb_data = payload.get("callback_data")
        if not buttons and cb_data:
            buttons = [{"text": "✅ Всё в порядке", "callback_data": cb_data}]

        target = await self.resolve_target(payload)
        channel = target["channel"]
        tg = target["telegram_id"]
        phone = target["phone"]

        # Если выбран WhatsApp, но нет телефона — fallback на Telegram
        if channel == "whatsapp" and not phone and tg:
            logger.info("ChannelRouter: WhatsApp chosen but no phone, falling back to TG %s", tg)
            channel = "telegram"
        # Если выбран Telegram, но нет TG — fallback на WhatsApp
        elif channel == "telegram" and not tg and phone:
            logger.info("ChannelRouter: Telegram chosen but no TG, falling back to WA %s", phone)
            channel = "whatsapp"

        nid = payload.get("notification_id")
        wf_id = payload.get("workflow_id")
        success = False
        last_error = None

        if channel == "whatsapp":
            if not phone:
                logger.error("ChannelRouter: cannot send via WhatsApp, no phone number")
                return False
            wa_service = get_whatsapp_service()
            for attempt in range(3):
                try:
                    if buttons:
                        await wa_service.send_interactive_buttons(
                            to_phone=phone,
                            body_text=msg,
                            buttons=buttons,
                        )
                    else:
                        await wa_service.send_text(to_phone=phone, text=msg)
                    success = True
                    logger.info("ChannelRouter: sent to WhatsApp %s (attempt %d)", phone, attempt + 1)
                    break
                except Exception as e:
                    last_error = e
                    if attempt < 2:
                        await asyncio.sleep([1, 2][attempt])
                    logger.warning("WhatsApp send to %s attempt %d failed: %s", phone, attempt + 1, e)

        elif channel == "telegram":
            if not tg:
                logger.error("ChannelRouter: cannot send via Telegram, no telegram_id")
                return False

            # Проверка kill switch
            try:
                from src.bot.handlers import can_send_async
                if not await can_send_async(tg):
                    logger.info("Kill switch blocked msg to %s", tg)
                    return False
            except Exception:
                pass

            if not self.bot_app or not hasattr(self.bot_app, "bot"):
                logger.warning("ChannelRouter: bot_app not available for TG send")
                return False

            for attempt in range(3):
                try:
                    reply_markup = None
                    if buttons:
                        reply_markup = InlineKeyboardMarkup([
                            [InlineKeyboardButton(
                                btn["text"],
                                callback_data=btn.get("callback_data"),
                                url=btn.get("url"),
                            )] for btn in buttons
                        ])
                    if reply_markup:
                        await self.bot_app.bot.send_message(chat_id=tg, text=msg, reply_markup=reply_markup)
                    else:
                        await self.bot_app.bot.send_message(chat_id=tg, text=msg)
                    success = True
                    logger.info("ChannelRouter: sent to Telegram %s (attempt %d)", tg, attempt + 1)
                    break
                except Exception as e:
                    last_error = e
                    if attempt < 2:
                        await asyncio.sleep([1, 2][attempt])
                    logger.warning("Telegram send to %s attempt %d failed: %s", tg, attempt + 1, e)

        # Обновляем репозитории
        if success:
            if nid:
                await self.notif_repo.mark_sent(nid)
            return True
        else:
            logger.error("ChannelRouter: all attempts failed for channel=%s: %s", channel, last_error)
            if nid:
                await self.notif_repo.mark_failed(nid, str(last_error))
            if wf_id:
                await WorkflowRepository(self.db_path).update_state(wf_id, "failed", {"error": str(last_error)})
            return False
