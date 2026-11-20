import asyncio
from typing import Any, Sequence

from aiogram import Bot
from loguru import logger

from src.web.endpoints.telegram import TelegramWebhookEndpoint

POLL_TIMEOUT_SECONDS = 30
# getUpdates must answer within its own timeout; a request hanging longer than this is dropped
# and retried, so a broken connection cannot stall the bot.
REQUEST_DEADLINE_SECONDS = POLL_TIMEOUT_SECONDS + 15
RETRY_DELAYS_SECONDS = (1, 2, 5, 10, 30)


async def run_polling(
    bot: Bot, endpoint: TelegramWebhookEndpoint, allowed_updates: Sequence[str]
) -> None:
    """Receives Telegram updates with getUpdates: no domain, no inbound connections.

    Updates go through the same handler as the webhook endpoint.
    """
    await bot.delete_webhook(drop_pending_updates=False)
    logger.info("Receiving Telegram updates by long polling")
    tasks: set[asyncio.Task[Any]] = set()
    offset = None
    failures = 0

    while True:
        try:
            updates = await asyncio.wait_for(
                bot.get_updates(
                    offset=offset,
                    timeout=POLL_TIMEOUT_SECONDS,
                    allowed_updates=list(allowed_updates),
                ),
                timeout=REQUEST_DEADLINE_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            delay = RETRY_DELAYS_SECONDS[min(failures, len(RETRY_DELAYS_SECONDS) - 1)]
            failures += 1
            logger.warning(f"Polling failed ({type(e).__name__}: {e}), retrying in {delay}s")
            await asyncio.sleep(delay)
            continue

        failures = 0
        for update in updates:
            offset = update.update_id + 1
            task = asyncio.create_task(endpoint._feed_update(bot, update))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
