"""PR8 — Twilio: sender с нумерованными кнопками, webhook, подпись."""

import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from src.api.twilio import parse_form
from src.channels.base import ChannelButton
from src.channels.twilio import (
    MockTwilioClient,
    TwilioSender,
    load_button_map,
    save_button_map,
    verify_twilio_signature,
)
from src.db.repository import ScheduledActionRepository


def _twilio_sig(url: str, params: dict, token: str) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(
        hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


# ── TwilioSender ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sender_plain_text():
    client = MockTwilioClient()
    res = await TwilioSender(client).send("+79990001122", "Привет")
    assert res.ok and res.message_id == "SM.mock-1"
    assert client.sent[0]["kind"] == "text"
    assert client.sent[0]["text"] == "Привет"


@pytest.mark.asyncio
async def test_sender_buttons_numbered_and_mapped(db):
    """Callback-кнопки → нумерованный текст + маппинг в system_settings."""
    client = MockTwilioClient()
    buttons = [ChannelButton(label="✅ Подтвердить", callback_id="resolve:1:abc"),
               ChannelButton(label="❌ Отклонить", callback_id="resolve:1:abc:no")]
    await TwilioSender(client, db_path=db).send("+7", "Вопрос", buttons)
    text = client.sent[0]["text"]
    assert "1. ✅ Подтвердить" in text and "2. ❌ Отклонить" in text
    assert await load_button_map("+7", db_path=db) == [
        "resolve:1:abc", "resolve:1:abc:no"]


@pytest.mark.asyncio
async def test_sender_url_buttons_become_text(db):
    client = MockTwilioClient()
    buttons = [ChannelButton(label="Написать", url="tg://user?id=5")]
    await TwilioSender(client, db_path=db).send("+7", "Алерт", buttons)
    assert "tg://user?id=5" in client.sent[0]["text"]
    assert await load_button_map("+7", db_path=db) == []


# ── parse_form / подпись ─────────────────────────────────────────────

def test_parse_form_text():
    item = parse_form({"From": "whatsapp:+79990001122", "Body": "Здравствуйте",
                       "MessageSid": "SM1", "ProfileName": "Мама"})
    assert item == {"phone": "+79990001122", "sid": "SM1", "name": "Мама",
                    "kind": "text", "text": "Здравствуйте"}


def test_parse_form_button_payload():
    item = parse_form({"From": "whatsapp:+7", "ButtonPayload": "resolve:1:x",
                       "MessageSid": "SM2"})
    assert item["kind"] == "callback" and item["callback_id"] == "resolve:1:x"


def test_parse_form_no_from():
    assert parse_form({"Body": "x"}) is None


def test_verify_signature():
    url = "https://example.com/twilio/whatsapp"
    params = {"From": "whatsapp:+7", "Body": "1", "MessageSid": "SM9"}
    sig = _twilio_sig(url, params, "tok")
    assert verify_twilio_signature(url, params, sig, "tok")
    assert not verify_twilio_signature(url, params, "bad", "tok")
    assert not verify_twilio_signature(url, params, None, "tok")
    assert verify_twilio_signature(url, params, None, None)  # открытый режим


# ── Webhook end-to-end ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_text_and_dedup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.config.settings.whatsapp_provider", "twilio")
    monkeypatch.setattr("src.config.settings.twilio_auth_token", "tok")
    from src.db.migrations import init_db
    await init_db("albion.db")
    from src.api.webhook import create_app
    client = TestClient(create_app())

    form = {"From": "whatsapp:+79990001122", "Body": "Хочу перенести",
            "MessageSid": "SM100", "ProfileName": "Мама"}
    url = "http://testserver/twilio/whatsapp"
    sig = _twilio_sig(url, form, "tok")

    # без подписи → 403
    assert client.post("/twilio/whatsapp", data=form).status_code == 403

    res = client.post("/twilio/whatsapp", data=form,
                      headers={"X-Twilio-Signature": sig})
    assert res.status_code == 200 and "<Response/>" in res.text
    rows = await ScheduledActionRepository("albion.db")._fetchall(
        "SELECT * FROM scheduled_actions WHERE action='inbound_text'")
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["channel"] == "whatsapp"
    assert payload["address"] == "+79990001122"
    assert payload["text"] == "Хочу перенести"

    # ретрай Twilio с тем же MessageSid → дубль не ставится
    res = client.post("/twilio/whatsapp", data=form,
                      headers={"X-Twilio-Signature": sig})
    assert res.status_code == 200
    rows = await ScheduledActionRepository("albion.db")._fetchall(
        "SELECT * FROM scheduled_actions WHERE action='inbound_text'")
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_webhook_digit_unfolds_to_callback(tmp_path, monkeypatch):
    """Ответ '2' → inbound_callback по сохранённому button map."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.config.settings.whatsapp_provider", "twilio")
    monkeypatch.setattr("src.config.settings.twilio_auth_token", "tok")
    from src.db.migrations import init_db
    await init_db("albion.db")
    await save_button_map("+7999", ["cb:yes", "cb:no"], db_path="albion.db")

    from src.api.webhook import create_app
    client = TestClient(create_app())
    form = {"From": "whatsapp:+7999", "Body": "2", "MessageSid": "SM101"}
    sig = _twilio_sig("http://testserver/twilio/whatsapp", form, "tok")
    res = client.post("/twilio/whatsapp", data=form,
                      headers={"X-Twilio-Signature": sig})
    assert res.status_code == 200
    rows = await ScheduledActionRepository("albion.db")._fetchall(
        "SELECT * FROM scheduled_actions WHERE action='inbound_callback'")
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["callback_id"] == "cb:no"


@pytest.mark.asyncio
async def test_webhook_digit_without_map_is_text(tmp_path, monkeypatch):
    """'5' без маппинга остаётс�� обычным текстом (родитель написал цифру)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.config.settings.whatsapp_provider", "twilio")
    monkeypatch.setattr("src.config.settings.twilio_auth_token", None)
    from src.db.migrations import init_db
    await init_db("albion.db")
    from src.api.webhook import create_app
    client = TestClient(create_app())
    form = {"From": "whatsapp:+7999", "Body": "5", "MessageSid": "SM102"}
    res = client.post("/twilio/whatsapp", data=form)
    assert res.status_code == 200
    rows = await ScheduledActionRepository("albion.db")._fetchall(
        "SELECT * FROM scheduled_actions WHERE action='inbound_text'")
    assert json.loads(rows[0]["payload"])["text"] == "5"


def test_factory_picks_twilio(monkeypatch):
    monkeypatch.setattr("src.config.settings.whatsapp_provider", "twilio")
    monkeypatch.setattr("src.config.settings.twilio_account_sid", "AC1")
    monkeypatch.setattr("src.config.settings.twilio_auth_token", "tok")
    from src.channels.factory import get_whatsapp_sender
    from src.channels.twilio import TwilioClient, TwilioSender
    sender = get_whatsapp_sender()
    assert isinstance(sender, TwilioSender)
    assert isinstance(sender._client, TwilioClient)


def test_factory_meta_default(monkeypatch):
    monkeypatch.setattr("src.config.settings.whatsapp_provider", "meta")
    monkeypatch.setattr("src.config.settings.whatsapp_token", "t")
    monkeypatch.setattr("src.config.settings.whatsapp_phone_number_id", "p")
    from src.channels.factory import get_whatsapp_sender
    from src.channels.whatsapp import WhatsAppSender
    assert isinstance(get_whatsapp_sender(), WhatsAppSender)
