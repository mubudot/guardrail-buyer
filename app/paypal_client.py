"""PayPal client interface + Mock and Sandbox implementations.

The Sandbox client targets the PayPal Orders v2 REST API:
  POST /v1/oauth2/token               (client_credentials)
  POST /v2/checkout/orders            (intent CAPTURE)
  POST /v2/checkout/orders/{id}/capture
Credentials come from PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET env vars only.
"""
from __future__ import annotations

import os
import time
import uuid
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

import httpx

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"


class PayPalError(RuntimeError):
    pass


class PayPalClient(ABC):
    name: str = "abstract"

    @abstractmethod
    def create_order(self, amount: Decimal, currency: str, description: str,
                     reference_id: str, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Create an order. Returns {'id', 'status', 'approve_url'?, 'raw'}."""

    @abstractmethod
    def capture_order(self, order_id: str) -> dict[str, Any]:
        """Capture an approved order. Returns {'id', 'status', 'capture_id'?, 'raw'}."""


class MockPayPal(PayPalClient):
    """In-memory stand-in that mimics the Orders v2 status machine.

    Mock orders are treated as already payer-approved so they can be captured
    immediately (real Sandbox orders need the buyer to approve first).
    """
    name = "mock"

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}

    def create_order(self, amount, currency, description, reference_id, items=None):
        oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
        self.orders[oid] = {
            "id": oid, "status": "APPROVED", "amount": str(amount), "currency": currency,
            "description": description, "reference_id": reference_id, "items": items or [],
        }
        return {"id": oid, "status": "APPROVED", "approve_url": None, "raw": dict(self.orders[oid])}

    def capture_order(self, order_id):
        order = self.orders.get(order_id)
        if order is None:
            raise PayPalError(f"order {order_id} not found")
        if order["status"] == "COMPLETED":
            raise PayPalError(f"order {order_id} already captured")
        order["status"] = "COMPLETED"
        cap = "MOCKCAP-" + uuid.uuid4().hex[:10].upper()
        order["capture_id"] = cap
        return {"id": order_id, "status": "COMPLETED", "capture_id": cap, "raw": dict(order)}


class SandboxPayPal(PayPalClient):
    """Stub for the real PayPal Sandbox. Untested until step 1c (needs creds)."""
    name = "sandbox"

    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 base_url: str = SANDBOX_BASE, timeout: float = 30.0,
                 return_url: str = "http://localhost:8000/paypal/return",
                 cancel_url: str = "http://localhost:8000/paypal/cancel") -> None:
        self.client_id = client_id or os.environ.get("PAYPAL_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("PAYPAL_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            raise PayPalError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set")
        self.base_url = base_url.rstrip("/")
        self.return_url, self.cancel_url = return_url, cancel_url
        self._http = httpx.Client(timeout=timeout)
        self._token: str | None = None
        self._token_exp = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self._http.post(f"{self.base_url}/v1/oauth2/token",
                            data={"grant_type": "client_credentials"},
                            auth=(self.client_id, self.client_secret),
                            headers={"Accept": "application/json"})
        if r.status_code != 200:
            raise PayPalError(f"OAuth failed: HTTP {r.status_code}")
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 300))
        return self._token

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"}
        if idempotency_key:
            h["PayPal-Request-Id"] = idempotency_key
        return h

    def create_order(self, amount, currency, description, reference_id, items=None):
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference_id,
                "description": description[:127],
                "amount": {"currency_code": currency, "value": f"{Decimal(amount):.2f}"},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "return_url": self.return_url, "cancel_url": self.cancel_url,
                "user_action": "PAY_NOW"}}},
        }
        r = self._http.post(f"{self.base_url}/v2/checkout/orders", json=payload,
                            headers=self._headers(idempotency_key=reference_id))
        if r.status_code not in (200, 201):
            raise PayPalError(f"create_order failed: HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        approve = next((l["href"] for l in body.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)
        return {"id": body["id"], "status": body["status"], "approve_url": approve, "raw": body}

    def capture_order(self, order_id):
        r = self._http.post(f"{self.base_url}/v2/checkout/orders/{order_id}/capture",
                            headers=self._headers(idempotency_key=f"cap-{order_id}"))
        if r.status_code not in (200, 201):
            raise PayPalError(f"capture_order failed: HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        cap_id = None
        try:
            cap_id = body["purchase_units"][0]["payments"]["captures"][0]["id"]
        except (KeyError, IndexError):
            pass
        return {"id": body["id"], "status": body["status"], "capture_id": cap_id, "raw": body}


def make_client(kind: str | None = None) -> PayPalClient:
    kind = (kind or os.environ.get("PAYPAL_MODE", "mock")).lower()
    if kind == "sandbox":
        return SandboxPayPal()
    return MockPayPal()
