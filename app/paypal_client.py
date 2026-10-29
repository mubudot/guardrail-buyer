"""PayPal client interface + Mock and Sandbox implementations.

The Sandbox client targets the PayPal Orders v2 REST API:
  POST /v1/oauth2/token               (client_credentials -> Bearer token)
  POST /v2/checkout/orders            (intent CAPTURE, returns payer-action URL)
  GET  /v2/checkout/orders/{id}
  POST /v2/checkout/orders/{id}/capture  (after the buyer approved)

Auth modes (PAYPAL_AUTH):
  oauth  (default) PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET from env -> OAuth2 token -> Bearer.
  proxy  no credentials in-process: an egress proxy injects
         `Authorization: Basic base64(client_id:secret)` for api-m.sandbox.paypal.com
         and PayPal REST accepts client credentials directly. Nothing secret lives in the app.
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
    """A PayPal API error. `issue` is PayPal's machine-readable code when known
    (e.g. ORDER_NOT_APPROVED, ORDER_ALREADY_CAPTURED), `status_code` the HTTP status."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 issue: str | None = None, debug_id: str | None = None) -> None:
        super().__init__(message)
        self.status_code, self.issue, self.debug_id = status_code, issue, debug_id

    @classmethod
    def from_response(cls, what: str, r: "httpx.Response") -> "PayPalError":
        issue = debug_id = None
        try:
            body = r.json()
            debug_id = body.get("debug_id")
            details = body.get("details") or []
            issue = (details[0].get("issue") if details else None) or body.get("name") or body.get("error")
        except Exception:  # non-JSON body
            pass
        msg = f"{what} failed: HTTP {r.status_code}" + (f" {issue}" if issue else "")
        if debug_id:
            msg += f" (debug_id {debug_id})"
        return cls(msg, status_code=r.status_code, issue=issue, debug_id=debug_id)


class PayPalClient(ABC):
    name: str = "abstract"

    @abstractmethod
    def create_order(self, amount: Decimal, currency: str, description: str,
                     reference_id: str, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Create an order. Returns {'id', 'status', 'approve_url'?, 'raw'}."""

    @abstractmethod
    def capture_order(self, order_id: str) -> dict[str, Any]:
        """Capture an approved order. Returns {'id', 'status', 'capture_id'?, 'raw'}."""

    def get_order(self, order_id: str) -> dict[str, Any]:  # pragma: no cover - optional
        raise NotImplementedError


class MockPayPal(PayPalClient):
    """In-memory stand-in that mimics the Orders v2 status machine.

    By default mock orders are treated as already payer-approved so they are
    captured immediately. With `require_payer_approval=True` (env
    PAYPAL_MOCK_APPROVAL=1) they start as PAYER_ACTION_REQUIRED with an
    approve_url pointing at the app's local mock checkout page, exactly like
    the real Sandbox flow.
    """
    name = "mock"

    def __init__(self, require_payer_approval: bool = False,
                 checkout_base: str = "http://localhost:8000") -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.require_payer_approval = require_payer_approval
        self.checkout_base = checkout_base.rstrip("/")

    def create_order(self, amount, currency, description, reference_id, items=None):
        oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
        status = "PAYER_ACTION_REQUIRED" if self.require_payer_approval else "APPROVED"
        self.orders[oid] = {
            "id": oid, "status": status, "amount": str(amount), "currency": currency,
            "description": description, "reference_id": reference_id, "items": items or [],
        }
        approve = f"{self.checkout_base}/paypal/mock-checkout?token={oid}" if self.require_payer_approval else None
        return {"id": oid, "status": status, "approve_url": approve, "raw": dict(self.orders[oid])}

    def approve(self, order_id: str) -> None:
        """Simulate the buyer clicking 'Pay' on the PayPal checkout page."""
        order = self.orders.get(order_id)
        if order is None:
            raise PayPalError(f"order {order_id} not found")
        if order["status"] == "PAYER_ACTION_REQUIRED":
            order["status"] = "APPROVED"

    def get_order(self, order_id):
        order = self.orders.get(order_id)
        if order is None:
            raise PayPalError(f"order {order_id} not found")
        return {"id": order_id, "status": order["status"], "raw": dict(order)}

    def capture_order(self, order_id):
        order = self.orders.get(order_id)
        if order is None:
            raise PayPalError(f"order {order_id} not found")
        if order["status"] == "COMPLETED":  # like Sandbox: ORDER_ALREADY_CAPTURED -> current order
            return {"id": order_id, "status": "COMPLETED", "capture_id": order.get("capture_id"),
                    "capture_status": "COMPLETED", "raw": dict(order)}
        if order["status"] != "APPROVED":
            raise PayPalError(f"capture_order failed: HTTP 422 ORDER_NOT_APPROVED (order {order_id} is {order['status']})",
                              status_code=422, issue="ORDER_NOT_APPROVED")
        order["status"] = "COMPLETED"
        cap = "MOCKCAP-" + uuid.uuid4().hex[:10].upper()
        order["capture_id"] = cap
        return {"id": order_id, "status": "COMPLETED", "capture_id": cap, "capture_status": "COMPLETED",
                "raw": dict(order)}


class SandboxPayPal(PayPalClient):
    """Real PayPal Sandbox (Orders v2). See module docstring for auth modes."""
    name = "sandbox"

    def __init__(self, client_id: str | None = None, client_secret: str | None = None,
                 base_url: str | None = None, timeout: float = 30.0,
                 return_url: str | None = None, cancel_url: str | None = None,
                 auth: str | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.auth = (auth or os.environ.get("PAYPAL_AUTH", "oauth")).lower()
        self.client_id = client_id or os.environ.get("PAYPAL_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("PAYPAL_CLIENT_SECRET")
        if self.auth == "oauth" and (not self.client_id or not self.client_secret):
            raise PayPalError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET not set (or use PAYPAL_AUTH=proxy)")
        if self.auth not in ("oauth", "proxy"):
            raise PayPalError(f"unknown PAYPAL_AUTH {self.auth!r}")
        self.base_url = (base_url or os.environ.get("PAYPAL_BASE_URL") or SANDBOX_BASE).rstrip("/")
        public = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
        self.return_url = return_url or f"{public}/paypal/return"
        self.cancel_url = cancel_url or f"{public}/paypal/cancel"
        self._http = httpx.Client(timeout=timeout, transport=transport)
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
            raise PayPalError.from_response("OAuth", r)
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 300))
        return self._token

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.auth == "oauth":
            h["Authorization"] = f"Bearer {self._access_token()}"
        # proxy mode: Authorization is injected on the wire, never held here.
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
            raise PayPalError.from_response("create_order", r)
        body = r.json()
        approve = next((l["href"] for l in body.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)
        return {"id": body["id"], "status": body["status"], "approve_url": approve, "raw": body}

    def get_order(self, order_id):
        r = self._http.get(f"{self.base_url}/v2/checkout/orders/{order_id}", headers=self._headers())
        if r.status_code != 200:
            raise PayPalError.from_response("get_order", r)
        body = r.json()
        return {"id": body["id"], "status": body["status"], "raw": body}

    def capture_order(self, order_id):
        # A fresh PayPal-Request-Id per attempt: PayPal replays the stored response
        # for a repeated id, so reusing one after ORDER_NOT_APPROVED could replay that
        # 422 even once the buyer has approved. Double capture is prevented by the
        # app's own status check and by PayPal (ORDER_ALREADY_CAPTURED).
        r = self._http.post(f"{self.base_url}/v2/checkout/orders/{order_id}/capture",
                            headers=self._headers(idempotency_key=f"cap-{order_id}-{uuid.uuid4().hex[:8]}"))
        if r.status_code not in (200, 201):
            err = PayPalError.from_response("capture_order", r)
            if err.issue == "ORDER_ALREADY_CAPTURED":
                body = self.get_order(order_id)["raw"]
            else:
                raise err
        else:
            body = r.json()
        cap_id = cap_status = None
        try:
            cap = body["purchase_units"][0]["payments"]["captures"][0]
            cap_id, cap_status = cap.get("id"), cap.get("status")
        except (KeyError, IndexError, TypeError):
            pass
        return {"id": body["id"], "status": body["status"], "capture_id": cap_id,
                "capture_status": cap_status, "raw": body}


def make_client(kind: str | None = None) -> PayPalClient:
    kind = (kind or os.environ.get("PAYPAL_MODE", "mock")).lower()
    if kind == "sandbox":
        return SandboxPayPal()
    return MockPayPal(require_payer_approval=os.environ.get("PAYPAL_MOCK_APPROVAL") == "1",
                      checkout_base=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000"))
