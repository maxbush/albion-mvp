"""Тесты приёмника вебхуков MeritHub (src/api/webhook.py) и команды /mh_events."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.integrations.merithub_client import MeritHubClient  # noqa: ensure import works (reuse)


# ── фейки telegram для /mh_events ──────────────────────────────────────
class FakeUser:
    def __init__(self, id, username=None, full_name="T"):
        self.id = id
        self.username = username
        self.full_name = full_name


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, user):
        self.effective_user = user
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []


def _hmac_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# =====================================================================
# verify_signature (юнит)
# =====================================================================

def test_verify_signature_hmac_hex():
    from src.api.webhook import verify_signature
    body = b'{"event":"Attendance"}'
    sec = "s3cret"
    assert verify_signature(body, {"x-merithub-signature": _hmac_hex(sec, body)}, sec) is True


def test_verify_signature_with_sha256_prefix():
    from src.api.webhook import verify_signature
    body = b"x"
    sec = "k"
    assert verify_signature(body, {"x-signature": "sha256=" + _hmac_hex(sec, body)}, sec) is True


def test_verify_signature_token_style():
    from src.api.webhook import verify_signature
    assert verify_signature(b"{}", {"authorization": "Bearer topsecret"}, "topsecret") is True
    assert verify_signature(b"{}", {"x-api-key": "topsecret"}, "topsecret") is True


def test_verify_signature_rejects_wrong_and_empty():
    from src.api.webhook import verify_signature
    assert verify_signature(b"{}", {"x-merithub-signature": "deadbeef"}, "real") is False
    assert verify_signature(b"{}", {}, "real") is False
    assert verify_signature(b"{}", {"x-merithub-signature": _hmac_hex("a", b"{}")}, "") is False


# =====================================================================
# HTTP-эндпоинт (TestClient запускает lifespan → init_db в tmp)
# =====================================================================

@pytest.fixture
def wh_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.api.webhook import app
    with TestClient(app) as c:
        yield c


def test_health(wh_client):
    r = wh_client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_no_secret_returns_503(wh_client, monkeypatch):
    monkeypatch.setattr(settings, "merithub_webhook_secret", None)
    r = wh_client.post("/merithub/webhook", content=b'{"event":"Attendance"}')
    assert r.status_code == 503


def test_invalid_signature_returns_401_and_stores(wh_client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "merithub_webhook_secret", "real")
    r = wh_client.post(
        "/merithub/webhook",
        content=b'{"event":"Attendance"}',
        headers={"x-merithub-signature": "wrong", "content-type": "application/json"},
    )
    assert r.status_code == 401
    import sqlite3
    con = sqlite3.connect("albion.db")
    rows = con.execute("SELECT signature_ok FROM webhook_events").fetchall()
    con.close()
    assert len(rows) == 1 and rows[0][0] == 0


def test_valid_signature_acks_and_captures_type(wh_client, monkeypatch, tmp_path):
    sec = "real"
    monkeypatch.setattr(settings, "merithub_webhook_secret", sec)
    body = json.dumps({"event": "Attendance", "learner": 7}).encode()
    r = wh_client.post(
        "/merithub/webhook",
        content=body,
        headers={"x-merithub-signature": _hmac_hex(sec, body), "content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["captured"] == "attendance"

    import sqlite3
    con = sqlite3.connect("albion.db")
    rows = con.execute("SELECT event_type, signature_ok FROM webhook_events ORDER BY id DESC").fetchall()
    con.close()
    assert rows and rows[0][0] == "attendance" and rows[0][1] == 1


# =====================================================================
# Команда /mh_events
# =====================================================================

@pytest.mark.asyncio
async def test_cmd_mh_events_lists_captured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.db.repository import WebhookEventRepository, UserRepository
    await UserRepository("albion.db").create("100", "coordinator", "Админ")
    await WebhookEventRepository("albion.db").save(
        "attendance", 1, {"x-merithub-signature": "abc"}, b'{"event":"Attendance","id":42}')

    from src.bot.pilot import cmd_mh_events
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_mh_events(upd, FakeContext([]))
    assert any("attendance" in r for r in upd.message.replies)
    assert any("42" in r for r in upd.message.replies)


@pytest.mark.asyncio
async def test_cmd_mh_events_empty_hint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    monkeypatch.setattr(settings, "albion_admin_telegram_ids", "100")
    from src.bot.pilot import cmd_mh_events
    upd = FakeUpdate(FakeUser(100, "admin"))
    await cmd_mh_events(upd, FakeContext([]))
    assert any("нет захваченных" in r for r in upd.message.replies)
