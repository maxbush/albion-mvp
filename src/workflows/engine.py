"""Workflow Engine — управление жизненным циклом воркфлоу.

Отложенные действия теперь через SQLite (scheduled_actions), не через JSON _delayed.
"""

import json, logging
from datetime import datetime, timezone, timedelta

from src.db.repository import WorkflowRepository, ScheduledActionRepository
from src.events.bus import bus
from src.events.types import Event, EventTypes

logger = logging.getLogger(__name__)


class WorkflowEngine:
    def __init__(self, db_path: str = "albion.db"):
        self.repo = WorkflowRepository(db_path)
        self.scheduler = ScheduledActionRepository(db_path)

    async def start_workflow(self, wtype: str, data: dict | None = None) -> int:
        wid = await self.repo.create(wtype, "running", data)
        logger.info("Workflow: %s #%d", wtype, wid)
        await bus.publish(Event(EventTypes.WORKFLOW_STARTED, {"workflow_id": wid, **(data or {})}))
        return wid

    async def complete_workflow(self, wid: int, result: dict | None = None) -> None:
        await self.repo.update_state(wid, "completed", result or {})
        wf = await self.repo.get(wid)
        await bus.publish(Event(EventTypes.WORKFLOW_COMPLETED, {"workflow_id": wid, **(result or {})}))

    async def fail_workflow(self, wid: int, error: str) -> None:
        await self.repo.update_state(wid, "failed", {"error": error})
        await bus.publish(Event(EventTypes.WORKFLOW_FAILED, {"workflow_id": wid, "error": error}))

    async def schedule_action(self, wid: int, delay_min: int, action: str, payload: dict | None = None) -> str:
        """Сохраняет отложенное действие в SQLite."""
        execute_at = (datetime.now(timezone.utc) + timedelta(minutes=delay_min)).isoformat()
        aid = await self.scheduler.create(wid, execute_at, action, payload)
        logger.info("Scheduled #%d: %s in %dmin (id=%s)", wid, action, delay_min, aid)
        return aid


# Global singleton
from src.config import settings
import re
_match = re.match(r'sqlite\+aiosqlite:///(.+)', settings.database_url)
_db_path = _match.group(1) if _match else "albion.db"
engine = WorkflowEngine(_db_path)
