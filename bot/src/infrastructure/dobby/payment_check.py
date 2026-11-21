from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional, Protocol
from uuid import UUID

from loguru import logger

from src.application.common.dao import TransactionDao
from src.application.dto import TransactionDto
from src.application.use_cases.gateways.commands.payment import ProcessPayment, ProcessPaymentDto
from src.application.use_cases.gateways.queries.providers import GetPaymentGatewayInstance
from src.core.enums import TransactionStatus
from src.core.utils.time import datetime_now

# Fast checks right after a payment is created (seconds since creation -> delay).
# The per-minute sweep takes over after that and gives up at GIVE_UP_AFTER.
FAST_POLL_SCHEDULE: tuple[tuple[int, int], ...] = ((180, 4), (600, 15))
GIVE_UP_AFTER = timedelta(minutes=30)
# A canceled payment can still turn out paid (the user paid after we gave up). The user's own
# check ("I paid", return to the bot) looks this far back.
LATE_PAYMENT_WINDOW = timedelta(hours=24)


def next_fast_poll_delay(age_seconds: float) -> Optional[int]:
    """Delay before the next fast check, or None when the fast phase is over."""
    for until, delay in FAST_POLL_SCHEDULE:
        if age_seconds < until:
            return delay
    return None


# A user may press "I paid" as often as they like; the gateway is asked at most once per
# payment in this window, repeated presses reuse the last answer.
USER_CHECK_INTERVAL_SECONDS = 5
_PENDING = "PENDING"


class CheckThrottle(Protocol):
    async def recent(self, payment_id: UUID) -> Optional[str]: ...

    async def remember(self, payment_id: UUID, status: str, ttl_seconds: int) -> None: ...


class NoThrottle(CheckThrottle):
    async def recent(self, payment_id: UUID) -> Optional[str]:
        return None

    async def remember(self, payment_id: UUID, status: str, ttl_seconds: int) -> None:
        return None


class NotYourPaymentError(Exception):
    """The payment does not exist or belongs to another user."""


@dataclass
class UserCheckResult:
    completed: list[UUID] = field(default_factory=list)
    pending: list[UUID] = field(default_factory=list)
    canceled: list[UUID] = field(default_factory=list)


class PaymentStatusChecker:
    """The single path that confirms payments without inbound notifications.

    Return to the bot, the "I paid" button, the background checks and gateway notifications
    all end here: ask the gateway API for the status, then let ProcessPayment apply it.
    ProcessPayment moves the transaction with a conditional UPDATE, so however many of these
    race, a payment activates or extends the subscription once.
    """

    def __init__(
        self,
        transaction_dao: TransactionDao,
        get_payment_gateway_instance: GetPaymentGatewayInstance,
        process_payment: ProcessPayment,
        throttle: Optional[CheckThrottle] = None,
        now: Callable[[], datetime] = datetime_now,
    ) -> None:
        self.transaction_dao = transaction_dao
        self.get_payment_gateway_instance = get_payment_gateway_instance
        self.process_payment = process_payment
        self.throttle = throttle or NoThrottle()
        self.now = now

    async def check(
        self, transaction: TransactionDto, give_up: bool = False
    ) -> Optional[TransactionStatus]:
        """Applies the gateway's status. Returns it, or None while the payment is pending."""
        gateway = await self.get_payment_gateway_instance.system(transaction.gateway_type)
        if not gateway.supports_status_check:
            return None

        status = await gateway.fetch_transaction_status(transaction.payment_id)

        if status is None and give_up and transaction.status == TransactionStatus.PENDING:
            logger.info(f"Giving up on pending payment '{transaction.payment_id}'")
            status = TransactionStatus.CANCELED

        if status is None or status == transaction.status:
            return status

        await self.process_payment.system(
            ProcessPaymentDto(
                payment_id=transaction.payment_id,
                new_transaction_status=status,
                gateway_type=transaction.gateway_type,
            )
        )
        return status

    async def check_payment(self, payment_id: UUID) -> Optional[TransactionStatus]:
        transaction = await self.transaction_dao.get_by_payment_id(payment_id)
        if not transaction or transaction.status != TransactionStatus.PENDING:
            return transaction.status if transaction else None
        return await self.check(transaction)

    async def fast_check(self, payment_id: UUID) -> Optional[int]:
        """One fast-phase check. Returns the delay before the next one, or None to stop."""
        transaction = await self.transaction_dao.get_by_payment_id(payment_id)
        if not transaction or transaction.status != TransactionStatus.PENDING:
            return None
        try:
            if await self.check(transaction) is not None:
                return None
        except Exception as e:
            # A dropped connection must not end the checks: try again on the next step.
            logger.warning(f"Failed to check payment '{payment_id}': {e}")
        if not transaction.created_at:
            return None
        return next_fast_poll_delay((self.now() - transaction.created_at).total_seconds())

    async def check_user(self, user_id: int, payment_id: Optional[UUID] = None) -> UserCheckResult:
        """The user says they paid ("I paid" button, return from the payment page).

        Pressing it never activates anything by itself: only a status the gateway API reports
        does. With payment_id, only that payment is checked, and only if it is the user's own.
        Without it, the user's pending and recently given-up payments are checked. Each payment
        is asked about at most once per USER_CHECK_INTERVAL_SECONDS.
        """
        result = UserCheckResult()
        for transaction in await self._user_transactions(user_id, payment_id):
            if transaction.status == TransactionStatus.COMPLETED:
                result.completed.append(transaction.payment_id)
                continue

            status = await self._throttled_check(transaction)
            if status == TransactionStatus.COMPLETED:
                result.completed.append(transaction.payment_id)
            elif status is None:
                result.pending.append(transaction.payment_id)
            elif transaction.status == TransactionStatus.PENDING or payment_id is not None:
                result.canceled.append(transaction.payment_id)

        return result

    async def _user_transactions(
        self, user_id: int, payment_id: Optional[UUID]
    ) -> list[TransactionDto]:
        """The user's own payments worth checking; the given one only if it is theirs."""
        if payment_id is not None:
            transaction = await self.transaction_dao.get_by_payment_id(payment_id)
            if not transaction or transaction.user_id != user_id:
                raise NotYourPaymentError(str(payment_id))
            candidates = [transaction]
            wanted: tuple[TransactionStatus, ...] = (
                TransactionStatus.PENDING,
                TransactionStatus.CANCELED,
                TransactionStatus.COMPLETED,
            )
        else:
            candidates = await self.transaction_dao.get_by_user(user_id)
            wanted = (TransactionStatus.PENDING, TransactionStatus.CANCELED)

        threshold = self.now() - LATE_PAYMENT_WINDOW
        return [
            t
            for t in candidates
            if not t.is_test and t.created_at and t.created_at >= threshold and t.status in wanted
        ]

    async def _throttled_check(self, transaction: TransactionDto) -> Optional[TransactionStatus]:
        cached = await self.throttle.recent(transaction.payment_id)
        if cached is not None:
            return None if cached == _PENDING else TransactionStatus(cached)

        status = await self.check(transaction)
        await self.throttle.remember(
            transaction.payment_id,
            status.value if status else _PENDING,
            USER_CHECK_INTERVAL_SECONDS,
        )
        return status

    async def sweep(self) -> None:
        """Every minute: check pending payments, give up on those older than GIVE_UP_AFTER."""
        now = self.now()
        for transaction in await self.transaction_dao.get_by_status(TransactionStatus.PENDING):
            if transaction.is_test or not transaction.created_at:
                continue
            give_up = now - transaction.created_at >= GIVE_UP_AFTER
            try:
                await self.check(transaction, give_up=give_up)
            except Exception as e:
                logger.warning(f"Failed to check payment '{transaction.payment_id}': {e}")
