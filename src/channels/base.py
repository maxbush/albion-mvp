"""Канал-нейтральные типы для исходящих уведомлений.

Workflow/handlers оперируют этими типами и не знают, в какой канал уйдёт
сообщение. Каждый ChannelSender маппит их на нативные средства своего канала:
Telegram — InlineKeyboardMarkup, WhatsApp — interactive buttons / CTA URL.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ChannelButton:
    """Одна кнопка действия под уведомлением.

    Ровно один из вариантов:
    - callback_id — действие в нашей системе ("resolve:1:abc"), канал
      приводит ответ обратно к этой же грамматике;
    - url — внешняя ссылка (CTA), ответа не ждём.
    """
    label: str
    callback_id: str | None = None
    url: str | None = None


@dataclass
class SendResult:
    ok: bool
    channel: str
    message_id: str | None = None
    error: str | None = None


class ChannelSender(ABC):
    """Отправитель исходящих сообщений в конкретный канал."""

    name: str = ""

    @abstractmethod
    async def send(
        self,
        address: str,
        text: str,
        buttons: list[ChannelButton] | None = None,
    ) -> SendResult:
        """Отправить текст получателю по адресу канала
        (TG chat_id / E.164 phone / email)."""
