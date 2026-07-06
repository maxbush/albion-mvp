import logging
from src.ai.client import llm_client
from src.events.types import Event, EventTypes
from src.events.bus import bus
logger = logging.getLogger(__name__)

async def handle_message_incoming(event):
    text = event.data.get("text","")
    if not text: return
    cls = await llm_client.classify_intent(text)
    logger.info("Classified: %s (%.2f)", cls["intent"], cls.get("confidence",0))
    await bus.publish(Event(EventTypes.MESSAGE_CLASSIFIED, {
        "text": text, "telegram_id": event.data.get("telegram_id"),
        "intent": cls["intent"], "confidence": cls.get("confidence",0),
    }))

async def register_handlers():
    bus.subscribe(EventTypes.MESSAGE_INCOMING, handle_message_incoming)
    logger.info("AI classifier registered")
