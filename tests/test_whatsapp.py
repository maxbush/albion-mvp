"""PR2 — WhatsApp: sender, webhook-приёмник, inbound-маршрутизация."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from src.api.whatsapp import parse_messages, verify_signature
from src.channels.base import ChannelButton
from src.channels.inbound import canonical_recipient, enqueue_inbound
from src.channels.router import parse_recipient, resolve
from src.channels.whatsapp import MockWhatsAppClient, WhatsAppSender
from src.channels import router as _router
from src.events.types import Event, EventTypes
from src.db.repository import (
    IncidentRepository,
    ScheduledActionRepository,
    UserChannelRepository,
    UserRepository,
    WorkflowRepository,
)
from src.workflows.engine import engine
from src.workflows.inbound import InboundHandlers


async def _init_tmp_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    return "albion.db"


@pytest.fixture(autouse=True)
def clean_registry():
    saved = dict(_router._senders)
    _router._senders.clear()
    yield
    _router._senders.clear()
    _router._senders.update(saved)


class FakeSender:
    name = "whatsapp"

    def __init__(self):
        self.sent = []

    async def send(self, address, text, buttons=None):
        self.sent.append({"address": address, "text": text, "buttons": buttons})


# ── WhatsAppSender (маршрутизация кнопок по типам) ────────────────────

@pytest.mark.asyncio
async def test_sender_plain_text():
    client = MockWhatsAppClient()
    res = await WhatsAppSender(client).send("+79990001122", "Привет")
    assert res.ok and res.message_id == "wamid.mock-1"
    assert client.sent[0]["kind"] == "text" and client.sent[0]["text"] == "Привет"


@pytest.mark.asyncio
async def test_sender_reply_buttons():
    client = MockWhatsAppClient()
    buttons = [ChannelButton(label="✅ Ок", callback_id="resolve:1:abc"),
               ChannelButton(label="⏰ Позже", callback_id="resolve:1:abc:late")]
    await WhatsAppSender(client).send("+7", "Текст", buttons)
    assert client.sent[0]["kind"] == "reply_buttons"


@pytest.mark.asyncio
async def test_sender_many_buttons_go_list():
    client = MockWhatsAppClient()
    buttons = [ChannelButton(label=f"Опция {i}", callback_id=f"pick:{i}") for i in range(5)]
    await WhatsAppSender(client).send("+7", "Выберите", buttons)
    assert client.sent[0]["kind"] == "list"


@pytest.mark.asyncio
async def test_sender_url_buttons_become_text():
    """URL-кнопок в WA нет — ссылка уходит строкой в текст."""
    client = MockWhatsAppClient()
    buttons = [ChannelButton(label="Написать родителю", url="tg://user?id=5")]
    await WhatsAppSender(client).send("+7", "Алерт", buttons)
    assert client.sent[0]["kind"] == "text"
    assert "tg://user?id=5" in client.sent[0]["text"]


# ── parse_recipient / canonical_recipient ────────────────────────────

def test_parse_recipient():
    assert parse_recipient("wa:+7999") == ("whatsapp", "+7999")
    assert parse_recipient("email:a@b.c") == ("email", "a@b.c")
    assert parse_recipient("555") == ("telegram", "555")
    assert parse_recipient(None) is None


@pytest.mark.asyncio
async def test_resolve_wa_ref(db):
    """'wa:'-ref → whatsapp, если sender есть; без sender — None (не TG)."""
    assert await resolve(telegram_id="wa:+7999", db_path=db) is None
    _router.register_sender(FakeSender())
    assert await resolve(telegram_id="wa:+7999", db_path=db) == ("whatsapp", "+7999")


@pytest.mark.asyncio
async def test_canonical_unknown(db):
    assert await canonical_recipient("whatsapp", "+7999", db_path=db) == "wa:+7999"


@pytest.mark.asyncio
async def test_canonical_preferred(db):
    """Известный пользователь с preferred whatsapp → 'wa:…' ref."""
    repo = UserChannelRepository(db)
    uid = await UserRepository(db).create("555", "parent", "Мама")
    await repo.set(uid, "whatsapp", "+79990001122")
    await repo.set(uid, "telegram", "555", preferred=False)
    assert await canonical_recipient("whatsapp", "+79990001122", db_path=db) == "wa:+79990001122"


@pytest.mark.asyncio
async def test_canonical_telegram_only_user(db):
    repo = UserChannelRepository(db)
    uid = await UserRepository(db).create("555", "parent", "Мама")
    await repo.set(uid, "whatsapp", "+79990001122", preferred=False)
    await repo.set(uid, "telegram", "555")
    # preferred — telegram → канонический ref = tg id
    assert await canonical_recipient("whatsapp", "+79990001122", db_path=db) == "555"


# ── Webhook: verify + signature + parse ──────────────────────────────

def test_verify_signature():
    body = b'{"x":1}'
    secret = "s3cret"
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, secret)
    assert not verify_signature(body, "sha256=bad", secret)
    assert not verify_signature(body, None, secret)
    assert verify_signature(body, None, None)  # без секрета — открытый режим


_WA_PAYLOAD = {
    "entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "79990001122", "profile": {"name": "Мама"}}],
        "messages": [{
            "id": "wamid.X1", "from": "79990001122", "type": "text",
            "text": {"body": "Здравствуйте, ищу репетитора"},
        }],
    }}]}],
}


def test_parse_messages_text():
    items = parse_messages(_WA_PAYLOAD)
    assert items == [{"phone": "+79990001122", "wamid": "wamid.X1",
                      "name": "Мама", "kind": "text",
                      "text": "Здравствуйте, ищу репетитора"}]


def test_parse_messages_buttons():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "w1", "from": "7999", "type": "interactive",
         "interactive": {"type": "button_reply",
                         "button_reply": {"id": "resolve:1:abc", "title": "Ок"}}},
        {"id": "w2", "from": "7999", "type": "interactive",
         "interactive": {"type": "list_reply",
                         "list_reply": {"id": "pick:3", "title": "Опция"}}},
        {"id": "w3", "from": "7999", "type": "button",
         "button": {"payload": "resolve:2:def"}},
    ]}}]}]}
    items = parse_messages(payload)
    assert [i["callback_id"] for i in items] == ["resolve:1:abc", "pick:3", "resolve:2:def"]


@pytest.mark.asyncio
async def test_webhook_verify_and_receive(tmp_path, monkeypatch):
    db = await _init_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr("src.config.settings.whatsapp_verify_token", "vtok")
    monkeypatch.setattr("src.config.settings.whatsapp_app_secret", "s3cret")
    from src.api.webhook import create_app
    client = TestClient(create_app())

    # GET verify-challenge
    ok = client.get("/whatsapp/webhook",
                    params={"hub.mode": "subscribe", "hub.verify_token": "vtok",
                            "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/whatsapp/webhook",
                     params={"hub.mode": "subscribe", "hub.verify_token": "wrong",
                             "hub.challenge": "42"})
    assert bad.status_code == 403

    # POST без подписи при заданном секрете → 401
    body = json.dumps(_WA_PAYLOAD).encode()
    assert client.post("/whatsapp/webhook", content=body).status_code == 401

    # POST с правильной подписью → 200 + scheduled_action inbound_text
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    res = client.post("/whatsapp/webhook", content=body,
                      headers={"x-hub-signature-256": sig})
    assert res.status_code == 200 and res.json()["enqueued"] == 1
    rows = await ScheduledActionRepository(db)._fetchall(
        "SELECT * FROM scheduled_actions WHERE action='inbound_text'")
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["channel"] == "whatsapp" and payload["address"] == "+79990001122"

    # Ретрай Meta с тем же wamid — не плодит дубли
    res = client.post("/whatsapp/webhook", content=body,
                      headers={"x-hub-signature-256": sig})
    assert res.json()["enqueued"] == 0


# ── Inbound tick → bus ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_inbound_text_publishes_message_incoming(db):
    from src.events.bus import bus

    got = []
    async def capture(e):
        got.append(e)
    bus.subscribe(EventTypes.MESSAGE_INCOMING, capture)

    await enqueue_inbound("inbound_text", channel="whatsapp",
                          address="+7999", payload={"text": "Привет"},
                          db_path=db)
    h = InboundHandlers(db)
    await h.handle_scheduler_tick(Event(EventTypes.SCHEDULER_TICK, {
        "action": "inbound_text", "workflow_id": None,
        "data": {"channel": "whatsapp", "address": "+7999", "text": "Привет"},
    }))
    assert got and got[0].data["text"] == "Привет"
    assert got[0].data["telegram_id"] == "wa:+7999"
    assert got[0].data["channel"] == "whatsapp"


@pytest.mark.asyncio
async def test_inbound_callback_resolve(db):
    """WA-кнопка resolve → инцидент закрыт + ack ушёл обратно в WA."""
    sender = FakeSender()
    _router.register_sender(sender)

    await UserRepository(db).create("coord_1", "coordinator", "К")
    inc_id = await IncidentRepository(db).create(type="absence", status="pending")
    await engine.start_workflow("absence_notification", {
        "incident_id": inc_id, "student_name": "Миша",
        "parent_telegram_id": "wa:+7999", "parent_callback_nonce": "nn1",
        "lesson_ref": "C1"})

    h = InboundHandlers(db)
    await h.handle_scheduler_tick(Event(EventTypes.SCHEDULER_TICK,
        {"action": "inbound_callback", "workflow_id": None,
         "data": {"channel": "whatsapp", "address": "+7999",
                  "callback_id": f"resolve:{inc_id}:nn1:ok"}},
    ))

    inc = await IncidentRepository(db).get(inc_id)
    assert inc["status"] == "resolved"
    assert sender.sent and "вопрос закрыт" in sender.sent[0]["text"]
