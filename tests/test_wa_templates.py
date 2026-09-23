"""PR10 — шаблонный режим исходящих WA-уведомлений: 24h-окно + approved
template с нумерованными опциями в body (общий button-map)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.channels.base import ChannelButton
from src.channels.inbound import enqueue_inbound
from src.channels.whatsapp import MockWhatsAppClient, WhatsAppSender
from src.db.repository import SystemSettingsRepository, ScheduledActionRepository


async def _set_window(db, phone: str, when: datetime):
    await SystemSettingsRepository(db).set(
        f"wa_window:{phone}", when.isoformat())


# ── Отметка окна на входящем ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_inbound_marks_window(db):
    await enqueue_inbound("inbound_text", channel="whatsapp",
                          address="+79990001122",
                          payload={"text": "привет"}, db_path=db)
    raw = await SystemSettingsRepository(db).get("wa_window:+79990001122")
    assert raw and datetime.fromisoformat(raw) > (
        datetime.now(timezone.utc) - timedelta(minutes=1))


@pytest.mark.asyncio
async def test_inbound_other_channel_no_window(db):
    await enqueue_inbound("inbound_text", channel="email",
                          address="a@b.c", payload={"text": "x"}, db_path=db)
    assert await SystemSettingsRepository(db).get("wa_window:a@b.c") is None


# ── Выбор session vs template ────────────────────────────────────────

@pytest.mark.asyncio
async def test_window_closed_goes_template(db, monkeypatch):
    monkeypatch.setattr("src.config.settings.whatsapp_notification_template",
                        "albion_notify")
    client = MockWhatsAppClient()
    await WhatsAppSender(client, db_path=db).send("+7999", "Напоминание")
    assert client.sent[0]["kind"] == "template"
    assert client.sent[0]["template"] == "albion_notify"


@pytest.mark.asyncio
async def test_window_open_goes_freeform(db, monkeypatch):
    monkeypatch.setattr("src.config.settings.whatsapp_notification_template",
                        "albion_notify")
    await _set_window(db, "+7999", datetime.now(timezone.utc))
    client = MockWhatsAppClient()
    await WhatsAppSender(client, db_path=db).send("+7999", "Ответ")
    assert client.sent[0]["kind"] == "text"


@pytest.mark.asyncio
async def test_expired_window_goes_template(db, monkeypatch):
    monkeypatch.setattr("src.config.settings.whatsapp_notification_template",
                        "albion_notify")
    await _set_window(db, "+7999",
                      datetime.now(timezone.utc) - timedelta(hours=25))
    client = MockWhatsAppClient()
    await WhatsAppSender(client, db_path=db).send("+7999", "Холодное")
    assert client.sent[0]["kind"] == "template"


@pytest.mark.asyncio
async def test_no_template_configured_always_freeform(db, monkeypatch):
    """Без настроенного шаблона — пробуем session (dev/mock поведение)."""
    monkeypatch.setattr("src.config.settings.whatsapp_notification_template",
                        None)
    client = MockWhatsAppClient()
    await WhatsAppSender(client, db_path=db).send("+7999", "Dev")
    assert client.sent[0]["kind"] == "text"


# ── Кнопки в шаблоне ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_template_buttons_numbered_and_mapped(db, monkeypatch):
    """Опции шаблона — нумерованные в body {{1}} + общий button-map
    (label'ы статичных quick_reply не используем — рассинхрон с payload)."""
    monkeypatch.setattr("src.config.settings.whatsapp_notification_template",
                        "albion_notify")
    from src.channels.inbound import load_button_map
    client = MockWhatsAppClient()
    buttons = [ChannelButton(label="Да", callback_id="resolve:1:abc"),
               ChannelButton(label="Нет", callback_id="resolve:1:abc:no")]
    await WhatsAppSender(client, db_path=db).send("+7999", "Был?", buttons)
    comps = client.sent[0]["components"]
    assert not [c for c in comps if c["type"] == "button"]
    body = comps[0]["parameters"][0]["text"]
    assert "1. Да" in body and "2. Нет" in body
    assert await load_button_map("+7999", db_path=db) == [
        "resolve:1:abc", "resolve:1:abc:no"]


@pytest.mark.asyncio
async def test_template_extra_buttons_numbered(db, monkeypatch):
    """>3 опций тоже работают: все уходят нумерованным списком в body."""
    monkeypatch.setattr("src.config.settings.whatsapp_notification_template",
                        "albion_notify")
    from src.channels.inbound import load_button_map
    client = MockWhatsAppClient()
    buttons = [ChannelButton(label=f"Опц{i}", callback_id=f"pick:{i}")
               for i in range(5)]
    await WhatsAppSender(client, db_path=db).send("+7999", "Выбор", buttons)
    body = client.sent[0]["components"][0]["parameters"][0]["text"]
    assert "4. Опц3" in body and "5. Опц4" in body
    assert await load_button_map("+7999", db_path=db) == [
        f"pick:{i}" for i in range(5)]


@pytest.mark.asyncio
async def test_template_body_param(db, monkeypatch):
    monkeypatch.setattr("src.config.settings.whatsapp_notification_template",
                        "albion_notify")
    client = MockWhatsAppClient()
    await WhatsAppSender(client, db_path=db).send("+7999", "Текст уведомления")
    body = client.sent[0]["components"][0]
    assert body["type"] == "body"
    assert body["parameters"][0]["text"] == "Текст уведомления"
