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


_CHANNEL_PREFIX = {"wa": "whatsapp", "email": "email", "tg": "telegram"}


def parse_recipient(ref: str | None) -> tuple[str, str] | None:
    """Канонический ref получателя → (channel, address).

    Форматы: 'wa:+7999…' → whatsapp, 'email:x@y' → email, 'tg:123' или
    голый id → telegram. Адрес канала ездит внутри ref'а — издатели
    NOTIFICATION_REQUESTED не меняются при добавлении нового канала.
    """
    if not ref:
        return None
    s = str(ref)
    prefix, sep, rest = s.partition(":")
    if sep and prefix in _CHANNEL_PREFIX and rest:
        return _CHANNEL_PREFIX[prefix], rest
    return "telegram", s


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
    # Канал-bound ref ('wa:+7…'): user_channels не консультируем — адрес уже
    # внутри; fallback на другой канал невозможен (его у получателя нет).
    if telegram_id:
        ch_addr = parse_recipient(telegram_id)
        if ch_addr and ch_addr[0] != "telegram":
            if get_sender(ch_addr[0]):
                return ch_addr
            logger.info("No sender registered for channel %s — dropped", ch_addr[0])
            return None

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
    if telegram_id:
        return "telegram", str(telegram_id)
    return None
