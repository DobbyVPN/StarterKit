import asyncio
import uuid
from decimal import Decimal
from typing import Any, Final, Optional, Union
from uuid import UUID

import orjson
from aiogram import Bot
from fastapi import Request
from httpx import AsyncClient, ConnectError, HTTPStatusError
from loguru import logger

from src.application.dto import (
    PaymentGatewayDto,
    PaymentResultDto,
)
from src.application.dto.payment_gateway import YooKassaGatewaySettingsDto
from src.core.config import AppConfig
from src.core.enums import Deeplink, TransactionStatus

from .base import BasePaymentGateway


# https://yookassa.ru/developers/
class YookassaGateway(BasePaymentGateway):
    _client: AsyncClient

    API_BASE: Final[str] = "https://api.yookassa.ru"
    PAYMENT_SUBJECT: Final[str] = "service"
    PAYMENT_MODE: Final[str] = "full_payment"

    CONNECT_RETRIES: Final[int] = 3
    CONNECT_RETRY_DELAY: Final[float] = 1.0

    NETWORKS = [
        "77.75.153.0/25",
        "77.75.156.11",
        "77.75.156.35",
        "77.75.154.128/25",
        "185.71.76.0/27",
        "185.71.77.0/27",
        "2a02:5180:0:1509::/64",
        "2a02:5180:0:2655::/64",
        "2a02:5180:0:1533::/64",
        "2a02:5180:0:2669::/64",
    ]

    def __init__(self, gateway: PaymentGatewayDto, bot: Bot, config: AppConfig) -> None:
        super().__init__(gateway, bot, config)

        if not isinstance(self.data.settings, YooKassaGatewaySettingsDto):
            raise TypeError(
                f"Invalid settings type: expected {YooKassaGatewaySettingsDto.__name__}, "
                f"got {type(self.data.settings).__name__}"
            )

        self._client = self._make_client(
            base_url=self.API_BASE,
            auth=(
                self.data.settings.shop_id,
                self.data.settings.api_key.get_secret_value(),  # type: ignore [arg-type, union-attr]
            ),
        )

    async def handle_create_payment(self, amount: Decimal, details: str) -> PaymentResultDto:
        payload = await self._create_payment_payload(str(amount), details)
        headers = {"Idempotence-Key": str(uuid.uuid4())}
        logger.debug(f"Creating payment payload: {payload}")

        last_connect_error: ConnectError | None = None

        for attempt in range(1, self.CONNECT_RETRIES + 1):
            try:
                response = await self._client.post("v3/payments", json=payload, headers=headers)
                response.raise_for_status()
                data = orjson.loads(response.content)
                return self._get_payment_data(data)

            except ConnectError as e:
                last_connect_error = e
                logger.warning(
                    f"ConnectError on attempt {attempt}/{self.CONNECT_RETRIES} "
                    f"while creating payment: {e}"
                )
                if attempt < self.CONNECT_RETRIES:
                    await asyncio.sleep(self.CONNECT_RETRY_DELAY * attempt)

            except HTTPStatusError as e:
                logger.error(
                    f"HTTP error creating payment. "
                    f"Status: '{e.response.status_code}', Body: {e.response.text}"
                )
                raise
            except (KeyError, orjson.JSONDecodeError) as e:
                logger.error(f"Failed to parse response. Error: {e}")
                raise
            except Exception as e:
                logger.exception(f"An unexpected error occurred while creating payment: {e}")
                raise

        logger.error(
            f"All {self.CONNECT_RETRIES} attempts to connect to YooKassa failed. "
            f"Last error: {last_connect_error}"
        )
        raise last_connect_error  # type: ignore[misc]

    # Payments are confirmed by asking the API (return to the bot, "I paid" button, background
    # check), so the shop works without a public address. Notifications are optional.
    supports_status_check = True

    async def handle_webhook(self, request: Request) -> Union[tuple[UUID, TransactionStatus], None]:
        logger.debug("Received YooKassa webhook request")

        # The notification only tells us which payment to look at. Its status is read back from
        # the YooKassa API with the shop's credentials, so a forged notification (the sender IP
        # header can be spoofed) cannot mark a payment as paid.
        webhook_data = await self._get_webhook_data(request)
        payment_object: dict = webhook_data.get("object", {})
        payment_id_str = payment_object.get("id")

        if not payment_id_str:
            raise ValueError("Required field 'id' is missing")

        payment_id = UUID(payment_id_str)
        status = await self.fetch_transaction_status(payment_id)
        return (payment_id, status) if status else None

    async def _get_payment(self, payment_id: UUID) -> dict[str, Any]:
        response = await self._client.get(f"v3/payments/{payment_id}")
        if response.status_code == 404:
            raise PermissionError(f"Payment '{payment_id}' not found in this shop")
        response.raise_for_status()
        data = orjson.loads(response.content)

        if not isinstance(data, dict) or data.get("id") != str(payment_id):
            raise PermissionError(f"YooKassa returned a different payment for '{payment_id}'")
        return data

    async def fetch_pending_payment_url(self, payment_id: UUID) -> Optional[str]:
        data = await self._get_payment(payment_id)
        if data.get("status") != "pending":
            return None
        url = data.get("confirmation", {}).get("confirmation_url")
        return str(url) if url else None

    async def fetch_transaction_status(self, payment_id: UUID) -> Optional[TransactionStatus]:
        data = await self._get_payment(payment_id)

        match data.get("status"):
            case "succeeded":
                return TransactionStatus.COMPLETED
            case "canceled":
                return TransactionStatus.CANCELED
            case status:
                logger.debug(f"YooKassa payment '{payment_id}' is '{status}'")
                return None

    async def _create_payment_payload(self, amount: str, details: str) -> dict[str, Any]:
        return_url = f"{await self._get_bot_redirect_url()}?start={Deeplink.PAYMENT_RETURN}"
        return {
            "amount": {"value": amount, "currency": self.data.currency},
            "confirmation": {
                "type": "redirect",
                # Back to the bot, which checks the payment right away.
                "return_url": return_url,
            },
            "capture": True,
            "description": details,
            "receipt": {
                "customer": {"email": self.data.settings.customer},  # type: ignore[union-attr]
                "items": [
                    {
                        "description": details,
                        "quantity": "1.00",
                        "amount": {"value": amount, "currency": self.data.currency},
                        "vat_code": self.data.settings.vat_code,  # type: ignore[union-attr]
                        "payment_subject": self.PAYMENT_SUBJECT,
                        "payment_mode": self.PAYMENT_MODE,
                    }
                ],
            },
        }

    def _get_payment_data(self, data: dict[str, Any]) -> PaymentResultDto:
        payment_id_str = data.get("id")

        if not payment_id_str:
            raise KeyError("Invalid response from YooKassa API: missing 'id'")

        confirmation: dict = data.get("confirmation", {})
        payment_url = confirmation.get("confirmation_url")

        if not payment_url:
            raise KeyError("Invalid response from YooKassa API: missing 'confirmation_url'")

        return PaymentResultDto(id=UUID(payment_id_str), url=str(payment_url))
