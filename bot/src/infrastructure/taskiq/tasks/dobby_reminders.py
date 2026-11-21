from dishka.integrations.taskiq import FromDishka, inject

from src.infrastructure.dobby.reminders import RenewalReminders
from src.infrastructure.taskiq.broker import broker


# Every minute, like the user export, so "subscription expired" goes out in the same minute
# the user drops out of the export file.
@broker.task(schedule=[{"cron": "* * * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def renewal_reminders_task(reminders: FromDishka[RenewalReminders]) -> None:
    await reminders.run()
