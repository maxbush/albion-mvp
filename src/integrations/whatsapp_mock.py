"""Mock-реализация WhatsApp Business Platform для тестов и локальной разработки.

Позволяет тестировать сценарии отправки, интерактивных кнопок и статусов
без наличия боевого аккаунта Meta Business.
"""

import logging
from typing import Any

from src.integrations.whatsapp_client import normalize_phone

logger = logging.getLogger(__name__)


class MockWhatsAppService:
    """Mock-сервис WhatsApp для тестов."""

    def __init__(self):
        self.sent_messages: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.sent_messages.clear()

    async def send_text(self, to_phone: str, text: str) -> dict[str, Any]:
        clean_phone = normalize_phone(to_phone)
        msg_id = f"wamid.mock_{len(self.sent_messages) + 1}"
        record = {
            "type": "text",
            "to": clean_phone,
            "text": text,
            "id": msg_id,
        }
        self.sent_messages.append(record)
        logger.info("[MockWhatsApp] Text to %s: %s", clean_phone, text[:60])
        return {
            "messaging_product": "whatsapp",
            "contacts": [{"input": to_phone, "wa_id": clean_phone}],
            "messages": [{"id": msg_id}],
        }

    async def send_interactive_buttons(
        self,
        to_phone: str,
        body_text: str,
        buttons: list[dict[str, str]],
        header_text: str | None = None,
    ) -> dict[str, Any]:
        clean_phone = normalize_phone(to_phone)
        msg_id = f"wamid.mock_{len(self.sent_messages) + 1}"
        record = {
            "type": "interactive",
            "to": clean_phone,
            "body": body_text,
            "buttons": buttons[:3],
            "header": header_text,
            "id": msg_id,
        }
        self.sent_messages.append(record)
        logger.info("[MockWhatsApp] Interactive to %s (%d buttons): %s",
                    clean_phone, len(buttons[:3]), body_text[:60])
        return {
            "messaging_product": "whatsapp",
            "contacts": [{"input": to_phone, "wa_id": clean_phone}],
            "messages": [{"id": msg_id}],
        }

    async def send_template(
        self,
        to_phone: str,
        template_name: str,
        language_code: str = "ru",
        components: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        clean_phone = normalize_phone(to_phone)
        msg_id = f"wamid.mock_{len(self.sent_messages) + 1}"
        record = {
            "type": "template",
            "to": clean_phone,
            "template": template_name,
            "language": language_code,
            "components": components or [],
            "id": msg_id,
        }
        self.sent_messages.append(record)
        logger.info("[MockWhatsApp] Template '%s' to %s", template_name, clean_phone)
        return {
            "messaging_product": "whatsapp",
            "contacts": [{"input": to_phone, "wa_id": clean_phone}],
            "messages": [{"id": msg_id}],
        }
