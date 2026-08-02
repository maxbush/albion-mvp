"""Workflow: честный fallback для непокрытых интентов (question/other).

Классификатор ставит intent'ы, у которых не было подписчиков — пользователь
получал «Обрабатываю...» и вечную тишину (R7-1, разрыв обещания интерфейса).
Теперь: мгновенный честный ответ пользователю + передача текста координаторам
(management by exception — человек разбирает то, что машина не поняла).
"""

import logging

from src.db.repository import UserRepository
from src.events.bus import bus
from src.events.types import Event, EventTypes

logger = logging.getLogger(__name__)


class FallbackWorkflow:
    def __init__(self, db_path: str | None = None):
        self.users = UserRepository(db_path)
        self._db_path = db_path

    async def handle_classified(self, event: Event) -> None:
        intent = event.data.get("intent")
        if intent not in ("question", "other"):
            return  # остальные интенты покрыты своими workflow
        text = (event.data.get("text") or "").strip()
        tg = str(event.data.get("telegram_id") or "")
        if not text or not tg:
            return

        # Пользователю — честный ack на его языке (тьюторы — EN).
        from src.utils.i18n import lang_of, tr
        await bus.publish(Event(EventTypes.NOTIFICATION_REQUESTED, {
            "telegram_id": tg,
            "message": tr("fb_user_ack", await lang_of(tg)),
        }))

        # Координаторам — текст + кнопка ответа. Сырой TG в текст не пишем (П9):
        # действие («написать») доступно url-кнопкой, имя — из карточки.
        from src.bot.roles import notify_all_coordinators
        user = await self.users.get_by_telegram_id(tg)
        author = (user or {}).get("name") or "—"
        role = (user or {}).get("role") or "?"
        await notify_all_coordinators(
            f"💬 Сообщение из чата ({role}: {author})\n💬 «{text[:500]}»",
            notification_type="user_question",
            db_path=self._db_path,
            buttons=[{"text": "👤 Написать пользователю", "url": f"tg://user?id={tg}"}],
        )
        logger.info("Fallback: intent=%s from %s forwarded to coordinators", intent, tg)


async def register_handlers() -> None:
    wf = FallbackWorkflow()
    bus.subscribe(EventTypes.MESSAGE_CLASSIFIED, wf.handle_classified)
    logger.info("Fallback workflow registered (question/other intents)")
