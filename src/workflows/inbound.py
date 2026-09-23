"""Входящие внешних каналов → bus бота.

webhook-процесс (src/api/whatsapp.py) пишет немедленные scheduled_actions:
  inbound_text     — свободный текст → MESSAGE_INCOMING (AI-классификатор,
                     существующие сценарии ответов);
  inbound_callback — кнопка (callback_id той же грамматики, что у TG
                     callback_data) → parent_actions / forward координатору.
scheduler_loop бота публикует их как SCHEDULER_TICK — здесь они превращаются
в события bus. Ответы уходят обратно в исходный канал через canonical
recipient ('wa:+7…' — см. channels/router.parse_recipient).
"""

import logging

from src.channels.inbound import canonical_recipient
from src.channels.router import get_sender
from src.events.bus import bus
from src.events.types import Event, EventTypes
from src.workflows.parent_actions import is_parent_callback, process_parent_callback

logger = logging.getLogger(__name__)

_ACTIONS = ("inbound_text", "inbound_callback")


class InboundHandlers:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path

    async def handle_scheduler_tick(self, event: Event) -> None:
        if event.type != EventTypes.SCHEDULER_TICK:
            return
        if event.data.get("action") not in _ACTIONS:
            return
        p = event.data.get("data") or {}
        channel, address = p.get("channel"), p.get("address")
        if not channel or not address:
            return
        actor = await canonical_recipient(channel, address, db_path=self.db_path)
        sender = get_sender(channel)

        if event.data["action"] == "inbound_text":
            text = (p.get("text") or "").strip()
            if not text:
                return
            logger.info("Inbound %s text from %s: %r", channel, address, text[:80])
            report = await bus.publish(Event(EventTypes.MESSAGE_INCOMING, {
                "text": text,
                "telegram_id": actor,   # canonical ref: 'wa:+…' или TG id
                "channel": channel,
                "address": address,
                "phone": address if channel == "whatsapp" else None,
                "sender_name": p.get("name"),
            }))
            if report.total_handlers and report.failed:
                raise RuntimeError(
                    f"MESSAGE_INCOMING publish failed for {address}: {report.errors}")
            return

        # inbound_callback
        cb = p.get("callback_id") or ""
        if is_parent_callback(cb):
            res = await process_parent_callback(cb, actor, db_path=self.db_path)
            reply = res.get("text") or res.get("toast")
            if sender and reply:
                try:
                    await sender.send(address, reply, res.get("buttons"))
                except Exception as e:
                    logger.error("WA callback ack send failed: %s", e)
            return

        # Неизвестная/координаторская кнопка — координаторы живут в TG;
        # передаём им контекст и подтверждаем отправителю.
        logger.info("Inbound %s unknown callback %r from %s", channel, cb, address)
        try:
            from src.bot.roles import notify_all_coordinators
            await notify_all_coordinators(
                f"💬 {channel} {address}: нажата кнопка «{cb}» — требуется разбор.",
                notification_type="ops_alert",
                db_path=self.db_path,
            )
        except Exception as e:
            logger.error("Coordinator forward failed: %s", e)
        if sender:
            try:
                await sender.send(address, "Принято — передали координатору 🙌")
            except Exception as e:
                logger.error("WA ack send failed: %s", e)


async def register_handlers(db_path: str | None = None) -> None:
    h = InboundHandlers(db_path)
    bus.subscribe(EventTypes.SCHEDULER_TICK, h.handle_scheduler_tick)
    logger.info("Inbound handlers registered")
