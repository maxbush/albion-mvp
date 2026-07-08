"""
ALBION MVP — точка входа.
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
from src.bot.handlers import setup_handlers, seed_demo_data
from src.scheduler.scheduler import scheduler_loop

logger = logging.getLogger(__name__)


async def register_all():
    await rdq()
    await ra()
    await rl()
    await rc()
    await rx()
    logger.info("Handlers registered. Events: %s", bus.get_subscribed_events())


async def cleanup_idempotency():
    while True:
        await asyncio.sleep(3600)
        try:
            from src.db.repository import IdempotencyRepository
            await IdempotencyRepository().cleanup_old(24)
        except Exception as e:
            logger.error("Cleanup: %s", e)


async def scheduler_wrapper():
    while True:
        try:
            await scheduler_loop(30)
        except Exception as e:
            logger.error("Scheduler crashed: %s, restart in 5s", e)
            await asyncio.sleep(5)


async def main(webhook: bool = False):
    setup_logging()
    logger.info("Start %s (webhook=%s)", settings.app_name, webhook)

    await init_db()
    await register_all()
    await seed_demo_data()

    app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    setup_handlers(app)

    async with app:
        await app.start()
        if webhook:
            url = settings.telegram_webhook_url
            if not url:
                logger.error("WEBHOOK_URL required"); sys.exit(1)
            await app.bot.set_webhook(url=url, secret_token=settings.telegram_webhook_secret)
            logger.info("Webhook: %s", url)
            await asyncio.gather(scheduler_wrapper(), cleanup_idempotency())
        else:
            await app.updater.start_polling(
                allowed_updates=["message", "callback_query"],
                drop_pending_updates=True,
            )
            logger.info("Polling started")
            await asyncio.gather(scheduler_wrapper(), cleanup_idempotency())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--webhook", action="store_true")
    args = p.parse_args()
    try:
        asyncio.run(main(webhook=args.webhook))
    except KeyboardInterrupt:
        logger.info("Shutdown.")
    except Exception as e:
        logger.error("Fatal: %s", e, exc_info=True)
        sys.exit(1)
