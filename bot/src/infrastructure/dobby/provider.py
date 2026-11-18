from dishka import Provider, Scope, provide
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.common import Remnawave
from src.application.common.dao import SubscriptionDao, UserDao

from .backend import DobbyBackend, ExportTrigger
from .config import DobbyConfig
from .export_job import RedisExportState, UserExportJob
from .exporter import FileUserExporter, UserExporter
from .links import SubscriptionLinkProvider, TemplateLinkProvider
from .snapshot import load_snapshot


class TaskiqExportTrigger(ExportTrigger):
    async def request(self) -> None:
        # Imported here: the task module imports the broker, which loads the whole config.
        from src.infrastructure.taskiq.tasks.dobby_export import (  # noqa: PLC0415
            export_users_after_change_task,
        )

        await export_users_after_change_task.kiq()  # type: ignore[call-overload]


class DobbyProvider(Provider):
    scope = Scope.APP

    @provide
    def get_config(self) -> DobbyConfig:
        return DobbyConfig()

    @provide
    def get_links(self, config: DobbyConfig) -> SubscriptionLinkProvider:
        return TemplateLinkProvider(config.subscription_url_template)

    @provide
    def get_exporter(self, config: DobbyConfig) -> UserExporter:
        # Replace with the sftp exporter once the target and format are agreed.
        return FileUserExporter(config.export_path)

    @provide
    def get_export_trigger(self) -> ExportTrigger:
        return TaskiqExportTrigger()

    @provide(scope=Scope.REQUEST)
    def get_backend(
        self,
        user_dao: UserDao,
        subscription_dao: SubscriptionDao,
        links: SubscriptionLinkProvider,
        export_trigger: ExportTrigger,
    ) -> Remnawave:
        return DobbyBackend(user_dao, subscription_dao, links, export_trigger)

    @provide(scope=Scope.REQUEST)
    def get_export_job(
        self,
        session: AsyncSession,
        exporter: UserExporter,
        redis: Redis,
        config: DobbyConfig,
    ) -> UserExportJob:
        return UserExportJob(
            load_users=lambda: load_snapshot(session),
            exporter=exporter,
            state=RedisExportState(redis),
            force_interval_seconds=config.force_export_interval_seconds,
        )
