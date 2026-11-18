import asyncio

from dishka.integrations.taskiq import FromDishka, inject

from src.infrastructure.dobby.export_job import UserExportJob
from src.infrastructure.taskiq.broker import broker

# Gives the caller's database transaction time to commit before the export reads it.
AFTER_CHANGE_DELAY_SECONDS = 2


@broker.task()
@inject(patch_module=True)
async def export_users_after_change_task(job: FromDishka[UserExportJob]) -> None:
    await asyncio.sleep(AFTER_CHANGE_DELAY_SECONDS)
    await job.run()


# Every minute: retries failed exports, catches changes made outside the backend adapter, and
# forces a full export once the last one is older than DOBBY_FORCE_EXPORT_INTERVAL_SECONDS.
@broker.task(schedule=[{"cron": "* * * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def export_users_task(job: FromDishka[UserExportJob]) -> None:
    await job.run()
