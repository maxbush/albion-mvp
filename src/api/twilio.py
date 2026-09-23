"""/twilio/whatsapp webhook — приём входящих сообщений Twilio → event bus.

Twilio шлёт application/x-www-form-urlencoded (не JSON):
From="whatsapp:+7999…", To=…, Body, MessageSid, ProfileName,
ButtonText/ButtonPayload (quick-reply из approved Content Template).

Проверка подписи: X-Twilio-Signature — base64(HMAC-SHA1(auth_token,
URL + отсортированные form-параметры)); без TWILIO_AUTH_TOKEN — открытый
режим (тот же принцип, что у остальных ресиверов).

Развёртка нумерованных ответов: если Body — "1".."99" и для адреса есть
button map (пишет TwilioSender при отправке кнопок), публикуем
inbound_callback с исходным callback_id — та же грамматика, что у TG/Meta.
"""

import json
import logging
from urllib.parse import parse_qsl

from fastapi import Request
from fastapi.responses import Response

from src.channels.inbound import enqueue_inbound
from src.channels.twilio import load_button_map, verify_twilio_signature
from src.config import settings
from src.db.repository import IdempotencyRepository, WebhookEventRepository

logger = logging.getLogger(__name__)


def parse_form(form: dict) -> dict | None:
    """Twilio form → нормализованное входящее {kind, phone, sid, name, ...}.

    kind='callback' — ButtonPayload (Content Template quick-reply)
                      или цифра по button map (resolve на вызывающей стороне —
                      там есть доступ к БД);
    kind='text'     — свободный текст Body.
    """
    phone = (form.get("From") or "").removeprefix("whatsapp:")
    if not phone:
        return None
    item = {"phone": phone,
            "sid": form.get("MessageSid") or "",
            "name": form.get("ProfileName")}
    cb = form.get("ButtonPayload")
    body = (form.get("Body") or "").strip()
    if cb:
        return {**item, "kind": "callback", "callback_id": cb}
    if body:
        return {**item, "kind": "text", "text": body}
    return None


def register_twilio_routes(app) -> None:
    """Twilio не требует GET-challenge — только POST с подписью."""

    @app.post(settings.twilio_webhook_path)
    async def twilio_receive(request: Request):
        raw = await request.body()
        # form-urlencoded без python-multipart — поля Twilio плоские.
        form = dict(parse_qsl(raw.decode("utf-8", "replace")))

        sig_ok = verify_twilio_signature(
            str(request.url), form,
            request.headers.get("X-Twilio-Signature"),
            settings.twilio_auth_token)
        repo = WebhookEventRepository()
        try:
            await repo.save("twilio", int(sig_ok), dict(request.headers), raw)
        except Exception as e:
            logger.error("twilio webhook save failed: %s", e)
        if not sig_ok:
            return Response(status_code=403)

        item = parse_form(form)
        if item is None:
            return _twiml()

        if item["sid"]:
            idem = IdempotencyRepository()
            key = f"tw_msg:{item['sid']}"
            if await idem.exists(key):
                return _twiml()
            await idem.save(key, "twilio_webhook", response="enqueued")

        kind = "inbound_text"
        callback_id = item.get("callback_id")
        text = item.get("text")
        if callback_id is None and text and text.isdigit() and len(text) <= 2:
            # Нумерованный ответ на кнопки TwilioSender → обратно в callback_id.
            cb_ids = await load_button_map(item["phone"])
            idx = int(text) - 1
            if 0 <= idx < len(cb_ids):
                callback_id = cb_ids[idx]
                text = None
        if callback_id:
            kind = "inbound_callback"

        await enqueue_inbound(
            kind,
            channel="whatsapp",
            address=item["phone"],
            payload={
                "text": text,
                "callback_id": callback_id,
                "wamid": item["sid"],
                "name": item.get("name"),
            },
        )
        return _twiml()

    logger.info("Twilio webhook registered at %s", settings.twilio_webhook_path)


def _twiml() -> Response:
    """Пустой TwiML-ответ: Twilio не ждёт автоответа от вебхука."""
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response/>',
        media_type="application/xml")
