"""Twilio WhatsApp — отправитель и mock (альтернативный провайдер, PR8).

Клиент изначально планировал WhatsApp через Twilio (ALBION_CONTEXT.md);
выбор провайдера — env WHATSAPP_PROVIDER=meta|twilio.

Отличия от Meta Cloud API:
- REST: POST /2010-04-01/Accounts/{SID}/Messages.json (basic auth SID:TOKEN),
  From/To в формате "whatsapp:+E.164";
- интерактивных session-кнопок в песочнице нет (только approved Content
  templates) → callback-кнопки рендерим нумерованным списком, а маппинг
  "цифра → callback_id" сохраняем в system_settings; входящее "2"
  webhook распаковывает обратно в inbound_callback (та же грамматика
  callback_id, что у TG/WA interactive).

Адрес канала — телефон E.164 (с '+' или без — нормализуем к "+E.164").
"""

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from src.channels.base import ChannelButton, ChannelSender, SendResult
# Общий маппинг нумерованных кнопок WA-провайдеров (бывш. twilio_btns:*).
from src.channels.inbound import load_button_map, save_button_map  # noqa: F401
from src.config import settings

logger = logging.getLogger(__name__)

_WA_WINDOW = timedelta(hours=24)  # customer-service окно Meta (то же, что у WA)


def _to_wa(address: str) -> str:
    """'+79991112233' → 'whatsapp:+79991112233'."""
    a = address.strip().removeprefix("whatsapp:")
    return f"whatsapp:+{a.lstrip('+')}"


def verify_twilio_signature(url: str, params: dict, signature: str | None,
                            auth_token: str | None) -> bool:
    """X-Twilio-Signature = base64(HMAC-SHA1(auth_token, url + sorted params)).

    Без auth_token — принимаем (открытый режим, как у остальных ресиверов).
    """
    if not auth_token:
        return True
    if not signature:
        return False
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    expected = base64.b64encode(
        hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)





class TwilioClient:
    """Низкоуровневый клиент Twilio REST Messages API."""

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        wa_from: str | None = None,
    ):
        self._sid = account_sid or settings.twilio_account_sid
        self._token = auth_token or settings.twilio_auth_token
        self._from = wa_from or settings.twilio_whatsapp_from
        self._base = (f"https://api.twilio.com/2010-04-01/Accounts/"
                      f"{self._sid}/Messages.json")

    async def send_text(self, to: str, text: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                self._base,
                data={"From": self._from, "To": _to_wa(to), "Body": text},
                auth=(self._sid, self._token),
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"Twilio API {resp.status_code}: {resp.text[:300]}")
            return resp.json()

    async def send_template(self, to: str, content_sid: str,
                            variables: dict | None = None) -> dict:
        """Approved Content Template (для продового спама-окна/кнопок)."""
        data = {"From": self._from, "To": _to_wa(to), "ContentSid": content_sid}
        if variables:
            data["ContentVariables"] = json.dumps(variables)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(self._base, data=data,
                                     auth=(self._sid, self._token))
            if resp.status_code >= 400:
                raise RuntimeError(f"Twilio API {resp.status_code}: {resp.text[:300]}")
            return resp.json()


class MockTwilioClient:
    """In-memory клиент — тот же интерфейс, что TwilioClient."""

    def __init__(self):
        self.sent: list[dict] = []
        self._seq = 0

    async def _record(self, kind: str, to: str, payload: dict) -> dict:
        self._seq += 1
        self.sent.append({"kind": kind, "to": to, **payload})
        return {"sid": f"SM.mock-{self._seq}"}

    async def send_text(self, to: str, text: str) -> dict:
        return await self._record("text", to, {"text": text})

    async def send_template(self, to: str, content_sid: str,
                            variables: dict | None = None) -> dict:
        return await self._record("template", to,
                                  {"content_sid": content_sid, "variables": variables})


class TwilioSender(ChannelSender):
    """WhatsApp-доставка через Twilio. name='whatsapp' — для роутера это
    тот же канал, провайдер — деталь реализации."""
    name = "whatsapp"

    def __init__(self, client, db_path: str | None = None):
        self._client = client
        self._db_path = db_path

    async def send(
        self,
        address: str,
        text: str,
        buttons: list[ChannelButton] | None = None,
    ) -> SendResult:
        url_buttons = [b for b in (buttons or []) if b.url]
        cb_buttons = [b for b in (buttons or []) if b.callback_id and not b.url]
        # URL-кнопки — ссылками в тексте (как у Meta-отправителя).
        for b in url_buttons:
            text = f"{text}\n{b.label}: {b.url}"
        if cb_buttons:
            # Песочница/базовый Twilio не даёт session-кнопок — нумеруем;
            # маппинг в БД читает /twilio/whatsapp webhook.
            opts = "\n".join(f"{i}. {b.label}" for i, b in enumerate(cb_buttons, 1))
            text = f"{text}\n\nОтветьте цифрой:\n{opts}"
            try:
                await save_button_map(address, [b.callback_id for b in cb_buttons],
                                      db_path=self._db_path)
            except Exception:
                logger.exception("twilio: не удалось сохранить button map для %s", address)
        # Вне 24h-окна Meta отклонит free-form Body — только approved Content
        # Template. Без TWILIO_CONTENT_SID fallback'а нет → явный провал.
        if settings.twilio_content_sid and not await self._window_open(address):
            resp = await self._client.send_template(
                address, settings.twilio_content_sid, {"1": text[:1600]})
        else:
            resp = await self._client.send_text(address, text)
        return SendResult(ok=True, channel=self.name,
                          message_id=resp.get("sid"))

    async def _window_open(self, address: str) -> bool:
        """24h-окно: свежесть последнего входящего от адреса (wa_window:*)."""
        from src.channels.inbound import normalize_phone
        from src.db.repository import SystemSettingsRepository
        raw = await SystemSettingsRepository(self._db_path).get(
            f"wa_window:{normalize_phone(address)}")
        try:
            return (datetime.now(timezone.utc)
                    - datetime.fromisoformat(raw)) < _WA_WINDOW
        except (TypeError, ValueError):
            # битое значение → fail closed (наружу шлём шаблоном)
            return False
