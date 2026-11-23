from typing import Optional
from uuid import UUID

from dishka import Provider, Scope, provide
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.common import EventPublisher, Remnawave
from src.application.common.dao import SubscriptionDao, TransactionDao, UserDao
from src.application.common.payment_watcher import PaymentWatcher
from src.application.common.referral_bonus import ReferralFriendBonus
from src.application.use_cases.gateways.commands.payment import ProcessPayment
from src.application.use_cases.gateways.queries.providers import GetPaymentGatewayInstance

from .admin import AdminSummaryService, ClientViewStore, RedisClientViewStore
from .backend import DobbyBackend, ExportTrigger
from .config import DobbyConfig
from .downloads import AndroidDownloadLink
from .export_job import RedisExportState, UserExportJob
from .exporter import FileUserExporter, UserExporter
from .links import SubscriptionLinkProvider, TemplateLinkProvider
from .payment_check import FAST_POLL_SCHEDULE, CheckThrottle, PaymentStatusChecker
from .reminders import RedisReminderLog, RenewalReminders, load_reminder_rows
from .snapshot import load_snapshot


class TaskiqExportTrigger(ExportTrigger):
    async def request(self) -> None:
        # Imported here: the task module imports the broker, which loads the whole config.
        from src.infrastructure.taskiq.tasks.dobby_export import (  # noqa: PLC0415
            export_users_after_change_task,
        )

        await export_users_after_change_task.kiq()  # type: ignore[call-overload]


class RedisCheckThrottle(CheckThrottle):
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def recent(self, payment_id: UUID) -> Optional[str]:
        value = await self.redis.get(f"dobby:paycheck:{payment_id}")
        return value.decode() if isinstance(value, bytes) else value

    async def remember(self, payment_id: UUID, status: str, ttl_seconds: int) -> None:
        await self.redis.set(f"dobby:paycheck:{payment_id}", status, ex=ttl_seconds)


class TaskiqPaymentWatcher(PaymentWatcher):
    async def watch(self, payment_id: UUID) -> None:
        from src.infrastructure.taskiq.tasks.dobby_payments import (  # noqa: PLC0415
            watch_payment_task,
        )

        first_delay = FAST_POLL_SCHEDULE[0][1]
        await watch_payment_task.kiq(payment_id, first_delay)  # type: ignore[call-overload]


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

    @provide
    def get_android_download_link(self, config: DobbyConfig) -> AndroidDownloadLink:
        return AndroidDownloadLink(config.releases_repository, config.download_url_android)

    @provide
    def get_client_view_store(self, redis: Redis) -> ClientViewStore:
        return RedisClientViewStore(redis)

    @provide(scope=Scope.REQUEST)
    def get_admin_summary(self, session: AsyncSession, config: DobbyConfig) -> AdminSummaryService:
        return AdminSummaryService(session, config.timezone)

    @provide
    def get_referral_friend_bonus(self, config: DobbyConfig) -> ReferralFriendBonus:
        return ReferralFriendBonus(days=config.referral_friend_bonus_days)

    @provide
    def get_payment_watcher(self) -> PaymentWatcher:
        return TaskiqPaymentWatcher()

    @provide(scope=Scope.REQUEST)
    def get_payment_checker(
        self,
        transaction_dao: TransactionDao,
        get_payment_gateway_instance: GetPaymentGatewayInstance,
        process_payment: ProcessPayment,
        redis: Redis,
    ) -> PaymentStatusChecker:
        return PaymentStatusChecker(
            transaction_dao,
            get_payment_gateway_instance,
            process_payment,
            throttle=RedisCheckThrottle(redis),
        )

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
    def get_renewal_reminders(
        self,
        session: AsyncSession,
        user_dao: UserDao,
        event_publisher: EventPublisher,
        redis: Redis,
        config: DobbyConfig,
    ) -> RenewalReminders:
        return RenewalReminders(
            load_rows=lambda now, look_ahead: load_reminder_rows(session, now, look_ahead),
            user_dao=user_dao,
            event_publisher=event_publisher,
            log=RedisReminderLog(redis),
            hours_before=config.reminder_hours_before,
        )

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
