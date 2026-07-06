"""
ALBION MVP — точка входа.

Запуск:
    python -m src.main              # Polling (dev)
    python -m src.main --webhook    # Webhook (prod)
"""

import argparse, asyncio, logging, sys
from telegram.ext import ApplicationBuilder
from src.config import settings
from src.db.migrations import init_db
from src.utils.logging import setup_logging
from src.events.bus import bus

from src.workflows.dlq_handler import register_handlers as rdq
from src.workflows.absence import register_handlers as ra
from src.workflows.lead_capture import register_handlers as rl
from src.workflows.cancellation import register_handlers as rc
from src.ai.classifier import register_handlers as rx
from src.bot.handlers import setup_handlers
from src.scheduler.scheduler import scheduler_loop

logger = logging.getLogger(__name__)


async def register_all():
    await rdq()
    await ra(); await rl(); await rc(); await rx()
    logger.info("Handlers registered. Events: %s", bus.get_subscribed_events())


async def cleanup_idempotency():
    """Фоновая задача: чистка старых idempotency keys раз в час."""
    while True:
        await asyncio.sleep(3600)
        try:
            from src.db.repository import IdempotencyRepository
            repo = IdempotencyRepository()
            await repo.cleanup_old(24)
            logger.debug("Idempotency keys cleaned up")
        except Exception as e:
            logger.error("Cleanup error: %s", e)


async def main(webhook: bool = False):
    setup_logging()
    logger.info("🚀 %s (webhook=%s)", settings.app_name, webhook)

    await init_db()
    await register_all()

    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    setup_handlers(app)

    # Фоновые задачи
    asyncio.create_task(scheduler_loop(30))
    asyncio.create_task(cleanup_idempotency())

    if webhook:
        url = settings.telegram_webhook_url
        if not url:
            logger.error("WEBHOOK_URL required")
            sys.exit(1)
        await app.bot.set_webhook(url=url, secret_token=settings.telegram_webhook_secret)
        await app.run_webhook(listen="0.0.0.0", port=8443, secret_token=settings.telegram_webhook_secret)
    else:
        logger.info("Polling mode...")
        await app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--webhook", action="store_true")
    try: asyncio.run(main(webhook=p.parse_args().webhook))
    except KeyboardInterrupt: logger.info("Shutdown.")
