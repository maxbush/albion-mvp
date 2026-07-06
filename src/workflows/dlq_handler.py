"""Обработчик Dead Letter Queue — подписывается на SYSTEM_DLQ_ALERT и пишет в БД."""

import logging

from src.db.repository import DeadLetterQueueRepository, WorkflowRepository
from src.events.bus import bus
from src.events.types import Event, EventTypes

logger = logging.getLogger(__name__)


async def handle_dlq_alert(event: Event) -> None:
    """Записывает упавшее событие в dead_letter_queue."""
    data = event.data
    dlq = DeadLetterQueueRepository()

    await dlq.put(
        source="event_bus",
        event_type=data.get("event_type"),
        payload=data.get("event_data", {}),
        error=data.get("error", "unknown"),
    )

    # Если был workflow_id в event_data — помечаем как failed
    wf_id = data.get("event_data", {}).get("workflow_id")
    if wf_id:
        wf_repo = WorkflowRepository()
        wf = await wf_repo.get(wf_id)
        if wf and wf["state"] != "failed":
            await wf_repo.update_state(wf_id, "failed", {"error": data.get("error")})
            logger.info("Workflow #%d marked failed via DLQ", wf_id)

    dlq_count = await dlq.count()
    logger.warning("DLQ: stored event %s (total in DLQ: %d)", data.get("event_type"), dlq_count)


async def register_handlers() -> None:
    bus.subscribe(EventTypes.SYSTEM_DLQ_ALERT, handle_dlq_alert)
    logger.info("DLQ handler registered")
