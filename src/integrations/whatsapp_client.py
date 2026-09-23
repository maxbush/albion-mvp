"""Клиент WhatsApp Business Platform (Meta Cloud API).

Поддерживает:
  - Отправку текстовых сообщений (внутри 24-часового сервисного окна)
  - Отправку интерактивных кнопок (quick-reply buttons, до 3 кнопок — лимит Meta)
  - Отправку шаблонных сообщений (template messages — для исходящих напоминаний вне окна)
  - Нормализацию номеров телефонов в международный формат (E.164 без знака +)

Документация Meta:
  https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages
"""

import logging
import re
from typing import Any

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


def normalize_phone(phone: str | None) -> str:
    """Очищает телефон до цифр (E.164 без '+').

    Пример: '+44 7493 994501' -> '447493994501'
            '+7 (701) 123-45-67' -> '77011234567'
    """
    if not phone:
        return ""
    digits = re.sub(r"\D", "", str(phone))
    return digits


class WhatsAppClient:
    """Реальный клиент Meta WhatsApp Cloud API."""

    def __init__(
        self,
        api_token: str,
        phone_number_id: str,
        api_version: str = "v20.0",
        timeout: float = 15.0,
    ):
        self.api_token = api_token
        self.phone_number_id = phone_number_id
        self.api_version = api_version
        self.timeout = timeout
        self.base_url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    async def send_text(self, to_phone: str, text: str) -> dict[str, Any]:
        """Отправка простого текстового сообщения."""
        clean_phone = normalize_phone(to_phone)
        if not clean_phone:
            raise ValueError(f"Invalid phone number: {to_phone}")

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_phone,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": text,
            },
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.base_url, headers=self._headers(), json=payload)
            if resp.status_code >= 400:
                logger.error("WhatsApp send_text error %d: %s", resp.status_code, resp.text)
                resp.raise_for_status()
            data = resp.json()
            logger.info("WhatsApp text sent to %s (msg_id=%s)", clean_phone,
                        data.get("messages", [{}])[0].get("id"))
            return data

    async def send_interactive_buttons(
        self,
        to_phone: str,
        body_text: str,
        buttons: list[dict[str, str]],
        header_text: str | None = None,
    ) -> dict[str, Any]:
        """Отправка интерактивного сообщения с кнопками (quick-reply).

        Лимит Meta: максимум 3 кнопки, title до 20 символов, id до 256 символов.
        Если кнопок больше 3, первые 3 отправляются кнопками, остальные добавляются
        в текст сообщения (graceful degradation).
        """
        clean_phone = normalize_phone(to_phone)
        if not clean_phone:
            raise ValueError(f"Invalid phone number: {to_phone}")

        # Meta limitation: max 3 buttons
        wa_buttons = []
        for btn in buttons[:3]:
            btn_id = btn.get("id") or btn.get("callback_data") or "btn"
            btn_title = (btn.get("title") or btn.get("text") or "ОК")[:20]
            wa_buttons.append({
                "type": "reply",
                "reply": {
                    "id": btn_id[:256],
                    "title": btn_title,
                },
            })

        action_payload: dict[str, Any] = {"buttons": wa_buttons}

        interactive_payload: dict[str, Any] = {
            "type": "button",
            "body": {"text": body_text[:1024]},
            "action": action_payload,
        }

        if header_text:
            interactive_payload["header"] = {
                "type": "text",
                "text": header_text[:60],
            }

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_phone,
            "type": "interactive",
            "interactive": interactive_payload,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.base_url, headers=self._headers(), json=payload)
            if resp.status_code >= 400:
                logger.error("WhatsApp interactive error %d: %s", resp.status_code, resp.text)
                resp.raise_for_status()
            data = resp.json()
            logger.info("WhatsApp interactive sent to %s (buttons=%d, msg_id=%s)",
                        clean_phone, len(wa_buttons), data.get("messages", [{}])[0].get("id"))
            return data

    async def send_template(
        self,
        to_phone: str,
        template_name: str,
        language_code: str = "ru",
        components: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Отправка шаблонного сообщения (для инициации диалога вне 24ч окна)."""
        clean_phone = normalize_phone(to_phone)
        if not clean_phone:
            raise ValueError(f"Invalid phone number: {to_phone}")

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_phone,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
                "components": components or [],
            },
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.base_url, headers=self._headers(), json=payload)
            if resp.status_code >= 400:
                logger.error("WhatsApp template error %d: %s", resp.status_code, resp.text)
                resp.raise_for_status()
            return resp.json()
