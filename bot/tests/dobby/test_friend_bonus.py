from decimal import Decimal
from typing import Any
from uuid import uuid4

from src.application.common.referral_bonus import ReferralFriendBonus
from src.application.dto import PlanSnapshotDto, PriceDetailsDto, TransactionDto, UserDto
from src.application.use_cases.referral.commands.rewards import (
    AssignReferralRewards,
    AssignReferralRewardsDto,
)
from src.core.enums import Currency, PaymentGatewayType, PurchaseType, TransactionStatus

FRIEND = UserDto(id=8, telegram_id=2002, name="Friend")


def tx(
    status: TransactionStatus = TransactionStatus.COMPLETED, amount: str = "199"
) -> TransactionDto:
    return TransactionDto(
        payment_id=uuid4(),
        user_id=FRIEND.id,
        status=status,
        purchase_type=PurchaseType.NEW,
        gateway_type=PaymentGatewayType.YOOKASSA,
        pricing=PriceDetailsDto(
            original_amount=Decimal(amount), discount_percent=0, final_amount=Decimal(amount)
        ),
        currency=Currency.RUB,
        plan_snapshot=PlanSnapshotDto.test(),
    )


class Transactions:
    def __init__(self, items: list[TransactionDto]) -> None:
        self.items = items

    async def get_by_user(self, user_id: int) -> list[TransactionDto]:
        return list(self.items)


class AddDays:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    @property
    def system(self) -> "AddDays":
        return self

    async def __call__(self, data: Any) -> None:
        self.calls.append((data.user_id, data.days))


def make(history: list[TransactionDto], days: int = 30) -> tuple[AssignReferralRewards, AddDays]:
    use_case = AssignReferralRewards.__new__(AssignReferralRewards)
    add = AddDays()
    use_case.transaction_dao = Transactions(history)  # type: ignore[assignment]
    use_case.add_subscription_duration = add  # type: ignore[assignment]
    use_case.friend_bonus = ReferralFriendBonus(days=days)
    return use_case, add


async def test_friend_gets_a_month_on_first_payment() -> None:
    current = tx()
    use_case, add = make([current])
    await use_case._give_friend_bonus(AssignReferralRewardsDto(user=FRIEND, transaction=current))
    assert add.calls == [(FRIEND.id, 30)]


async def test_no_friend_bonus_after_an_earlier_payment() -> None:
    current = tx()
    use_case, add = make([tx(), current])
    await use_case._give_friend_bonus(AssignReferralRewardsDto(user=FRIEND, transaction=current))
    assert add.calls == []


async def test_free_and_unfinished_earlier_transactions_do_not_count() -> None:
    current = tx()
    history = [tx(amount="0"), tx(status=TransactionStatus.CANCELED), current]
    use_case, add = make(history)
    await use_case._give_friend_bonus(AssignReferralRewardsDto(user=FRIEND, transaction=current))
    assert add.calls == [(FRIEND.id, 30)]


async def test_friend_bonus_can_be_turned_off() -> None:
    current = tx()
    use_case, add = make([current], days=0)
    await use_case._give_friend_bonus(AssignReferralRewardsDto(user=FRIEND, transaction=current))
    assert add.calls == []
