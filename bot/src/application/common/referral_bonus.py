from dataclasses import dataclass


@dataclass(frozen=True)
class ReferralFriendBonus:
    """Days the invited friend gets on their first payment (0 = none)."""

    days: int
