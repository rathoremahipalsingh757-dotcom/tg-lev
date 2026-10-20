"""Asynchronous client for the Paymento merchant API."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx


class PaymentoError(Exception):
    """Paymento returned a definite error response."""


class PaymentoTransportError(Exception):
    """The request outcome is unknown or the response was invalid."""


class PaymentoClient:
    BASE_URL = "https://api.paymento.io/v1/payment/"
    CHECKOUT_URL = "https://app.paymento.io/gateway"

    def __init__(self, api_key: str, timeout_seconds: float = 15.0) -> None:
        self._api_key = api_key
        self._timeout = timeout_seconds

    async def _post(
        self, endpoint: str, payload: dict[str, Any], accept: str
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self.BASE_URL}{endpoint}",
                    json=payload,
                    headers={
                        "Api-key": self._api_key,
                        "Accept": accept,
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                return response
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                raise PaymentoError(
                    f"Paymento rejected {endpoint} with HTTP 4xx."
                ) from exc
            raise PaymentoTransportError(
                f"Paymento request {endpoint} failed or timed out."
            ) from exc
        except httpx.RequestError as exc:
            raise PaymentoTransportError(
                f"Paymento request {endpoint} failed or timed out."
            ) from exc

    async def create_payment(
        self, order_id: str, amount_usd: str, return_url: str
    ) -> dict[str, str]:
        response = await self._post(
            "request",
            {
                "fiatAmount": amount_usd,
                "fiatCurrency": "USD",
                "ReturnUrl": return_url,
                "orderId": order_id,
            },
            "text/plain",
        )
        try:
            result = response.json()
        except ValueError:
            result = None

        if isinstance(result, dict):
            if result.get("success") is False:
                raise PaymentoError("Paymento could not create the payment request.")
            if result.get("success") is not True:
                raise PaymentoTransportError(
                    "Paymento returned an invalid payment request response."
                )
            token = result.get("body")
        else:
            token = response.text.strip()

        if not isinstance(token, str) or not token:
            raise PaymentoTransportError(
                "Paymento returned an incomplete payment request."
            )
        return {
            "token": token,
            "payment_url": f"{self.CHECKOUT_URL}?{urlencode({'token': token})}",
        }

    async def verify_payment(self, token: str) -> dict[str, Any]:
        response = await self._post(
            "verify", {"token": token}, "application/json"
        )
        try:
            result = response.json()
        except ValueError as exc:
            raise PaymentoTransportError(
                "Paymento returned an invalid verification response."
            ) from exc

        if not isinstance(result, dict):
            raise PaymentoTransportError(
                "Paymento returned an invalid verification response."
            )
        body = result.get("body")
        if not isinstance(body, dict):
            message = result.get("message")
            if message == "Invalid Token":
                raise PaymentoError("Paymento does not recognize this payment token.")
            raise PaymentoTransportError(
                "Paymento returned no payment details during verification."
            )
        return body

    async def configure_ipn(self, ipn_url: str) -> None:
        response = await self._post(
            "settings",
            {"IPN_Url": ipn_url, "IPN_Method": 1},
            "text/plain",
        )
        try:
            result = response.json()
        except ValueError:
            result = None
        if not isinstance(result, dict):
            raise PaymentoTransportError(
                "Paymento returned an invalid IPN settings response."
            )
        if result.get("success") is not True:
            raise PaymentoError("Paymento did not accept the IPN settings.")
