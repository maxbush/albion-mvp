"""Приёмник вебхуков MeritHub (захват событий для демо-пилота).

MeritHub работает по push-модели: при событиях (Attendance, Session Status,
Users Added, Invoice, ...) он POSTит на наш Webhook URL. Этот эндпоинт:
  1) проверяет подпись/секрет (принцип «Безопасный запуск»),
  2) СОХРАНЯЕТ сырой payload + заголовки в таблицу webhook_events,
  3) отвечает 200, чтобы MeritHub не ретраил событие.

На этом этапе мы НЕ угадываем схему JSON — мы захватываем реальные события,
а точные авто-обработчики (Attendance → автоматическое уведомление родителя /
эскалация координатору) дописываем по захваченным примерам (команда /mh_events).

Запуск (отдельный процесс; та же SQLite через WAL):
    uvicorn src.api.webhook:app --host 0.0.0.0 --port 8000
Публичный URL (туннель ngrok/cloudflared или VPS) + путь указываются в
MeritHub → Webhook Url. localhost использовать нельзя (требование MeritHub).
"""

import base64
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.config import settings
from src.db.repository import WebhookEventRepository

logger = logging.getLogger(__name__)

# Кандидаты на заголовок с подписью / токеном (схема уточняется по докам MeritHub).
SIGNATURE_HEADERS = (
    "x-merithub-signature", "x-webhook-signature", "x-signature", "x-hub-signature-256",
)
TOKEN_HEADERS = ("x-merithub-token", "x-api-key", "authorization")
_HEADER_VALUE_LIMIT = 200


def verify_signature(body: bytes, headers: dict, secret: str) -> bool:
    """Проверяет подпись/секрет входящего вебхука. Толерантна к схеме MeritHub.

    Принимает: HMAC-SHA256(secret, body) в hex или base64 (с префиксом
    'sha256=' и без) в одном из SIGNATURE_HEADERS, либо точное совпадение
    секрета в TOKEN_HEADERS (в т.ч. 'Bearer ...').
    """
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


async def _dispatch_attendance(payload: dict) -> None:
    """requestType=attendance → автоматически помечаем неявки.

    Зачисленные в класс (merithub_enrollments) минус присутствовавшие в webhook
    (totalTime>0) = отсутствующие. По каждому — триггерим сценарий неявки,
    уведомляя реального родителя (TG берётся из маппинга)."""
    from src.integrations.merithub_client import MeritHubClient
    from src.db.repository import MeritHubEnrollmentRepository
    from src.bot.pilot import trigger_absence

    class_id = str(payload.get("classId") or "")
    if not class_id:
        return
    enrolled = await MeritHubEnrollmentRepository().list_by_class(class_id)
    if not enrolled:
        logger.info("Attendance class=%s: нет зачислений — только захват (выполните /mh_enroll)", class_id)
        return
    attended = MeritHubClient.attended_user_ids(payload)
    fired = 0
    for e in enrolled:
        if (e.get("role") or "student") in ("tutor", "teacher", "C", "host"):
            continue
        mh_id = e.get("merithub_user_id")
        if mh_id and mh_id in attended:
            continue  # присутствовал
        if not e.get("parent_telegram_id"):
            logger.info("Attendance: пропуск %s (нет TG родителя)", e.get("client_user_id"))
            continue
        await trigger_absence(
            lesson_ref=class_id,
            student_id=e.get("client_user_id") or mh_id or "?",
            student_name=e.get("student_name") or "Ученик",
            parent_telegram_id=e["parent_telegram_id"],
            tutor_id="merithub",
            source="merithub_attendance_webhook",
        )
        fired += 1
    logger.info("Attendance class=%s: авто-неявок=%d (зачислено=%d присутств.=%d)",
                class_id, fired, len(enrolled), len(attended))


def _compact_headers(headers) -> dict:
    out = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl == "cookie":
            continue
        out[kl] = v[:_HEADER_VALUE_LIMIT]
    return out


async def _receive(request: Request):
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

    # MeritHub в доке НЕ описывает подпись вебхуков → пинги без подписи принимаем.
    # Секрет — опциональное усиление: проверяем ТОЛЬКО когда он задан И в запросе
    # есть распознанный заголовок подписи/токена. Отклоняем лишь явный mismatch.
    sig_present = any(headers.get(h) for h in (SIGNATURE_HEADERS + TOKEN_HEADERS))
    if secret and sig_present:
        accepted = verify_signature(body, headers, secret)
    else:
        accepted = True  # без секрета или MeritHub шлёт без подписи — принимаем

    await repo.save(etype, int(accepted), headers, body)
    logger.info("MeritHub webhook captured: type=%s accepted=%s sig_present=%s bytes=%d",
                etype, accepted, sig_present, len(body))

    if not accepted:
        return JSONResponse({"status": "unauthorized"}, status_code=401)

    # Реальные авто-обработчики по requestType (схема из док MeritHub).
    if etype == "attendance":
        try:
            await _dispatch_attendance(payload)
        except Exception as e:
            logger.error("attendance dispatch failed: %s", e, exc_info=True)
    return {"status": "ok", "captured": etype, "accepted": accepted}


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        from src.db.migrations import init_db
        await init_db()  # гарантирует наличие таблицы webhook_events
        logger.info("MeritHub webhook receiver ready on path=%s", settings.merithub_webhook_path)
        yield

    app = FastAPI(title="ALBION MeritHub webhook receiver", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "albion-merithub-webhook"}

    app.add_api_route(settings.merithub_webhook_path, _receive, methods=["POST"])
    return app


app = create_app()
