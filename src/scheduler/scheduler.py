"""Планировщик отложенных действий — читает из SQLite.

Больше никаких in-memory списков. Все отложенные задачи переживают рестарт.
"""

import asyncio
import json
import logging

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
                aid = task["id"]
                try:
                    payload = json.loads(task["payload"])
                    report = await bus.publish(Event(EventTypes.SCHEDULER_TICK, {
                        "action_id": aid,
                        "action": task["action"],
                        "workflow_id": task["workflow_id"],
                        "data": payload,
                        "execute_at": task["execute_at"],
                    }))
                    if report.total_handlers == 0:
                        raise RuntimeError(f"No handlers for scheduled action: {task['action']}")
                    if report.failed:
                        first_error = report.errors[0]["error"] if report.errors else "unknown error"
                        if int(task.get("attempts") or 0) >= MAX_RETRIES:
                            await repo.mark_failed(aid, first_error)
                            logger.error("Scheduler: action %s failed permanently: %s", aid, first_error)
                        else:
                            await repo.requeue(aid, first_error)
                            logger.warning("Scheduler: action %s requeued after handler failure: %s", aid, first_error)
                    else:
                        await repo.mark_done(aid)
                except Exception as e:
                    if int(task.get("attempts") or 0) >= MAX_RETRIES:
                        await repo.mark_failed(aid, str(e))
                        logger.error("Scheduler: action %s failed permanently: %s", aid, e, exc_info=True)
                    else:
                        await repo.requeue(aid, str(e))
                        logger.warning("Scheduler: action %s requeued after execution error: %s", aid, e, exc_info=True)

            if tasks:
                logger.info("Scheduler: fired %d actions", len(tasks))

        except Exception as e:
            logger.error("Scheduler tick error: %s", e, exc_info=True)

        await asyncio.sleep(interval)
