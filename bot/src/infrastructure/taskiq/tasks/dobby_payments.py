import asyncio
from uuid import UUID

from dishka.integrations.taskiq import FromDishka, inject

from src.infrastructure.dobby.payment_check import PaymentStatusChecker
from src.infrastructure.taskiq.broker import broker


# Fast checks of one new payment: every few seconds at first, then less often (see
# FAST_POLL_SCHEDULE). Each run checks once and schedules the next run, so no database
# session is held while waiting.
@broker.task(retry_on_error=False)
@inject(patch_module=True)
async def watch_payment_task(
    payment_id: UUID, delay: int, checker: FromDishka[PaymentStatusChecker]
) -> None:
    await asyncio.sleep(delay)
    next_delay = await checker.fast_check(payment_id)
    if next_delay is not None:
        await watch_payment_task.kiq(payment_id, next_delay)  # type: ignore[call-overload]


# Every minute: all pending payments, including ones whose fast checks were lost on a
# restart. Gives up on payments older than GIVE_UP_AFTER.
@broker.task(schedule=[{"cron": "* * * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def sweep_pending_payments_task(checker: FromDishka[PaymentStatusChecker]) -> None:
    await checker.sweep()
