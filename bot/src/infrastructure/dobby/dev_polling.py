import asyncio
from typing import Any, Sequence

from aiogram import Bot
from loguru import logger

from src.web.endpoints.telegram import TelegramWebhookEndpoint

POLL_TIMEOUT_SECONDS = 30
RETRY_DELAY_SECONDS = 5


async def run_dev_polling(
    bot: Bot, endpoint: TelegramWebhookEndpoint, allowed_updates: Sequence[str]
) -> None:
    """Local development only: fetch updates with getUpdates instead of a webhook.

    Used when the machine has no public HTTPS address. Updates go through the same handler as
    the webhook endpoint. Payment provider webhooks still need a public address.
    """
    await bot.delete_webhook(drop_pending_updates=False)
    logger.warning("DOBBY_DEV_POLLING is on: receiving Telegram updates by polling")
    tasks: set[asyncio.Task[Any]] = set()
    offset = None

    while True:
        try:
            updates = await bot.get_updates(
                offset=offset,
                timeout=POLL_TIMEOUT_SECONDS,
                allowed_updates=list(allowed_updates),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Polling failed: {e}, retrying in {RETRY_DELAY_SECONDS}s")
            await asyncio.sleep(RETRY_DELAY_SECONDS)
            continue

        for update in updates:
            offset = update.update_id + 1
            task = asyncio.create_task(endpoint._feed_update(bot, update))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
