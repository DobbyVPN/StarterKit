from typing import Callable
from uuid import UUID

import httpx
import orjson
import pytest
from starlette.requests import Request

from src.core.enums import TransactionStatus
from src.infrastructure.payment_gateways.yookassa import YookassaGateway

PAYMENT_ID = UUID("2f1a2b3c-000f-5000-8000-1a2b3c4d5e6f")


def make_gateway(handler: Callable[[httpx.Request], httpx.Response]) -> YookassaGateway:
    gateway = YookassaGateway.__new__(YookassaGateway)
    gateway._client = httpx.AsyncClient(
        base_url=YookassaGateway.API_BASE, transport=httpx.MockTransport(handler)
    )
    return gateway


def notification(status: str, payment_id: UUID = PAYMENT_ID) -> Request:
    body = orjson.dumps(
        {
            "type": "notification",
            "event": f"payment.{status}",
            "object": {"id": str(payment_id), "status": status},
        }
    )

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        # Spoofed sender IP: must not matter any more.
        "headers": [(b"cf-connecting-ip", b"185.71.76.1")],
    }
    return Request(scope, receive)


def api_says(status: str) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/v3/payments/{PAYMENT_ID}"
        return httpx.Response(200, json={"id": str(PAYMENT_ID), "status": status})

    return handler


async def test_status_comes_from_the_api_not_the_notification() -> None:
    # A forged "succeeded" notification for a payment the API still reports as pending.
    gateway = make_gateway(api_says("pending"))
    assert await gateway.handle_webhook(notification("succeeded")) is None


async def test_succeeded_payment_completes_the_transaction() -> None:
    gateway = make_gateway(api_says("succeeded"))
    assert await gateway.handle_webhook(notification("succeeded")) == (
        PAYMENT_ID,
        TransactionStatus.COMPLETED,
    )


async def test_canceled_payment_cancels_the_transaction() -> None:
    gateway = make_gateway(api_says("canceled"))
    assert await gateway.handle_webhook(notification("canceled")) == (
        PAYMENT_ID,
        TransactionStatus.CANCELED,
    )


async def test_unknown_payment_is_rejected() -> None:
    gateway = make_gateway(lambda request: httpx.Response(404, json={"type": "error"}))
    with pytest.raises(PermissionError):
        await gateway.handle_webhook(notification("succeeded"))
