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
from src.config import settings
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
            # Свободный ответ родителя на открытый инцидент неявки —
            # интерпретируем и резолвим как в TG-пути (handle_message),
            # иначе текст ушёл бы в классификатор и инцидент не закрылся.
            if await self._try_incident_reply(actor, text, sender, address):
                return
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
        if cb.startswith(("checkin:", "checkin_late_time:")):
            # Ответ на напоминание перед уроком (checkin-кнопки).
            from src.workflows.parent_actions import process_checkin_callback
            reply, cbuttons = await process_checkin_callback(
                cb, actor, db_path=self.db_path)
            if sender and reply:
                try:
                    await sender.send(address, reply, cbuttons)
                except Exception as e:
                    logger.error("WA checkin ack send failed: %s", e)
            return
        from src.workflows.cancellation import (
            is_cancel_resched_callback, process_cancel_resched_callback)
        if is_cancel_resched_callback(cb):
            reply, crbuttons = await process_cancel_resched_callback(
                cb, actor, db_path=self.db_path)
            if sender and reply:
                try:
                    await sender.send(address, reply, crbuttons)
                except Exception as e:
                    logger.error("WA cancel/resched ack send failed: %s", e)
            return
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


    async def _try_incident_reply(self, actor: str, text: str, sender, address: str) -> bool:
        """Свободный текст в активном сценарии родителя → обработка по месту.

        Порядок — как в TG-пути (bot/handlers.handle_message):
        1) отложенный шаг визарда (parent_resched — предложение времени),
        2) свободный ответ на checkin-напоминание перед уроком,
        3) ответ по открытому инциденту неявки (закрытие/пометка),
        4) по уже эскалированному — форвард координаторам.
        True — сообщение поглощено, дальше не публикуем."""
        import json
        import re

        from src.ai.client import llm_client
        from src.db.repository import (
            ScheduledActionRepository, WizardStateRepository, WorkflowRepository,
        )
        from src.workflows.absence import AbsenceWorkflow

        # 1) Визард переноса: «на какое время?» — текст = предложение слота.
        wiz = WizardStateRepository(self.db_path)
        pending = await wiz.get(actor)
        if pending and pending.get("flow") == "parent_resched":
            try:
                pdata = json.loads(pending.get("data") or "{}")
            except Exception:
                pdata = {}
            await wiz.delete(actor)
            await bus.publish(Event(EventTypes.RESCHEDULE_REQUESTED, {
                "class_id": pdata.get("class_id"),
                "occurrence_date": pdata.get("occurrence_date"),
                "proposed": text,
                "is_paid": pdata.get("is_paid"),
                "free_hours": settings.albion_reschedule_free_hours,
                "requested_by": actor,
            }))
            if sender:
                try:
                    await sender.send(
                        address,
                        "🔁 Запрос на перенос передан координатору — он свяжется с вами.")
                except Exception as e:
                    logger.error("WA resched ack send failed: %s", e)
            return True

        # 2) Активный checkin: текст = ответ на напоминание перед уроком.
        from src.workflows.lesson_ops import LessonOpsWorkflow
        ops = LessonOpsWorkflow(self.db_path)
        active_checkin = await ops.find_active_checkin(actor, ("parent",))
        if active_checkin:
            wid = active_checkin[0]
            try:
                interpreted = await llm_client.interpret_parent_reply(text)
                status = interpreted.get("status", "other")
                action = ("ready" if status == "ok"
                          else (status if status in {"late", "no_show"} else "other"))
                await ops.record_checkin_response(
                    wid, actor_tg=actor, action=action, free_text=text)
                checkin_reply_map = {
                    "ready": "✅ Спасибо! Отметили, что всё в порядке.",
                    "no_show": "❌ Спасибо! Отметили, что сегодня занятия не будет. Координатор уведомлён.",
                    "late": "⏰ Спасибо! Отметили, что ученик опоздает. Координатор уведомлён.",
                    "other": "💬 Спасибо! Передали ответ координатору для ручной обработки.",
                }
                if sender:
                    try:
                        await sender.send(
                            address, checkin_reply_map.get(action, checkin_reply_map["other"]))
                    except Exception as e:
                        logger.error("WA checkin-reply ack send failed: %s", e)
            except Exception as e:
                logger.error("WA checkin processing failed: %s", e, exc_info=True)
            return True

        # 3-4) Инцидент неявки.
        wf = AbsenceWorkflow(self.db_path)
        active = await wf.find_active_incident_for_parent(actor)
        reply_map = {
            "ok": "✅ Спасибо! Отметили, что всё в порядке. Координатор уведомлён.",
            "no_show": "❌ Спасибо! Отметили, что сегодня занятия не будет. Координатор уведомлён.",
            "late": "⏰ Спасибо! Отметили, что ученик опоздает. Координатор уведомлён.",
            "other": "💬 Спасибо! Передали ответ координатору для ручной обработки.",
        }
        if active:
            inc_id, _ = active
            interpreted = await llm_client.interpret_parent_reply(text)
            status = interpreted.get("status", "other")
            resolution_map = {
                "ok": "parent_ok",
                "no_show": "parent_not_coming",
                "late": "parent_late",
                "other": "parent_text_reply",
            }
            late_minutes = None
            if status == "late":
                m = re.search(r"(\d{1,3})\s*мин", text)
                if m:
                    late_minutes = m.group(1)
            if status == "other":
                # Непонятный ответ не закрывает инцидент — review до разбора.
                await wf.incidents.update_status(inc_id, "review")
                wf_rows = await WorkflowRepository(self.db_path).find_by_json(
                    "incident_id", inc_id, limit=1)
                if wf_rows:
                    await ScheduledActionRepository(
                        self.db_path).cancel_by_workflow(wf_rows[0]["id"])
                await wf.notify_coordinators_parent_reply(
                    inc_id, "free_text", parent_text=text,
                    parent_telegram_id=actor, review=True)
            else:
                await wf.resolve_absence(
                    inc_id, actor,
                    resolution=resolution_map.get(status, "parent_text_reply"))
                await wf.notify_coordinators_parent_reply(
                    inc_id,
                    status if status in {"ok", "no_show", "late"} else "free_text",
                    parent_text=text,
                    parent_telegram_id=actor,
                    late_minutes=late_minutes,
                )
            if sender:
                try:
                    await sender.send(address, reply_map.get(status, reply_map["other"]))
                except Exception as e:
                    logger.error("WA incident-reply ack send failed: %s", e)
            return True

        escalated = await wf.find_escalated_incident_for_parent(actor)
        if escalated:
            inc_id, _ = escalated
            await wf.notify_coordinators_parent_reply(
                inc_id, "free_text", parent_text=text, parent_telegram_id=actor)
            if sender:
                try:
                    await sender.send(
                        address,
                        "💬 Спасибо за ответ! Координатор уже был уведомлён ранее, "
                        "но ваш ответ передан для учёта.")
                except Exception as e:
                    logger.error("WA late-reply ack send failed: %s", e)
            return True
        return False


async def register_handlers(db_path: str | None = None) -> None:
    h = InboundHandlers(db_path)
    bus.subscribe(EventTypes.SCHEDULER_TICK, h.handle_scheduler_tick)
    logger.info("Inbound handlers registered")
