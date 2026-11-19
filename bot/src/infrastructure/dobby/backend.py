from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Protocol, Union
from uuid import UUID, uuid4

from loguru import logger
from packaging.version import Version
from remnapy.enums import TrafficLimitStrategy

from src.application.common import Remnawave
from src.application.common.dao import SubscriptionDao, UserDao
from src.application.common.remnawave import T
from src.application.dto import (
    PlanSnapshotDto,
    RemnaSubscriptionDto,
    SquadInfoDto,
    SubscriptionDto,
    UserDto,
)
from src.core.constants import REMNAWAVE_MIN_VERSION
from src.core.enums import SubscriptionStatus
from src.core.utils.converters import days_to_datetime, gb_to_bytes
from src.core.utils.time import datetime_now

from .links import SubscriptionLinkProvider


class ExportTrigger(Protocol):
    async def request(self) -> None: ...


@dataclass(kw_only=True)
class BackendUser:
    """What remnashop reads from a panel user, filled from the bot's own data.

    Traffic, devices, squads and node fields stay empty: the config-delivery service owns them.
    """

    uuid: UUID
    status: SubscriptionStatus
    expire_at: datetime
    subscription_url: str
    telegram_id: Optional[int] = None
    username: str = ""
    email: Optional[str] = None
    tag: Optional[str] = None
    traffic_limit_bytes: int = 0
    traffic_limit_strategy: TrafficLimitStrategy = TrafficLimitStrategy.NO_RESET
    hwid_device_limit: Optional[int] = None
    active_internal_squads: list[Any] = field(default_factory=list)
    external_squad_uuid: Optional[UUID] = None
    used_traffic_bytes: int = 0
    lifetime_used_traffic_bytes: int = 0
    online_at: Optional[datetime] = None
    first_connected_at: Optional[datetime] = None
    last_connected_node_uuid: Optional[UUID] = None


# The protocol is typed with remnapy's UserResponseDto. BackendUser has the fields remnashop reads
# from it, so methods returning BackendUser carry "type: ignore[override]".
class DobbyBackend(Remnawave):
    """Drop-in replacement for the Remnawave panel client.

    remnashop already stores every subscription in its own database, so the panel calls only
    have to return the values the bot would have got back (id, status, expiry, link) and then
    trigger an export of the full user list. The export reads the database after the caller's
    commit; the scheduled export catches anything an early run missed.
    """

    def __init__(
        self,
        user_dao: UserDao,
        subscription_dao: SubscriptionDao,
        links: SubscriptionLinkProvider,
        export_trigger: ExportTrigger,
    ) -> None:
        self.user_dao = user_dao
        self.subscription_dao = subscription_dao
        self.links = links
        self.export_trigger = export_trigger

    async def try_connection(self) -> Version:
        # No panel to connect to. Report the minimum supported version so startup checks pass.
        return REMNAWAVE_MIN_VERSION

    async def create_user(  # type: ignore[override]
        self,
        user: UserDto,
        plan: Optional[PlanSnapshotDto] = None,
        subscription: Optional[SubscriptionDto] = None,
    ) -> BackendUser:
        uuid = subscription.user_remna_id if subscription else await self._login_for(user)
        result = self._build(user, uuid, plan, subscription, default_days=3650)
        await self._changed()
        logger.info(f"Backend user '{uuid}' created for user '{user.id}'")
        return result

    async def update_user(  # type: ignore[override]
        self,
        user: UserDto,
        uuid: UUID,
        plan: Optional[PlanSnapshotDto] = None,
        subscription: Optional[SubscriptionDto] = None,
        reset_traffic: bool = False,
    ) -> BackendUser:
        if not plan and not subscription:
            raise ValueError("Either 'plan' or 'subscription' must be provided")
        result = self._build(user, uuid, plan, subscription)
        await self._changed()
        logger.info(f"Backend user '{uuid}' updated for user '{user.id}'")
        return result

    async def enable_user(self, uuid: UUID) -> None:
        await self._changed()

    async def disable_user(self, uuid: UUID) -> None:
        await self._changed()

    async def delete_user(self, uuid: UUID) -> bool:
        await self._changed()
        return True

    async def get_user_by_uuid(self, uuid: UUID) -> Optional[BackendUser]:  # type: ignore[override]
        # A login is shared by all subscriptions of a user; prefer the current one.
        subscription = await self.subscription_dao.get_by_remna_id(uuid)
        if not subscription:
            return None
        user = await self.user_dao.get_by_id(subscription.user_id)
        if user:
            current = await self.subscription_dao.get_current(user.id)
            if current and current.user_remna_id == uuid:
                subscription = current
        return self._from_subscription(subscription, user)

    async def get_users_by_telegram_id(self, telegram_id: int) -> list[BackendUser]:  # type: ignore[override]
        user = await self.user_dao.get_by_telegram_id(telegram_id)
        if not user:
            return []
        subscription = await self.subscription_dao.get_current(user.id)
        return [self._from_subscription(subscription, user)] if subscription else []

    async def get_users_by_email(self, email: str) -> list[BackendUser]:  # type: ignore[override]
        user = await self.user_dao.get_by_email(email)
        if not user:
            return []
        subscription = await self.subscription_dao.get_current(user.id)
        return [self._from_subscription(subscription, user)] if subscription else []

    async def get_all_users(self, limit: int, offset: int) -> list[BackendUser]:  # type: ignore[override]
        # Only used to import users from a panel. There is no panel.
        return []

    # Devices, connections, traffic and squads belong to the config-delivery service.

    async def get_devices(self, uuid: UUID) -> list[Any]:
        return []

    async def delete_device(self, user_uuid: UUID, hwid: str) -> Optional[int]:
        return 0

    async def delete_all_devices(self, user_uuid: UUID) -> None:
        return None

    async def drop_connections(self, user_uuid: UUID) -> None:
        return None

    async def reset_traffic(self, uuid: UUID) -> Optional[BackendUser]:  # type: ignore[override]
        return None

    async def revoke_subscription(self, uuid: UUID) -> None:
        # Link rotation depends on the config-delivery service. Not supported yet.
        logger.warning(f"Revoking subscription link '{uuid}' is not supported yet")

    async def get_squads_available(self) -> bool:
        return False

    async def get_internal_squads(self) -> list[SquadInfoDto]:
        return []

    async def get_external_squads(self) -> list[SquadInfoDto]:
        return []

    def apply_sync(self, target: T, source: Union[SubscriptionDto, RemnaSubscriptionDto]) -> T:
        if not is_dataclass(target) or not is_dataclass(source):
            raise TypeError("Both target and source must be dataclasses")
        source_fields = {f.name for f in fields(source)}
        target_fields = {f.name for f in fields(target)}
        if "user_remna_id" in target_fields and "uuid" in source_fields:
            setattr(target, "user_remna_id", getattr(source, "uuid"))
        for name in target_fields & source_fields:
            setattr(target, name, getattr(source, name))
        return target

    async def _login_for(self, user: UserDto) -> UUID:
        """The user's permanent login: the id of their first subscription, or a new one.

        Every later subscription of the user (new purchase after expiry, purchase after an
        admin deleted the subscription, promo, admin-assigned plan) reuses it, so the
        subscription link never changes.
        """
        history = await self.subscription_dao.get_all_by_user(user.id)
        if history:
            first = min(history, key=lambda s: (s.created_at is None, s.created_at, s.id))
            return first.user_remna_id
        return uuid4()

    async def _changed(self) -> None:
        try:
            await self.export_trigger.request()
        except Exception as e:
            # The scheduled export will pick the change up.
            logger.warning(f"Failed to trigger user export: {e}")

    def _build(
        self,
        user: UserDto,
        uuid: UUID,
        plan: Optional[PlanSnapshotDto],
        subscription: Optional[SubscriptionDto],
        default_days: Optional[int] = None,
    ) -> BackendUser:
        if subscription:
            status = (
                SubscriptionStatus.DISABLED
                if subscription.status == SubscriptionStatus.DISABLED
                else SubscriptionStatus.ACTIVE
            )
            return BackendUser(
                uuid=uuid,
                status=status,
                expire_at=subscription.expire_at,
                subscription_url=self.links.url_for(uuid),
                telegram_id=user.telegram_id,
                username=user.remna_name,
                email=user.email,
                tag=subscription.tag,
                traffic_limit_bytes=gb_to_bytes(subscription.traffic_limit),
                traffic_limit_strategy=subscription.traffic_limit_strategy,
                hwid_device_limit=subscription.device_limit,
            )
        if plan:
            return BackendUser(
                uuid=uuid,
                status=SubscriptionStatus.ACTIVE,
                expire_at=days_to_datetime(plan.duration),
                subscription_url=self.links.url_for(uuid),
                telegram_id=user.telegram_id,
                username=user.remna_name,
                email=user.email,
                tag=plan.tag,
                traffic_limit_bytes=gb_to_bytes(plan.traffic_limit),
                traffic_limit_strategy=plan.traffic_limit_strategy,
                hwid_device_limit=plan.device_limit,
            )
        return BackendUser(
            uuid=uuid,
            status=SubscriptionStatus.ACTIVE,
            expire_at=datetime_now() + timedelta(days=default_days or 0),
            subscription_url=self.links.url_for(uuid),
            telegram_id=user.telegram_id,
            username=user.remna_name,
            email=user.email,
        )

    def _from_subscription(
        self, subscription: SubscriptionDto, user: Optional[UserDto]
    ) -> BackendUser:
        return BackendUser(
            uuid=subscription.user_remna_id,
            status=subscription.status,
            expire_at=subscription.expire_at,
            subscription_url=self.links.url_for(subscription.user_remna_id),
            telegram_id=user.telegram_id if user else None,
            username=user.remna_name if user else "",
            email=user.email if user else None,
            tag=subscription.tag,
            traffic_limit_bytes=gb_to_bytes(subscription.traffic_limit),
            traffic_limit_strategy=subscription.traffic_limit_strategy,
            hwid_device_limit=subscription.device_limit,
        )
