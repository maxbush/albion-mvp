"""Роутинг исходящих: получатель → (channel, address) → ChannelSender.

Политика: предпочтительный канал из user_channels, если у него есть
зарегистрированный отправитель; иначе fallback на telegram по telegram_id.
Отправителей регистрируют процессы при старте (бот — TelegramSender).
"""

import logging

from src.channels.base import ChannelSender
from src.db.repository import UserChannelRepository, UserRepository

logger = logging.getLogger(__name__)

_senders: dict[str, ChannelSender] = {}


def register_sender(sender: ChannelSender) -> None:
    _senders[sender.name] = sender


def get_sender(channel: str) -> ChannelSender | None:
    return _senders.get(channel)


def registered_channels() -> list[str]:
    return sorted(_senders)


async def resolve(
    *,
    telegram_id: str | None = None,
    user_id: int | None = None,
    db_path: str | None = None,
) -> tuple[str, str] | None:
    """(channel, address) для исходящего уведомления.

    Предпочтение пользователя из user_channels учитывается только если
    канал реально настроен (есть отправитель) — иначе сообщение ушло бы
    в пустоту. Fallback — telegram по telegram_id.
    """
    uid = user_id
    if uid is None and telegram_id:
        user = await UserRepository(db_path).get_by_telegram_id(str(telegram_id))
        uid = user["id"] if user else None
    if uid is not None:
        pref = await UserChannelRepository(db_path).preferred_for_user(uid)
        if pref and get_sender(pref["channel"]):
            return pref["channel"], pref["address"]
        if pref:
            logger.info(
                "Channel %s preferred by user %s has no sender — telegram fallback",
                pref["channel"], uid,
            )
    if not telegram_id and uid is not None:
        # предпочтение не сработало (нет отправителя) — достаём TG из аккаунта
        user = await UserRepository(db_path).get(uid)
        telegram_id = user["telegram_id"] if user else None
    if telegram_id:
        return "telegram", str(telegram_id)
    return None
