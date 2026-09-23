"""WhatsApp Business webhook: GET verify-challenge + POST приём сообщений.

Принимает сообщения, сохраняет сырой payload в webhook_events и ставит
немедленные scheduled_actions (inbound_text/inbound_callback) — бот-процесс
подхватывает их scheduler_loop'ом и публикует на своём bus.
Межпроцессный канал — БД (паттерн MeritHub webhook, DECISIONS D3/D4).

Подпись: X-Hub-Signature-256 = sha256 HMAC тела по app_secret.
Без заданного секрета принимаем (тот же принцип, что у MeritHub-ресивера).
"""

import hashlib
import hmac
import json
import logging

from fastapi import Request
from fastapi.responses import PlainTextResponse

from src.channels.inbound import enqueue_inbound
from src.config import settings
from src.db.repository import IdempotencyRepository, WebhookEventRepository

logger = logging.getLogger(__name__)


def verify_signature(body: bytes, signature: str | None, secret: str | None) -> bool:
    """X-Hub-Signature-256 check. Без app_secret — принимаем (открытый режим)."""
    if not secret:
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _norm_phone(raw: str) -> str:
    """Meta шлёт from без '+'; приводим к +E.164 для адресов 'wa:+…'."""
    digits = "".join(c for c in (raw or "") if c.isdigit())
    return f"+{digits}" if digits else ""


def parse_messages(payload: dict) -> list[dict]:
    """WA webhook payload → нормализованные входящие.

    kind='text'     — свободный текст (text.body);
    kind='callback' — interactive button_reply/list_reply.id или template
                      quick-reply button.payload; callback_id = та же
                      грамматика, что у TG callback_data ('resolve:1:abc').
    """
    out: list[dict] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {
                _norm_phone(c.get("wa_id")): ((c.get("profile") or {}).get("name"))
                for c in value.get("contacts") or []
            }
            for msg in value.get("messages") or []:
                phone = _norm_phone(msg.get("from") or "")
                item = {"phone": phone, "wamid": msg.get("id") or "",
                        "name": names.get(phone)}
                mtype = msg.get("type")
                if mtype == "text":
                    body = ((msg.get("text") or {}).get("body") or "").strip()
                    if body:
                        out.append({**item, "kind": "text", "text": body})
                elif mtype == "interactive":
                    inter = msg.get("interactive") or {}
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    if reply.get("id"):
                        out.append({**item, "kind": "callback", "callback_id": reply["id"]})
                elif mtype == "button":
                    cb = (msg.get("button") or {}).get("payload") or ""
                    if cb:
                        out.append({**item, "kind": "callback", "callback_id": cb})
    return out


def register_whatsapp_routes(app) -> None:
    path = settings.whatsapp_webhook_path
    if not settings.whatsapp_app_secret:
        logger.warning(
            "WHATSAPP_APP_SECRET не задан — %s принимает запросы без проверки "
            "подписи (открытый режим, для прода задайте секрет)", path,
        )

    @app.get(path)
    async def wa_verify(request: Request):
        qp = request.query_params
        ok = (qp.get("hub.mode") == "subscribe"
              and bool(settings.whatsapp_verify_token)
              and qp.get("hub.verify_token") == settings.whatsapp_verify_token)
        if ok and qp.get("hub.challenge"):
            return PlainTextResponse(qp["hub.challenge"])
        return PlainTextResponse("Forbidden", status_code=403)

    @app.post(path)
    async def wa_receive(request: Request):
        body = await request.body()
        sig_ok = verify_signature(
            body, request.headers.get("x-hub-signature-256"),
            settings.whatsapp_app_secret)
        repo = WebhookEventRepository()
        try:
            await repo.save("whatsapp", int(sig_ok), dict(request.headers), body)
        except Exception as e:
            logger.error("whatsapp webhook save failed: %s", e)
        if not sig_ok:
            return PlainTextResponse("Invalid signature", status_code=401)
        try:
            items = parse_messages(json.loads(body or b"{}"))
        except Exception as e:
            logger.error("whatsapp webhook parse failed: %s", e)
            return {"status": "ok", "enqueued": 0}

        idem = IdempotencyRepository()
        enqueued = 0
        for it in items:
            key = f"wa_msg:{it['wamid']}" if it["wamid"] else None
            if key and await idem.exists(key):
                continue
            # сначала enqueue — иначе падение между save и enqueue теряет
            # сообщение навсегда (Meta-повтор отсекается ключом идемпотентности)
            await enqueue_inbound(
                "inbound_text" if it["kind"] == "text" else "inbound_callback",
                channel="whatsapp",
                address=it["phone"],
                payload={
                    "text": it.get("text"),
                    "callback_id": it.get("callback_id"),
                    "wamid": it["wamid"],
                    "name": it.get("name"),
                },
            )
            if key:
                await idem.save(key, "whatsapp_webhook", response="enqueued")
            enqueued += 1
        return {"status": "ok", "enqueued": enqueued}
