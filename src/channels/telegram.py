"""Telegram-отправитель — тонкая обёртка над telegram.Bot."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.channels.base import ChannelButton, ChannelSender, SendResult

logger = logging.getLogger(__name__)


class TelegramSender(ChannelSender):
    name = "telegram"

    def __init__(self, bot):
        self._bot = bot

    async def send(
        self,
        address: str,
        text: str,
        buttons: list[ChannelButton] | None = None,
    ) -> SendResult:
        reply_markup = None
        if buttons:
            reply_markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(b.label, callback_data=b.callback_id, url=b.url)]
                for b in buttons
            ])
        msg = await self._bot.send_message(
            chat_id=address, text=text, reply_markup=reply_markup,
        )
        return SendResult(ok=True, channel=self.name, message_id=str(msg.message_id))
