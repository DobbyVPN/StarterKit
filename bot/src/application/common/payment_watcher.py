from typing import Protocol
from uuid import UUID


class PaymentWatcher(Protocol):
    """Starts checking a new payment's status with its gateway until it is final."""

    async def watch(self, payment_id: UUID) -> None: ...
