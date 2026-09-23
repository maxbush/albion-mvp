"""Входящие сообщения внешних каналов (WhatsApp и далее).

Webhook-процесс (src/api/whatsapp.py) кладёт немедленную scheduled_action
(action=inbound_text/inbound_callback) — бот забирает её claim'ом и
публикует на своём bus. Межпроцессный канал — БД: тот же паттерн, что у
MeritHub webhook → scheduler (см. DECISIONS.md D3/D4).
"""

import logging
from datetime import datetime, timezone

from src.db.repository import ScheduledActionRepository, UserChannelRepository

logger = logging.getLogger(__name__)

_PREFIX_OF = {"whatsapp": "wa", "email": "email", "telegram": "tg"}


async def enqueue_inbound(
    kind: str,
    *,
    channel: str,
    address: str,
    payload: dict,
    db_path: str | None = None,
) -> str:
    """Поставить входящее в очередь бота. kind: 'inbound_text' | 'inbound_callback'."""
    now = datetime.now(timezone.utc).isoformat()
    aid = await ScheduledActionRepository(db_path).create(
        None, now, kind,
        {"channel": channel, "address": address, **payload},
    )
    logger.info("Inbound %s from %s:%s queued (action %s)", kind, channel, address, aid)
    return aid


async def canonical_recipient(channel: str, address: str, db_path: str | None = None) -> str:
    """Канонический ref отправителя для контекста событий.

    'wa:+7999…' для whatsapp-адресов — издатели ответов передают его как
    telegram_id, а router.parse_recipient направляет в нужный канал.
    Известному пользователю отвечаем на его preferred-канал.
    """
    prefix = _PREFIX_OF.get(channel, channel)
    row = await UserChannelRepository(db_path).find_by_address(channel, address)
    if not row:
        return f"{prefix}:{address}"
    pref = await UserChannelRepository(db_path).preferred_for_user(row["user_id"])
    if pref:
        ch = pref["channel"]
        return f"{_PREFIX_OF.get(ch, ch)}:{pref['address']}" if ch != "telegram" else pref["address"]
    for c in await UserChannelRepository(db_path).list_for_user(row["user_id"]):
        if c["channel"] == "telegram":
            return c["address"]
    return f"{prefix}:{address}"
