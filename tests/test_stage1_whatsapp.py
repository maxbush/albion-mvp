"""Тесты Этапа 1: WhatsApp Meta Cloud API, маршрутизация каналов, вебхуки."""

import json
import pytest
from starlette.testclient import TestClient

from src.config import settings
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.integrations.whatsapp_client import normalize_phone, WhatsAppClient
from src.integrations.whatsapp_mock import MockWhatsAppService
from src.integrations.factory import get_whatsapp_service
from src.notifications.channel_router import ChannelRouter
from src.db.repository import UserRepository, MeritHubContactRepository, NotificationRepository


def test_normalize_phone():
    assert normalize_phone("+44 7493 994501") == "447493994501"
    assert normalize_phone("+7 (701) 123-45-67") == "77011234567"
    assert normalize_phone("380505292480") == "380505292480"
    assert normalize_phone(None) == ""
    assert normalize_phone("") == ""


@pytest.mark.asyncio
async def test_mock_whatsapp_service():
    wa = MockWhatsAppService()
    wa.reset()
    res1 = await wa.send_text("+44 7493 994501", "Hello from Albion!")
    assert res1["messaging_product"] == "whatsapp"
    assert len(wa.sent_messages) == 1
    assert wa.sent_messages[0]["to"] == "447493994501"
    assert wa.sent_messages[0]["text"] == "Hello from Albion!"

    res2 = await wa.send_interactive_buttons(
        "+44 7493 994501",
        "Ученик на уроке?",
        [
            {"id": "btn1", "text": "✅ Да"},
            {"id": "btn2", "text": "❌ Нет"},
        ]
    )
    assert len(wa.sent_messages) == 2
    assert len(wa.sent_messages[1]["buttons"]) == 2


@pytest.mark.asyncio
async def test_channel_router_resolves_and_sends_whatsapp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")

    # Пользователь с телефоном, предпочитает WhatsApp
    u_repo = UserRepository("albion.db")
    await u_repo.create("tg_user_1", "parent", "Мама", phone="+447493994501")
    await u_repo._execute(
        "UPDATE users SET preferred_channel='whatsapp' WHERE telegram_id='tg_user_1'"
    )

    wa_mock = get_whatsapp_service()
    if isinstance(wa_mock, MockWhatsAppService):
        wa_mock.reset()

    router = ChannelRouter(bot_app=None, db_path="albion.db")
    target = await router.resolve_target({"telegram_id": "tg_user_1"})
    assert target["channel"] == "whatsapp"
    assert target["phone"] == "447493994501"

    sent = await router.send({
        "telegram_id": "tg_user_1",
        "message": "Уведомление в WhatsApp",
        "buttons": [{"text": "ОК", "callback_data": "ack"}],
    })
    assert sent is True
    if isinstance(wa_mock, MockWhatsAppService):
        assert len(wa_mock.sent_messages) >= 1
        assert wa_mock.sent_messages[-1]["to"] == "447493994501"


@pytest.mark.asyncio
async def test_channel_router_sends_telegram_when_tg_preferred(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")

    u_repo = UserRepository("albion.db")
    await u_repo.create("tg_user_2", "parent", "Папа")

    class FakeApp:
        class FakeBot:
            def __init__(self):
                self.sent = []

            async def send_message(self, chat_id, text, reply_markup=None):
                self.sent.append({"chat_id": chat_id, "text": text})

        def __init__(self):
            self.bot = self.FakeBot()

    fake_app = FakeApp()
    router = ChannelRouter(bot_app=fake_app, db_path="albion.db")
    sent = await router.send({
        "telegram_id": "tg_user_2",
        "message": "Уведомление в TG",
    })
    assert sent is True
    assert len(fake_app.bot.sent) == 1
    assert fake_app.bot.sent[0]["chat_id"] == "tg_user_2"


def test_whatsapp_webhook_verification(monkeypatch):
    from src.api.webhook import app
    client = TestClient(app)
    # 1. Успешная верификация
    res = client.get(
        f"{settings.whatsapp_webhook_path}?hub.mode=subscribe&hub.verify_token={settings.whatsapp_webhook_verify_token}&hub.challenge=123456"
    )
    assert res.status_code == 200
    assert res.text == "123456"

    # 2. Неверный токен
    res_bad = client.get(
        f"{settings.whatsapp_webhook_path}?hub.mode=subscribe&hub.verify_token=wrong_token&hub.challenge=123456"
    )
    assert res_bad.status_code == 403


@pytest.mark.asyncio
async def test_whatsapp_webhook_incoming_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.db.migrations import init_db
    await init_db("albion.db")
    from src.api.webhook import app

    captured_events = []

    async def _cap(ev):
        captured_events.append(ev)

    bus.subscribe(EventTypes.MESSAGE_INCOMING, _cap)
    try:
        client = TestClient(app)
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "wa_acc_1",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "messages": [
                                    {
                                        "from": "447493994501",
                                        "id": "wamid.123",
                                        "type": "text",
                                        "text": {"body": "Мы заболели, не сможем прийти"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        res = client.post(settings.whatsapp_webhook_path, json=payload)
        assert res.status_code == 200
        assert len(captured_events) == 1
        assert captured_events[0].data["phone"] == "447493994501"
        assert "заболели" in captured_events[0].data["text"]
        assert captured_events[0].data["source"] == "whatsapp"
    finally:
        bus.unsubscribe(EventTypes.MESSAGE_INCOMING, _cap)
