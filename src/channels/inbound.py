"""Входящие сообщения внешних каналов (WhatsApp и далее).

Webhook-процесс (src/api/whatsapp.py) кладёт немедленную scheduled_action
(action=inbound_text/inbound_callback) — бот забирает её claim'ом и
публикует на своём bus. Межпроцессный канал — БД: тот же паттерн, что у
MeritHub webhook → scheduler (см. DECISIONS.md D3/D4).
"""

import json
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


async def recipient_aliases(ref: str, db_path: str | None = None) -> set[str]:
    """Все адресные рефы того же человека: TG id, 'wa:+…', голый телефон.

    Записи (enrollment, workflow data) пишутся под одним каналом, а актор
    после привязки может прийти с другого — без расширения списка записи,
    сделанные под альтернативным каналом, невидимы актору."""
    refs = {str(ref)}
    try:
        from src.db.repository import MeritHubContactRepository, UserRepository
        ucr = UserChannelRepository(db_path)
        tg = str(ref)
        row = None
        if tg.startswith("wa:"):
            row = await ucr.find_by_address("whatsapp", tg[3:])
        else:
            row = await ucr.find_by_address("telegram", tg)
        if row:
            for c in await ucr.list_for_user(row["user_id"]):
                refs.add(f"wa:{c['address']}" if c["channel"] == "whatsapp"
                         else c["address"])
        contacts = MeritHubContactRepository(db_path)
        phone = normalize_phone(tg)
        if phone:
            crow = await contacts.get_by_phone(phone)
            if crow and crow.get("telegram_id"):
                refs.add(str(crow["telegram_id"]))
        if not tg.startswith("wa:"):
            crow = await contacts.get_by_telegram(tg)
            np = normalize_phone((crow or {}).get("phone"))
            if np:
                refs.add(f"wa:{np}")
                refs.add(np)
    except Exception:
        logger.exception("recipient alias expansion failed for %s", ref)
    return refs


async def resolve_user_for_ref(ref: str, db_path: str | None = None) -> dict | None:
    """users-запись по адресному рефу получателя.

    'wa:+…' → контакт (phone → telegram_id) → user. Голый TG id — как раньше.
    Нужен везде, где раньше стоял get_by_telegram_id: 'wa:'-родитель без
    TG-аккаунта не «незарегистрирован» — ему можно слать в WhatsApp."""
    from src.db.repository import UserRepository
    users = UserRepository(db_path)
    user = await users.get_by_telegram_id(str(ref))
    if user or not str(ref).startswith("wa:"):
        return user
    from src.db.repository import MeritHubContactRepository
    crow = await MeritHubContactRepository(db_path).get_by_phone(
        normalize_phone(str(ref)[3:]))
    if crow and crow.get("telegram_id"):
        return await users.get_by_telegram_id(str(crow["telegram_id"]))
    return None


_BTN_MAP_PREFIX = "wa_btns:"


async def save_button_map(phone: str, callback_ids: list[str],
                          db_path: str | None = None) -> None:
    """Адрес → последние нумерованные callback-кнопки.

    Общий маппинг WA-провайдеров: нумерованные опции («Ответьте цифрой»)
    у Meta-шаблона и у Twilio одинаково разворачиваются webhook'ом обратно
    в исходный callback_id.
    """
    from src.db.repository import SystemSettingsRepository
    await SystemSettingsRepository(db_path).set(
        f"{_BTN_MAP_PREFIX}{normalize_phone(phone)}", json.dumps(callback_ids))


async def load_button_map(phone: str, db_path: str | None = None) -> list[str]:
    from src.db.repository import SystemSettingsRepository
    raw = await SystemSettingsRepository(db_path).get(
        f"{_BTN_MAP_PREFIX}{normalize_phone(phone)}")
    try:
        return json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []
