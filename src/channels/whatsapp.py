"""WhatsApp Cloud API — отправитель и mock (по образцу merithub_*).

Ограничения канала, учтённые здесь:
- вне 24-часового окна диалога — только template-сообщения (approve у Meta);
- interactive reply-кнопок максимум 3, list — до 10 опций;
- URL-кнопок в session-сообщениях нет → url-кнопки уходят строками в текст
  («{label}: {url}»), callback-кнопки — interactive.

Адрес канала — телефон E.164 без '+' допускается, но храним с '+'.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from src.channels.base import ChannelButton, ChannelSender, SendResult
from src.config import settings

logger = logging.getLogger(__name__)

_WA_MAX_REPLY_BUTTONS = 3
_WA_MAX_LIST_ROWS = 10
_WA_BUTTON_TITLE_MAX = 20
_WA_ROW_TITLE_MAX = 24
_WA_WINDOW = timedelta(hours=24)  # окно свободных сообщений Meta


class WhatsAppClient:
    """Низкоуровневый клиент Meta WhatsApp Cloud API."""

    def __init__(
        self,
        token: str | None = None,
        phone_number_id: str | None = None,
        graph_version: str | None = None,
    ):
        self._token = token or settings.whatsapp_token
        self._phone_number_id = phone_number_id or settings.whatsapp_phone_number_id
        ver = graph_version or settings.whatsapp_graph_version
        self._base = f"https://graph.facebook.com/{ver}/{self._phone_number_id}/messages"

    async def _post(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                self._base,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"WhatsApp API {resp.status_code}: {resp.text[:300]}")
            return resp.json()

    @staticmethod
    def _envelope(to: str, type_: str, body: dict) -> dict:
        return {"messaging_product": "whatsapp", "to": to.lstrip("+"),
                "type": type_, type_: body}

    async def send_text(self, to: str, text: str) -> dict:
        return await self._post(
            self._envelope(to, "text", {"body": text, "preview_url": True}))

    async def send_reply_buttons(self, to: str, text: str, buttons: list[ChannelButton]) -> dict:
        if len(buttons) > _WA_MAX_REPLY_BUTTONS:
            raise ValueError(f"reply buttons max {_WA_MAX_REPLY_BUTTONS}, got {len(buttons)}")
        return await self._post(self._envelope(to, "interactive", {
            "type": "button",
            "body": {"text": text},
            "action": {"buttons": [
                {"type": "reply", "reply": {
                    "id": (b.callback_id or "")[:256],
                    "title": b.label[:_WA_BUTTON_TITLE_MAX],
                }} for b in buttons
            ]},
        }))

    async def send_list(self, to: str, text: str, buttons: list[ChannelButton],
                        list_button: str = "Выбрать") -> dict:
        rows = buttons[:_WA_MAX_LIST_ROWS]
        return await self._post(self._envelope(to, "interactive", {
            "type": "list",
            "body": {"text": text},
            "action": {
                "button": list_button[:_WA_BUTTON_TITLE_MAX],
                "sections": [{"rows": [
                    {"id": (b.callback_id or "")[:256],
                     "title": b.label[:_WA_ROW_TITLE_MAX]} for b in rows
                ]}],
            },
        }))

    async def send_template(self, to: str, template: str, lang: str = "ru",
                            components: list | None = None) -> dict:
        body: dict = {"name": template, "language": {"code": lang}}
        if components:
            body["components"] = components
        return await self._post(
            self._envelope(to, "template", body))


class MockWhatsAppClient:
    """In-memory клиент для тестов/демо — тот же интерфейс, что WhatsAppClient."""

    def __init__(self):
        self.sent: list[dict] = []
        self._seq = 0

    async def _record(self, kind: str, to: str, payload: dict) -> dict:
        self._seq += 1
        self.sent.append({"kind": kind, "to": to, **payload})
        return {"messages": [{"id": f"wamid.mock-{self._seq}"}]}

    async def send_text(self, to: str, text: str) -> dict:
        return await self._record("text", to, {"text": text})

    async def send_reply_buttons(self, to: str, text: str, buttons: list[ChannelButton]) -> dict:
        return await self._record("reply_buttons", to, {"text": text, "buttons": buttons})

    async def send_list(self, to: str, text: str, buttons: list[ChannelButton],
                        list_button: str = "Выбрать") -> dict:
        return await self._record("list", to, {"text": text, "buttons": buttons})

    async def send_template(self, to: str, template: str, lang: str = "ru",
                            components: list | None = None) -> dict:
        return await self._record("template", to, {
            "template": template, "lang": lang, "components": components})


class WhatsAppSender(ChannelSender):
    name = "whatsapp"

    def __init__(self, client, db_path: str | None = None):
        self._client = client
        self._db_path = db_path

    async def _window_open(self, address: str) -> bool:
        """24h-окно Meta: открыто, если получатель писал нам за последние 24ч
        (отметку ставит enqueue_inbound). Вне окна — только шаблоны."""
        try:
            from src.channels.inbound import normalize_phone
            from src.db.repository import SystemSettingsRepository
            raw = await SystemSettingsRepository(self._db_path).get(
                f"wa_window:{normalize_phone(address)}")
            if not raw:
                return False
            marked = datetime.fromisoformat(raw)
            if marked.tzinfo is None:
                marked = marked.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) - marked < _WA_WINDOW
        except Exception:
            logger.exception("wa_window check failed for %s", address)
            return True  # при ошибке чтения не блокируем отправку

    async def _send_template(
        self,
        address: str,
        text: str,
        cb_buttons: list[ChannelButton],
    ) -> dict:
        """Business-initiated уведомление approved-шаблоном.

        Шаблон: body '{{1}}' + до 3 quick_reply кнопок (payload — наш
        callback_id, Meta вернёт его в webhook как button.payload).
        Кнопок больше 3 — лишние уходят нумерованным списком в тексте.
        """
        shown = cb_buttons[:_WA_MAX_REPLY_BUTTONS]
        extra = cb_buttons[_WA_MAX_REPLY_BUTTONS:]
        if extra:
            opts = "\n".join(f"{i + _WA_MAX_REPLY_BUTTONS + 1}. {b.label}"
                             for i, b in enumerate(extra))
            text = f"{text}\n\n{opts}"
        components: list[dict] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": text[:1024]}],
        }]
        for i, b in enumerate(shown):
            components.append({
                "type": "button", "sub_type": "quick_reply", "index": str(i),
                "parameters": [{"type": "payload", "payload": b.callback_id[:128]}],
            })
        return await self._client.send_template(
            address, settings.whatsapp_notification_template,
            settings.whatsapp_template_lang, components)

    async def send(
        self,
        address: str,
        text: str,
        buttons: list[ChannelButton] | None = None,
    ) -> SendResult:
        url_buttons = [b for b in (buttons or []) if b.url]
        cb_buttons = [b for b in (buttons or []) if b.callback_id and not b.url]
        # Session-сообщение WA не умеет URL-кнопки — ссылки уходят строками.
        for b in url_buttons:
            text = f"{text}\n{b.label}: {b.url}"
        # Вне 24h-окна free-form запрещён Meta → approved-шаблон,
        # если он настроен; без шаблона — пробуем session (dev/mock).
        if (settings.whatsapp_notification_template
                and not await self._window_open(address)):
            resp = await self._send_template(address, text, cb_buttons)
        elif not cb_buttons:
            resp = await self._client.send_text(address, text)
        elif len(cb_buttons) <= _WA_MAX_REPLY_BUTTONS:
            resp = await self._client.send_reply_buttons(address, text, cb_buttons)
        else:
            resp = await self._client.send_list(address, text, cb_buttons)
        wamid = None
        try:
            wamid = (resp.get("messages") or [{}])[0].get("id")
        except Exception:
            pass
        return SendResult(ok=True, channel=self.name, message_id=wamid)
