"""Фабрика каналов: real WhatsApp клиент или mock по наличию ключей
(тот же vendor-agnostic принцип, что у MeritHub — D5)."""

from src.channels.base import ChannelSender
from src.channels.whatsapp import MockWhatsAppClient, WhatsAppClient, WhatsAppSender
from src.config import settings


def get_whatsapp_sender() -> ChannelSender:
    """WhatsApp-отправитель по выбранному провайдеру (WHATSAPP_PROVIDER).

    meta   — Meta Cloud API (Graph v21);
    twilio — Twilio Messages API (BSP-провайдер клиента, см. ALBION_CONTEXT).
    """
    if settings.whatsapp_provider == "twilio":
        from src.channels.twilio import MockTwilioClient, TwilioClient, TwilioSender
        if settings.twilio_use_real:
            return TwilioSender(TwilioClient())
        return TwilioSender(MockTwilioClient())
    if settings.whatsapp_use_real:
        return WhatsAppSender(WhatsAppClient())
    return WhatsAppSender(MockWhatsAppClient())
