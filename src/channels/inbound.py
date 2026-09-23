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


def normalize_phone(raw: str | None) -> str:
    """Номер → канонический адрес '+E.164' для whatsapp-канала.

    Принимает '+7 999 000-11-22', '89990001122', 'whatsapp:+7…' и т.п.
    Пустой ввод → ''."""
    if not raw:
        return ""
    digits = "".join(c for c in raw if c.isdigit())
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    return f"+{digits}" if digits else ""


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
    if channel == "whatsapp":
        # Отметка 24h-окна Meta: любое входящее открывает окно свободных
        # ответов. WhatsAppSender читает её для выбора session vs template.
        try:
            from src.db.repository import SystemSettingsRepository
            await SystemSettingsRepository(db_path).set(
                f"wa_window:{normalize_phone(address)}", now)
        except Exception:
            logger.exception("wa_window mark failed for %s", address)
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
        row = await _autobind_contact(channel, address, db_path)
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


async def _autobind_contact(channel: str, address: str,
                            db_path: str | None = None) -> dict | None:
    """Привязка канала к пользователю по merithub_contacts.

    WA-входящее от номера из contacts (импорт/регистрация): если у контакта
    есть telegram_id и по нему есть users-запись — создаём user_channels:
    telegram preferred (полный UX), whatsapp привязан. Возвращает свежую
    строку user_channels (или None — адрес остаётся 'wa:'-anonymous).
    """
    if channel != "whatsapp":
        return None
    from src.db.repository import MeritHubContactRepository, UserRepository
    phone = normalize_phone(address)
    contact = await MeritHubContactRepository(db_path).get_by_phone(phone)
    if not contact or not contact.get("telegram_id"):
        return None
    user = await UserRepository(db_path).get_by_telegram_id(str(contact["telegram_id"]))
    if not user:
        return None
    ucr = UserChannelRepository(db_path)
    await ucr.set(user["id"], "telegram", str(contact["telegram_id"]), preferred=True)
    await ucr.set(user["id"], "whatsapp", phone, preferred=False)
    logger.info("Autobound whatsapp %s → user %s", phone, user["id"])
    return {"user_id": user["id"], "channel": channel, "address": address}
