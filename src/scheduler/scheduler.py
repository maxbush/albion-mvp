"""Планировщик отложенных действий — читает из SQLite.

Больше никаких in-memory списков. Все отложенные задачи переживают рестарт.
"""

import asyncio, logging
from datetime import datetime, timezone

from src.db.repository import ScheduledActionRepository
from src.events.bus import bus
from src.events.types import Event, EventTypes

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


async def scheduler_loop(interval: int = 30) -> None:
    """Тикает каждые interval секунд, забирает просроченные задачи из SQLite."""
    logger.info("Scheduler: started (interval=%ds, max_retries=%d)", interval, MAX_RETRIES)

    # Фоновый cleanup раз в час
    async def cleanup_loop():
        while True:
            await asyncio.sleep(3600)
            try:
                repo = ScheduledActionRepository()
                await repo.cleanup_old(24)
            except Exception as e:
                logger.error("Scheduler cleanup error: %s", e)

    asyncio.create_task(cleanup_loop())

    while True:
        try:
            repo = ScheduledActionRepository()
            tasks = await repo.claim_pending(limit=20)

            for task in tasks:
                payload = __import__("json").loads(task["payload"])
                await bus.publish(Event(EventTypes.SCHEDULER_TICK, {
                    "action_id": task["id"],
                    "action": task["action"],
                    "workflow_id": task["workflow_id"],
                    "data": payload,
                    "execute_at": task["execute_at"],
                }))

            if tasks:
                logger.info("Scheduler: fired %d actions", len(tasks))

        except Exception as e:
            logger.error("Scheduler tick error: %s", e, exc_info=True)

        await asyncio.sleep(interval)
