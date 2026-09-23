"""Фабрика каналов: real WhatsApp клиент или mock по наличию ключей
(тот же vendor-agnostic принцип, что у MeritHub — D5)."""

from src.channels.base import ChannelSender
from src.channels.whatsapp import MockWhatsAppClient, WhatsAppClient, WhatsAppSender
from src.config import settings


def get_whatsapp_sender() -> ChannelSender:
    if settings.whatsapp_use_real:
        return WhatsAppSender(WhatsAppClient())
    return WhatsAppSender(MockWhatsAppClient())
