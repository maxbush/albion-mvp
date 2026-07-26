import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from src.events.types import Event, EventTypes

logger = logging.getLogger(__name__)

HANDLER_TIMEOUT = 10.0  # seconds


@dataclass
class PublishReport:
    total_handlers: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: list[dict] = field(default_factory=list)


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, list] = defaultdict(list)

    def subscribe(self, event_type: str, handler) -> None:
        self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler) -> None:
        if handler in self._subscribers[event_type]:
            self._subscribers[event_type].remove(handler)

    async def publish(self, event: Event) -> PublishReport:
        logger.info("Event: %s %s", event.type, event.data)

        handlers = self._subscribers.get(event.type, []) + self._subscribers.get("*", [])
        report = PublishReport(total_handlers=len(handlers))

        for handler in handlers:
            try:
                await asyncio.wait_for(handler(event), timeout=HANDLER_TIMEOUT)
                report.succeeded += 1
            except asyncio.TimeoutError:
                error = f"Timeout {HANDLER_TIMEOUT}s"
                logger.error("TIMEOUT [%ss]: %s.%s", HANDLER_TIMEOUT, event.type, handler.__name__)
                report.failed += 1
                report.errors.append({"handler": handler.__name__, "error": error})
                await self._publish_alert(event, handler, error)
            except Exception as e:
                logger.error("FAILED: %s.%s — %s", event.type, handler.__name__, e, exc_info=True)
                report.failed += 1
                report.errors.append({"handler": handler.__name__, "error": str(e)})
                await self._publish_alert(event, handler, str(e))
        return report

    async def _publish_alert(self, event: Event, handler, error: str) -> None:
        """Публикует алерт в систему (DLQ handler подхватит и запишет в БД)."""
        if event.type != EventTypes.SYSTEM_DLQ_ALERT:
            await self.publish(Event(EventTypes.SYSTEM_DLQ_ALERT, {
                "event_type": event.type,
                "handler": handler.__name__,
                "error": error,
                "event_data": event.data,
            }))

    def get_subscribed_events(self) -> list[str]:
        return list(self._subscribers.keys())


bus = EventBus()
