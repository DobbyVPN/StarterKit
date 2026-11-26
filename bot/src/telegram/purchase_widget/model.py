"""The subscription widget: one message for choosing a term, paying and the result.

The widget draws whatever its chat state says. The bot writes that state when it sends the
widget, answers each widget query with the new state, and also stores it with
editMessageWidgetData, so a widget that is drawn again later shows the same thing.

This module is pure: it builds the HTML and the state and parses queries. The service does
the work (payments, trial, checks).
"""

import json
import zlib
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from importlib import resources
from typing import Literal, Optional, Union

# Bot API limits for a widget (https://core.telegram.org/bots/widgets#limits).
MAX_HTML_BYTES = 32768
MAX_COMPRESSED_HTML_BYTES = 4096
MAX_DATA_BYTES = 1024
# Plans and terms shown at once: more does not fit the chat state (1024 bytes) or the bubble.
MAX_OFFERS = 5

State = Literal["choose", "wait", "fail", "paid"]

# Texts the widget shows, by key in the HTML; values are translation keys.
TEXT_KEYS: dict[str, str] = {
    "title": "widget-buy.title",
    "pay": "widget-buy.pay",
    "none": "widget-buy.no-plans",
    "once": "widget-buy.one-off",
    "refund": "widget-buy.refund",
    "wait": "widget-buy.wait",
    "waiting": "widget-buy.waiting",
    "open": "widget-buy.open",
    "again": "widget-buy.open-again",
    "other": "widget-buy.other-term",
    "failT": "widget-buy.failed-title",
    "fail": "widget-buy.failed",
    "retry": "widget-buy.retry",
    "until": "widget-buy.until",
    "connect": "widget-buy.connect",
    "renew": "widget-buy.renew",
    "error": "widget-buy.error",
}


@dataclass(frozen=True)
class Offer:
    """One card: a plan with one of its terms."""

    plan_id: int
    days: int
    title: str
    price: str
    per_month: str = ""

    @property
    def key(self) -> str:
        return f"{self.plan_id}:{self.days}"

    def dump(self) -> dict[str, str]:
        data = {"k": self.key, "t": self.title, "p": self.price}
        if self.per_month:
            data["m"] = self.per_month
        return data


@dataclass
class WidgetState:
    state: State = "choose"
    offers: list[Offer] = field(default_factory=list)
    # Label of the free trial button; empty when the user cannot take a trial.
    trial: str = ""
    # Waiting / failed: the chosen offer, its payment and payment page.
    offer: Optional[Offer] = None
    payment_id: Optional[str] = None
    payment_url: Optional[str] = None
    # Paid: "until" date and the length left, already formatted.
    paid_until: str = ""
    paid_left: str = ""

    def public(self) -> str:
        """What the widget sees (chat_data, query answers). No payment id."""
        data: dict[str, object] = {"s": self.state, "o": [o.dump() for o in self.offers]}
        if self.offers:
            data["sel"] = self.offers[len(self.offers) // 2].key
        if self.trial:
            data["tr"] = self.trial
        if self.offer and self.state in ("wait", "fail"):
            data["w"] = {
                "k": self.offer.key,
                "t": self.offer.title,
                "p": self.offer.price,
                "u": self.payment_url or "",
            }
        if self.state == "paid":
            data["pd"] = {"d": self.paid_until, "n": self.paid_left}
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    def dump(self) -> str:
        """What the bot keeps: the public state plus the payment and the chosen offer."""
        offer = self.offer
        return json.dumps(
            {
                "public": self.public(),
                "payment_id": self.payment_id,
                "offer": [offer.plan_id, offer.days, offer.title, offer.price, offer.per_month]
                if offer
                else None,
            },
            ensure_ascii=False,
        )

    @classmethod
    def load(cls, raw: str) -> "WidgetState":
        stored = json.loads(raw)
        public = json.loads(stored["public"])
        offer = Offer(*stored["offer"]) if stored.get("offer") else None
        paid = public.get("pd", {})
        return cls(
            state=public.get("s", "choose"),
            offers=[_offer_from(o) for o in public.get("o", [])],
            trial=public.get("tr", ""),
            offer=offer,
            payment_id=stored.get("payment_id"),
            payment_url=public.get("w", {}).get("u") or None,
            paid_until=paid.get("d", ""),
            paid_left=paid.get("n", ""),
        )


def _offer_from(data: dict[str, str]) -> Offer:
    plan_id, days = data["k"].split(":")
    return Offer(int(plan_id), int(days), data["t"], data["p"], data.get("m", ""))


def fit_offers(state: WidgetState) -> WidgetState:
    """Drops offers from the end until the public state fits the chat state limit."""
    state.offers = state.offers[:MAX_OFFERS]
    while state.offers and len(state.public().encode()) > MAX_DATA_BYTES:
        state.offers = state.offers[:-1]
    return state


# Queries: "check", "choose", "trial", "connect", "buy:<plan>:<days>".


@dataclass(frozen=True)
class Buy:
    plan_id: int
    days: int


Query = Union[Literal["check", "choose", "trial", "connect"], Buy]


def parse_query(data: str) -> Optional[Query]:
    if data in ("check", "choose", "trial", "connect"):
        return data  # type: ignore[return-value]
    parts = data.split(":")
    if len(parts) == 3 and parts[0] == "buy" and parts[1].isdigit() and parts[2].isdigit():
        return Buy(int(parts[1]), int(parts[2]))
    return None


def format_money(amount: Decimal, symbol: str) -> str:
    value = amount.quantize(Decimal(1)) if amount == amount.to_integral() else amount
    return f"{value.normalize():f} {symbol}"


def per_month(amount: Decimal, days: int) -> Optional[Decimal]:
    """Price per month for terms of two months and longer."""
    if days < 60 or amount <= 0:
        return None
    return (amount * 30 / days).quantize(Decimal(1), rounding=ROUND_HALF_UP)


def render_html(texts: dict[str, str]) -> str:
    template = resources.files(__package__).joinpath("widget.html").read_text(encoding="utf-8")
    payload = json.dumps(texts, ensure_ascii=False).replace("</", "<\\/")
    return template.replace("__TEXTS__", payload)


def compressed_size(html: str) -> int:
    return len(zlib.compress(html.encode(), 9))


def check_html_size(html: str) -> None:
    size = len(html.encode())
    packed = compressed_size(html)
    if size > MAX_HTML_BYTES or packed > MAX_COMPRESSED_HTML_BYTES:
        raise ValueError(f"Widget HTML too large: {size} bytes, {packed} compressed")
