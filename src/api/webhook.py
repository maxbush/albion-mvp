"""Приёмник вебхуков: MeritHub и WhatsApp Meta Cloud API + Мониторинг.

1. MeritHub (push-модель):
   - Attendance -> авто-неявка
   - classStatus -> фиксация live/completed
2. WhatsApp (Meta Cloud API):
   - GET /whatsapp/webhook: валидация токена (hub.challenge)
   - POST /whatsapp/webhook: приём входящих текстовых сообщений, нажатий кнопок
     и статусов доставки.
3. Мониторинг и Observability:
   - GET /health: проверка здоровья сервиса и БД
   - GET /metrics: операционные метрики для масштаба 100+ тьюторов
"""

import base64
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from src.config import settings
from src.db.repository import WebhookEventRepository

logger = logging.getLogger(__name__)

# Заголовки подписи MeritHub
SIGNATURE_HEADERS = (
    "x-merithub-signature", "x-webhook-signature", "x-signature", "x-hub-signature-256",
)
TOKEN_HEADERS = ("x-merithub-token", "x-api-key", "authorization")
_HEADER_VALUE_LIMIT = 200


def verify_signature(body: bytes, headers: dict, secret: str) -> bool:
    """Проверяет подпись/секрет входящего вебхука MeritHub."""
    if not secret:
        return False
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    expected_hex = mac.hexdigest()
    expected_b64 = base64.b64encode(mac.digest()).decode()
    for h in SIGNATURE_HEADERS:
        v = headers.get(h)
        if not v:
            continue
        cand = v.split("=", 1)[-1].strip()
        if hmac.compare_digest(cand, expected_hex) or hmac.compare_digest(cand, expected_b64):
            return True
    for h in TOKEN_HEADERS:
        v = headers.get(h)
        if not v:
            continue
        tok = v[7:].strip() if v.lower().startswith("bearer ") else v.strip()
        if tok and hmac.compare_digest(tok, secret):
            return True
    return False


def _extract_type(payload: dict) -> str | None:
    t = (
        payload.get("requestType") or payload.get("event") or payload.get("type")
        or payload.get("event_type") or payload.get("action") or payload.get("name") or ""
    )
    return str(t).lower() or None


async def _dispatch_class_status(payload: dict) -> None:
    """requestType=classStatus → обновляем последний статус класса для live-check."""
    from src.db.repository import MeritHubClassStatusRepository
    from src.events.bus import bus
    from src.events.types import Event, EventTypes
    class_id = str(payload.get("classId") or "")
    status = str(payload.get("status") or "")
    if not class_id or not status:
        return
    event_time = str(payload.get("startTime") or payload.get("eventTime") or "") or None
    await MeritHubClassStatusRepository().upsert(class_id, status, payload=payload, event_time=event_time)
    evt = {"lv": EventTypes.LESSON_STARTED, "cp": EventTypes.LESSON_COMPLETED}.get(status)
    if evt:
        await bus.publish(Event(evt, {
            "class_id": class_id, "status": status,
            "sub_class_id": str(payload.get("subClassId") or "") or None,
            "event_time": event_time,
            "source": "merithub_classstatus_webhook",
        }))


async def _dispatch_attendance(payload: dict) -> None:
    """requestType=attendance → автоматически помечаем неявки."""
    from src.integrations.merithub_client import MeritHubClient
    from src.db.repository import MeritHubEnrollmentRepository
    from src.bot.pilot import trigger_absence

    class_id = str(payload.get("classId") or "")
    if not class_id:
        return
    enrolled = await MeritHubEnrollmentRepository().list_by_class(class_id)
    if not enrolled:
        logger.info("Attendance class=%s: нет зачислений — только захват", class_id)
        return
    attended = MeritHubClient.attended_user_ids(payload)
    fired = 0
    for e in enrolled:
        if (e.get("role") or "student") in ("tutor", "teacher", "C", "host"):
            continue
        mh_id = e.get("merithub_user_id")
        if mh_id and mh_id in attended:
            continue
        if not e.get("parent_telegram_id"):
            logger.info("Attendance: пропуск %s (нет TG родителя)", e.get("client_user_id"))
            continue
        inc_id, _wid = await trigger_absence(
            lesson_ref=class_id,
            student_id=e.get("client_user_id") or mh_id or "?",
            student_name=e.get("student_name") or "Ученик",
            parent_telegram_id=e["parent_telegram_id"],
            tutor_id="merithub",
            source="merithub_attendance_webhook",
        )
        if inc_id:
            fired += 1
    logger.info("Attendance class=%s: авто-неявок=%d (зачислено=%d присутств.=%d)",
                class_id, fired, len(enrolled), len(attended))


async def _dispatch_whatsapp(payload: dict) -> None:
    """Обрабатывает входящие события WhatsApp Meta Cloud API."""
    from src.events.bus import bus
    from src.events.types import Event, EventTypes
    from src.db.repository import MeritHubContactRepository, UserRepository
    from src.integrations.whatsapp_client import normalize_phone

    entries = payload.get("entry", [])
    for entry in entries:
        changes = entry.get("changes", [])
        for change in changes:
            value = change.get("value", {})
            messages = value.get("messages", [])
            for msg in messages:
                from_phone = normalize_phone(msg.get("from"))
                msg_type = msg.get("type")

                # Ищем пользователя/контакт по номеру телефона
                u_repo = UserRepository()
                c_repo = MeritHubContactRepository()
                crows = await c_repo._fetchall(
                    "SELECT * FROM merithub_contacts WHERE phone LIKE ?",
                    (f"%{from_phone[-10:]}%",)
                )
                urows = await u_repo._fetchall(
                    "SELECT * FROM users WHERE phone LIKE ?",
                    (f"%{from_phone[-10:]}%",)
                )
                tg_id = crows[0].get("telegram_id") if crows else (
                    urows[0].get("telegram_id") if urows else None
                )

                # 1. Интерактивная кнопка (quick-reply)
                if msg_type == "interactive":
                    interactive = msg.get("interactive", {})
                    button_reply = interactive.get("button_reply", {})
                    cb_data = button_reply.get("id") or ""
                    logger.info("WhatsApp button click from %s: %s", from_phone, cb_data)

                    # Сценарий ответа на неявку
                    if cb_data.startswith("resolve:"):
                        from src.workflows.absence import AbsenceWorkflow
                        parts = cb_data.split(":")
                        if len(parts) >= 4:
                            try:
                                inc_id = int(parts[1])
                                act = parts[3]
                                res_map = {
                                    "ok": "parent_ok",
                                    "no": "parent_not_coming",
                                    "late": "parent_late",
                                }
                                wf = AbsenceWorkflow()
                                await wf.resolve_absence(
                                    inc_id, f"wa:{from_phone}",
                                    resolution=res_map.get(act, "parent_confirmed")
                                )
                            except Exception as e:
                                logger.error("WhatsApp resolve error: %s", e)

                    # Сценарий переноса/отмены
                    elif cb_data.startswith("resched_req:"):
                        parts = cb_data.split(":")
                        cid = parts[1] if len(parts) > 1 else ""
                        occ = parts[2] if len(parts) > 2 else None
                        await bus.publish(Event(EventTypes.LESSON_RESCHEDULE_REQUESTED, {
                            "lesson_id": cid,
                            "occurrence_date": occ,
                            "reported_by": f"wa:{from_phone}",
                        }))
                    elif cb_data.startswith("cancel_yes"):
                        parts = cb_data.split(":")
                        cid = parts[1] if len(parts) > 1 else ""
                        occ = parts[2] if len(parts) > 2 else None
                        is_paid = "paid" in cb_data
                        await bus.publish(Event(EventTypes.LESSON_CANCELLED, {
                            "lesson_id": cid,
                            "occurrence_date": occ,
                            "is_paid": is_paid,
                            "reason": "Отмена родителем через WhatsApp",
                            "reported_by": f"wa:{from_phone}",
                        }))

                # 2. Текстовое сообщение
                elif msg_type == "text":
                    text_body = msg.get("text", {}).get("body", "")
                    logger.info("WhatsApp text incoming from %s: %s", from_phone, text_body[:60])
                    await bus.publish(Event(EventTypes.MESSAGE_INCOMING, {
                        "text": text_body,
                        "phone": from_phone,
                        "telegram_id": tg_id,
                        "chat_id": f"wa:{from_phone}",
                        "source": "whatsapp",
                    }))


def _compact_headers(headers) -> dict:
    out = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl == "cookie":
            continue
        out[kl] = v[:_HEADER_VALUE_LIMIT]
    return out


async def _receive(request: Request):
    """Приёмник MeritHub вебхуков."""
    body = await request.body()
    headers = _compact_headers(request.headers)
    secret = settings.merithub_webhook_secret
    repo = WebhookEventRepository()

    etype = None
    payload: dict = {}
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                payload = parsed
                etype = _extract_type(payload)
        except Exception:
            etype = None

    sig_present = any(headers.get(h) for h in (SIGNATURE_HEADERS + TOKEN_HEADERS))
    if secret and sig_present:
        accepted = verify_signature(body, headers, secret)
    else:
        accepted = True

    await repo.save(etype, int(accepted), headers, body)
    logger.info("MeritHub webhook captured: type=%s accepted=%s bytes=%d",
                etype, accepted, len(body))

    if not accepted:
        return JSONResponse({"status": "unauthorized"}, status_code=401)

    if etype == "classstatus":
        try:
            await _dispatch_class_status(payload)
        except Exception as e:
            logger.error("classStatus dispatch failed: %s", e, exc_info=True)
    elif etype == "attendance":
        try:
            await _dispatch_attendance(payload)
        except Exception as e:
            logger.error("attendance dispatch failed: %s", e, exc_info=True)
    return {"status": "ok", "captured": etype, "accepted": accepted}


async def _verify_whatsapp(request: Request):
    """Верификация вебхука Meta WhatsApp Cloud API (GET)."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == settings.whatsapp_webhook_verify_token:
        logger.info("WhatsApp webhook verified successfully")
        return PlainTextResponse(challenge or "")
    logger.warning("WhatsApp webhook verification failed: token mismatch")
    return JSONResponse({"status": "forbidden"}, status_code=403)


async def _receive_whatsapp(request: Request):
    """Приёмник вебхуков Meta WhatsApp Cloud API (POST)."""
    body = await request.body()
    headers = _compact_headers(request.headers)
    repo = WebhookEventRepository()

    payload = {}
    if body:
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}

    await repo.save("whatsapp", 1, headers, body)
    logger.info("WhatsApp webhook captured: %d bytes", len(body))

    try:
        await _dispatch_whatsapp(payload)
    except Exception as e:
        logger.error("WhatsApp dispatch error: %s", e, exc_info=True)

    return {"status": "ok"}


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        from src.db.migrations import init_db
        await init_db()
        logger.info("Webhook receivers ready: MeritHub (%s), WhatsApp (%s)",
                    settings.merithub_webhook_path, settings.whatsapp_webhook_path)
        yield

    app = FastAPI(title="ALBION Operations Platform", lifespan=lifespan)

    @app.get("/health")
    async def health():
        from src.db.repository import UserRepository
        db_ok = True
        try:
            await UserRepository()._fetchall("SELECT 1")
        except Exception as e:
            db_ok = False
        return {
            "status": "ok" if db_ok else "unhealthy",
            "service": "albion-backend",
            "database": "connected" if db_ok else "error",
            "version": "1.0-prod",
        }

    @app.get("/metrics")
    async def metrics():
        from src.db.repository import (
            MeritHubClassRepository, MeritHubContactRepository,
            MeritHubStudentRepository, IncidentRepository,
            ScheduledActionRepository, DeadLetterQueueRepository,
            NotificationRepository,
        )
        try:
            students = await MeritHubStudentRepository()._fetchall("SELECT client_user_id FROM merithub_students WHERE role='student'")
            tutors = await MeritHubContactRepository()._fetchall("SELECT client_user_id FROM merithub_contacts WHERE role='tutor'")
            classes = await MeritHubClassRepository().list_all()
            perma_classes = sum(1 for c in classes if c.get("class_type") == "perma")
            open_inc = await IncidentRepository()._fetchall("SELECT id FROM incidents WHERE status='pending'")
            pending_actions = await ScheduledActionRepository()._fetchall("SELECT id FROM scheduled_actions WHERE status='pending'")
            dlq_items = await DeadLetterQueueRepository()._fetchall("SELECT id FROM dead_letter_queue")
            notifs = await NotificationRepository()._fetchall("SELECT channel, count(*) as cnt FROM notifications GROUP BY channel")
            channels = {row["channel"]: row["cnt"] for row in notifs}
            return {
                "tutors_total": len(tutors),
                "students_total": len(students),
                "classes_total": len(classes),
                "classes_perma": perma_classes,
                "classes_onetime": len(classes) - perma_classes,
                "incidents_open": len(open_inc),
                "scheduled_pending": len(pending_actions),
                "dlq_count": len(dlq_items),
                "notifications_by_channel": channels,
            }
        except Exception as e:
            return JSONResponse({"status": "error", "error": str(e)}, status_code=500)

    # MeritHub Webhook
    app.add_api_route(settings.merithub_webhook_path, _receive, methods=["POST"])

    # WhatsApp Webhook
    app.add_api_route(settings.whatsapp_webhook_path, _verify_whatsapp, methods=["GET"])
    app.add_api_route(settings.whatsapp_webhook_path, _receive_whatsapp, methods=["POST"])

    return app


app = create_app()
