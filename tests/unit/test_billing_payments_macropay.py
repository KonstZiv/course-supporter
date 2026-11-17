"""The Macropay adapter without a network (BIL-PAY, the second provider).

No sandbox yet, and the documentation's pages were not reachable from here:
the webhook bodies are built from the fields the documentation names (read
2026-11-15, see the adapter's docstring) in the Standard Webhooks envelope
(``type``, ``timestamp``, ``data``), signed here with a test secret. The
sandbox check replaces them with Macropay's own deliveries. The API talks to
an ``httpx.MockTransport``: nothing leaves the process.

The signature's reference vector is Standard Webhooks' own, verbatim:
https://github.com/standard-webhooks/standard-webhooks/blob/36afae792227eae8a182a7a1a5711dbdc08a4334/libraries/python/tests/test_webhooks.py
(``test_sign_function``; the same in ``libraries/go/webhook_test.go``,
``TestWebhookSign``). The specification itself shows a signature without its
secret, so it gives no vector.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from structlog.testing import capture_logs

from course_supporter.billing.payments.catalog import Package
from course_supporter.billing.payments.macropay import (
    ID_HEADER,
    LIVE_API_BASE,
    MACROPAY,
    SANDBOX_API_BASE,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    MacropayProvider,
)
from course_supporter.billing.payments.port import (
    CheckoutRequest,
    CheckoutUnavailableError,
    InvalidWebhookError,
    PaymentProviderError,
    PurchaseCompleted,
    RefundApproved,
    RefundRequest,
    Unknown,
)
from course_supporter.billing.payments.registry import (
    PaymentConfigError,
    build_registry,
)
from course_supporter.config import Environment, Settings

TEST_KEY = "macropay_oat_test_key_for_unit_tests_only"
KEY_BYTES = b"macropay-unit-tests-only-32bytes"
SECRET = "whsec_" + base64.b64encode(KEY_BYTES).decode()
NOW = 1763380800
"""The clock of every test: 2026-11-17 12:00:00 UTC."""
WEBHOOK_ID = "msg_2KWPBgLlAfxdpx2AI54pPJ85f4W"
ACCOUNT = uuid.UUID("0190a0a0-0000-7000-8000-000000000001")
REF = uuid.UUID("5f0c2a5e-8d1b-4c3e-9a7f-2b6d4e8f1a3c")
"""The platform's own number of the checkout."""
STARTER = Package(id="starter", units=1000, prices_minor={"EUR": 990}, active=True)
ORDER = "0d5b3c8e-6f2a-4c1b-9e7d-3a2f1b0c9d8e"
REFUND = "7a1e9c4b-2d3f-4e5a-8b6c-1f0e9d8c7b6a"

# ── Bodies, from the fields the documentation names ────────────────────

ORDER_PAID: dict[str, Any] = {
    "type": "order.paid",
    "timestamp": "2026-11-17T11:59:58.123456Z",
    "data": {
        "id": ORDER,
        "status": "paid",
        "billing_reason": "purchase",
        "subtotal_amount": 990,
        "discount_amount": 0,
        "net_amount": 990,
        "tax_amount": 208,
        "total_amount": 1198,
        "refunded_amount": 0,
        "refunded_tax_amount": 0,
        "currency": "eur",
        "checkout_id": "c4b1d0e2-0000-4000-8000-000000000001",
        "product_id": "prod-starter",
        "customer": {
            "id": "cus-1",
            "external_id": None,
            "email": "customer@emaildomain",
        },
        "metadata": {
            "account_id": str(ACCOUNT),
            "package_id": "starter",
            "checkout_ref": str(REF),
        },
    },
}

REFUND_SUCCEEDED: dict[str, Any] = {
    "type": "refund.updated",
    "timestamp": "2026-11-17T11:59:59Z",
    "data": {
        "id": REFUND,
        "order_id": ORDER,
        "amount": 495,
        "tax_amount": 104,
        "currency": "eur",
        "status": "succeeded",
        "reason": "customer_request",
        "metadata": {},
    },
}

ORDER_REFUNDED: dict[str, Any] = {
    "type": "order.refunded",
    "timestamp": "2026-11-17T11:59:59Z",
    "data": ORDER_PAID["data"]
    | {"status": "partially_refunded", "refunded_amount": 495},
}


def macropay(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    *,
    api_base: str = SANDBOX_API_BASE,
    secret: str = SECRET,
    now: float = NOW,
) -> MacropayProvider:
    transport = httpx.MockTransport(handler) if handler is not None else None
    return MacropayProvider(
        api_key=TEST_KEY,
        webhook_secret=secret,
        api_base=api_base,
        transport=transport,
        clock=lambda: now,
    )


def signature(
    body: bytes,
    *,
    key: bytes = KEY_BYTES,
    webhook_id: str = WEBHOOK_ID,
    timestamp: int = NOW,
) -> str:
    """Standard Webhooks' ``v1,<base64>`` (written here, not by the adapter)."""
    signed = f"{webhook_id}.{timestamp}.".encode() + body
    digest = hmac.new(key, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def signed(
    payload: dict[str, Any],
    *,
    webhook_id: str = WEBHOOK_ID,
    timestamp: int = NOW,
) -> tuple[dict[str, str], bytes]:
    body = json.dumps(payload).encode()
    return {
        ID_HEADER: webhook_id,
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: signature(body, webhook_id=webhook_id, timestamp=timestamp),
    }, body


def with_(payload: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """A deep copy with ``data`` fields changed (None removes one)."""
    out = copy.deepcopy(payload)
    for name, value in changes.items():
        if value is None:
            out["data"].pop(name, None)
        else:
            out["data"][name] = value
    return out


def checkout_request(provider_item: str | None = "prod-starter") -> CheckoutRequest:
    return CheckoutRequest(
        account_id=ACCOUNT,
        checkout_ref=REF,
        package=STARTER,
        provider_item=provider_item,
        language="ukr",
        success_url=f"https://campus.example/t/billing/checkouts/{REF}",
        cancel_url="https://campus.example/t/billing?checkout=cancelled",
    )


# ── Signature ──────────────────────────────────────────────────────────


class TestSignature:
    def test_the_standard_webhooks_reference_vector(self) -> None:
        provider = macropay(secret="whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw")
        assert (
            provider.sign(
                "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330, b'{"test": 2432232314}'
            )
            == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
        )

    def test_the_reference_vector_passes_the_check(self) -> None:
        """Its body is no event of Macropay: refused after the signature held."""
        provider = macropay(
            secret="whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", now=1614265330
        )
        headers = {
            ID_HEADER: "msg_p5jXN8AQM9LWM0D4loKWxJek",
            TIMESTAMP_HEADER: "1614265330",
            SIGNATURE_HEADER: "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=",
        }
        with pytest.raises(InvalidWebhookError, match="not an event of macropay"):
            provider.parse_webhook(headers, b'{"test": 2432232314}')

    def test_a_body_signed_with_the_secret_is_read(self) -> None:
        [event] = macropay().parse_webhook(*signed(ORDER_PAID))
        assert isinstance(event, PurchaseCompleted)

    def test_a_wrong_signature_is_refused(self) -> None:
        headers, body = signed(ORDER_PAID)
        with pytest.raises(InvalidWebhookError, match="wrong signature"):
            macropay().parse_webhook(headers, body + b" ")

    @pytest.mark.parametrize(
        ("header", "value"),
        [(ID_HEADER, "msg_other"), (TIMESTAMP_HEADER, str(NOW - 1))],
    )
    def test_another_id_or_time_breaks_the_signature(
        self, header: str, value: str
    ) -> None:
        headers, body = signed(ORDER_PAID)
        with pytest.raises(InvalidWebhookError, match="wrong signature"):
            macropay().parse_webhook(headers | {header: value}, body)

    @pytest.mark.parametrize(
        "missing", [ID_HEADER, TIMESTAMP_HEADER, SIGNATURE_HEADER, "all"]
    )
    def test_no_headers_no_event(self, missing: str) -> None:
        headers, body = signed(ORDER_PAID)
        headers = {} if missing == "all" else headers | {missing: ""}
        with pytest.raises(InvalidWebhookError, match="no signature"):
            macropay().parse_webhook(headers, body)

    @pytest.mark.parametrize("offset", [-301, 301, -86400])
    def test_a_time_outside_five_minutes_is_refused(self, offset: int) -> None:
        headers, body = signed(ORDER_PAID, timestamp=NOW + offset)
        with pytest.raises(InvalidWebhookError, match="outside the 5 minutes"):
            macropay().parse_webhook(headers, body)

    @pytest.mark.parametrize("offset", [-300, 300])
    def test_five_minutes_either_way_is_within(self, offset: int) -> None:
        [event] = macropay().parse_webhook(*signed(ORDER_PAID, timestamp=NOW + offset))
        assert isinstance(event, PurchaseCompleted)

    @pytest.mark.parametrize("value", ["soon", "1763380800.5", "1e9"])
    def test_a_time_that_is_not_whole_seconds_is_refused(self, value: str) -> None:
        headers, body = signed(ORDER_PAID)
        with pytest.raises(InvalidWebhookError, match="timestamp that is not one"):
            macropay().parse_webhook(headers | {TIMESTAMP_HEADER: value}, body)

    def test_while_the_secret_rotates_one_valid_signature_of_several_is_enough(
        self,
    ) -> None:
        headers, body = signed(ORDER_PAID)
        old = signature(body, key=b"the-previous-secret-of-32-bytes!")
        rotated = headers | {
            SIGNATURE_HEADER: f"{old} {headers[SIGNATURE_HEADER]} v1a,AAAA"
        }
        [event] = macropay().parse_webhook(rotated, body)
        assert isinstance(event, PurchaseCompleted)

    def test_several_signatures_none_valid_is_refused(self) -> None:
        headers, body = signed(ORDER_PAID)
        wrong = " ".join(
            signature(body, key=key) for key in (b"first-wrong-key", b"second-wrong")
        )
        with pytest.raises(InvalidWebhookError, match="wrong signature"):
            macropay().parse_webhook(headers | {SIGNATURE_HEADER: wrong}, body)

    def test_the_secret_used_undecoded_is_refused(self) -> None:
        """The broken variant: HMAC keyed by the ``whsec_…`` text itself."""
        headers, body = signed(ORDER_PAID)
        undecoded = signature(body, key=SECRET.encode())
        with pytest.raises(InvalidWebhookError, match="wrong signature"):
            macropay().parse_webhook(headers | {SIGNATURE_HEADER: undecoded}, body)

    def test_the_prefix_is_optional_the_key_the_same(self) -> None:
        bare = macropay(secret=SECRET.removeprefix("whsec_"))
        [event] = bare.parse_webhook(*signed(ORDER_PAID))
        assert isinstance(event, PurchaseCompleted)

    @pytest.mark.parametrize("secret", ["whsec_not base64!", "whsec_", ""])
    def test_a_secret_that_is_not_whsec_base64_is_refused(self, secret: str) -> None:
        with pytest.raises(ValueError, match="webhook secret"):
            macropay(secret=secret)

    @pytest.mark.parametrize(
        "body", [b"not json", b"[]", b'{"type": "order.paid"}', b"\xff\xfe"]
    )
    def test_a_signed_body_that_is_not_an_event_is_refused(self, body: bytes) -> None:
        headers = {
            ID_HEADER: WEBHOOK_ID,
            TIMESTAMP_HEADER: str(NOW),
            SIGNATURE_HEADER: signature(body),
        }
        with pytest.raises(InvalidWebhookError, match="not an event of macropay"):
            macropay().parse_webhook(headers, body)

    def test_an_id_longer_than_the_journal_keeps_is_refused(self) -> None:
        with pytest.raises(InvalidWebhookError, match="1 to 64"):
            macropay().parse_webhook(*signed(ORDER_PAID, webhook_id="m" * 65))


# ── Events ─────────────────────────────────────────────────────────────


class TestOrderPaid:
    def test_a_paid_order_is_a_purchase_of_the_package(self) -> None:
        [event] = macropay().parse_webhook(*signed(ORDER_PAID))
        assert event == PurchaseCompleted(
            event_id=WEBHOOK_ID,  # the body has no id: webhook-id is the event's
            provider_type="order.paid",
            occurred_at=datetime(2026, 11, 17, 11, 59, 58, 123456, tzinfo=UTC),
            account_id=ACCOUNT,
            checkout_ref=REF,
            package_id="starter",
            amount_paid_minor=1198,  # total_amount: tax included
            currency="EUR",
            transaction_id=ORDER,
            line_id=ORDER,
        )

    def test_without_an_account_the_purchase_names_none(self) -> None:
        payload = with_(ORDER_PAID, metadata={"package_id": "starter"})
        [event] = macropay().parse_webhook(*signed(payload))
        assert isinstance(event, PurchaseCompleted)
        assert (event.account_id, event.checkout_ref) == (None, None)

    @pytest.mark.parametrize(
        "metadata", [None, {}, {"account_id": str(ACCOUNT)}, {"package_id": 7}]
    )
    def test_a_purchase_that_names_no_package_is_refused(
        self, metadata: dict[str, Any] | None
    ) -> None:
        payload = with_(ORDER_PAID)
        payload["data"]["metadata"] = metadata
        with pytest.raises(InvalidWebhookError, match="names no package"):
            macropay().parse_webhook(*signed(payload))

    @pytest.mark.parametrize("status", ["pending", "refunded", None])
    def test_an_order_not_paid_is_ignored(self, status: str | None) -> None:
        [event] = macropay().parse_webhook(*signed(with_(ORDER_PAID, status=status)))
        assert event == Unknown(
            event_id=WEBHOOK_ID,
            provider_type="order.paid",
            occurred_at=datetime(2026, 11, 17, 11, 59, 58, 123456, tzinfo=UTC),
        )

    def test_no_total_amount_is_refused(self) -> None:
        payload = with_(ORDER_PAID, total_amount=None)
        with pytest.raises(InvalidWebhookError, match="amount paid"):
            macropay().parse_webhook(*signed(payload))

    def test_the_payers_data_never_reaches_the_refusal(self) -> None:
        payload = with_(ORDER_PAID, total_amount="customer@emaildomain")
        with pytest.raises(InvalidWebhookError) as caught:
            macropay().parse_webhook(*signed(payload))
        assert "emaildomain" not in str(caught.value)

    @pytest.mark.parametrize("event_type", ["order.created", "order.updated"])
    def test_the_orders_other_events_are_unknown(self, event_type: str) -> None:
        payload = copy.deepcopy(ORDER_PAID) | {"type": event_type}
        [event] = macropay().parse_webhook(*signed(payload))
        assert isinstance(event, Unknown) and event.provider_type == event_type


class TestRefunds:
    @pytest.mark.parametrize("event_type", ["refund.created", "refund.updated"])
    def test_a_succeeded_refund_is_an_approval_of_its_adjustment(
        self, event_type: str
    ) -> None:
        payload = copy.deepcopy(REFUND_SUCCEEDED) | {"type": event_type}
        [event] = macropay().parse_webhook(*signed(payload))
        assert event == RefundApproved(
            event_id=WEBHOOK_ID,
            provider_type=event_type,
            occurred_at=datetime(2026, 11, 17, 11, 59, 59, tzinfo=UTC),
            adjustment_id=REFUND,
            transaction_id=ORDER,
            amount_minor=599,  # amount + tax_amount: as total_amount, with tax
            currency="EUR",
        )

    def test_a_partial_refund_is_its_own_amount(self) -> None:
        payload = with_(REFUND_SUCCEEDED, amount=1, tax_amount=None)
        [event] = macropay().parse_webhook(*signed(payload))
        assert isinstance(event, RefundApproved) and event.amount_minor == 1

    @pytest.mark.parametrize("status", ["pending", "failed", "canceled", None])
    def test_a_refund_not_succeeded_is_ignored(self, status: str | None) -> None:
        payload = with_(REFUND_SUCCEEDED, status=status)
        [event] = macropay().parse_webhook(*signed(payload))
        assert isinstance(event, Unknown) and event.provider_type == "refund.updated"

    def test_a_refund_that_names_no_order_is_refused(self) -> None:
        payload = with_(REFUND_SUCCEEDED, order_id=None)
        with pytest.raises(InvalidWebhookError, match="not an event of macropay"):
            macropay().parse_webhook(*signed(payload))

    def test_order_refunded_is_unknown_not_a_second_approval(self) -> None:
        [event] = macropay().parse_webhook(*signed(ORDER_REFUNDED))
        assert isinstance(event, Unknown) and event.provider_type == "order.refunded"


@pytest.mark.parametrize("status", ["failed", "expired"])
def test_a_failed_checkout_is_unknown(status: str) -> None:
    payload = {
        "type": "checkout.updated",
        "timestamp": "2026-11-17T11:59:59Z",
        "data": {"id": "c4b1d0e2", "status": status, "metadata": {}},
    }
    [event] = macropay().parse_webhook(*signed(payload))
    assert isinstance(event, Unknown) and event.provider_type == "checkout.updated"


@pytest.mark.parametrize(
    "event_type", ["customer.created", "product.updated", "dispute.created"]
)
def test_any_other_event_is_unknown(event_type: str) -> None:
    payload = {"type": event_type, "data": {"id": "x"}}
    [event] = macropay().parse_webhook(*signed(payload))
    assert event == Unknown(event_id=WEBHOOK_ID, provider_type=event_type)


# ── Checkout ───────────────────────────────────────────────────────────


class Recorder:
    """A fake Macropay API: answers with ``status`` and ``body``; keeps requests."""

    def __init__(self, status: int = 201, body: Any = None) -> None:
        self.status = status
        self.body = (
            {
                "id": "c4b1d0e2-0000-4000-8000-000000000001",
                "url": "https://sandbox-checkout.macropay.example/c4b1d0e2",
                "status": "open",
                "expires_at": "2026-11-17T13:00:00Z",
            }
            if body is None
            else body
        )
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.body, bytes):
            return httpx.Response(self.status, content=self.body)
        return httpx.Response(self.status, json=self.body)


class TestCreateCheckout:
    async def test_it_posts_the_product_with_our_metadata(self) -> None:
        api = Recorder()
        checkout = await macropay(api).create_checkout(checkout_request())
        assert checkout.checkout_id == "c4b1d0e2-0000-4000-8000-000000000001"
        assert checkout.url == "https://sandbox-checkout.macropay.example/c4b1d0e2"
        [sent] = api.requests
        assert (sent.method, str(sent.url)) == (
            "POST",
            "https://sandbox-api.macropay.ai/v1/checkouts/",
        )
        assert sent.headers["authorization"] == f"Bearer {TEST_KEY}"
        assert json.loads(sent.content) == {
            "products": ["prod-starter"],
            "currency": "eur",
            "success_url": f"https://campus.example/t/billing/checkouts/{REF}",
            "return_url": "https://campus.example/t/billing?checkout=cancelled",
            "metadata": {
                "account_id": str(ACCOUNT),
                "package_id": "starter",
                "checkout_ref": str(REF),
            },
        }

    async def test_the_live_host_only_when_named(self) -> None:
        api = Recorder()
        await macropay(api, api_base=LIVE_API_BASE).create_checkout(checkout_request())
        assert str(api.requests[0].url) == "https://api.macropay.ai/v1/checkouts/"

    async def test_a_package_without_a_macropay_product_is_unavailable(self) -> None:
        api = Recorder()
        with pytest.raises(CheckoutUnavailableError, match="no product"):
            await macropay(api).create_checkout(checkout_request(provider_item=None))
        assert api.requests == []

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429, 500, 503])
    async def test_a_refusal_or_failure_is_a_provider_error_asked_once(
        self, status: int
    ) -> None:
        api = Recorder(status, {"detail": "no"})
        with pytest.raises(PaymentProviderError, match=str(status)):
            await macropay(api).create_checkout(checkout_request())
        assert len(api.requests) == 1  # no retries inside the adapter

    @pytest.mark.parametrize("body", [b"<html>", {"id": "c1"}, {"url": "https://x"}])
    async def test_an_answer_without_id_or_link_is_a_provider_error(
        self, body: Any
    ) -> None:
        with pytest.raises(PaymentProviderError):
            await macropay(Recorder(201, body)).create_checkout(checkout_request())

    @pytest.mark.parametrize(
        "error",
        [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.ConnectError("c")],
    )
    async def test_the_network_failing_is_a_provider_error(
        self, error: httpx.HTTPError
    ) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise error

        with pytest.raises(PaymentProviderError, match=type(error).__name__):
            await macropay(handler).create_checkout(checkout_request())

    async def test_the_token_and_the_secret_are_never_logged(self) -> None:
        provider = macropay(Recorder(500, {"detail": "boom"}))
        with capture_logs() as logs, pytest.raises(PaymentProviderError):
            await provider.create_checkout(checkout_request())
        assert logs, "the failure is logged"
        text = repr(logs) + repr(provider)
        assert TEST_KEY not in text and SECRET not in text


# ── Refund ─────────────────────────────────────────────────────────────

BATCH = uuid.UUID("0190a0a0-0000-7000-8000-0000000000b1")


def refund_request(amount_minor: int = 599) -> RefundRequest:
    return RefundRequest(
        batch_id=BATCH,
        transaction_id=ORDER,
        line_id=ORDER,
        units=500,
        amount_minor=amount_minor,
        currency="EUR",
    )


class TestRequestRefund:
    async def test_it_posts_the_order_the_cents_and_the_reason(self) -> None:
        api = Recorder(201, {"id": REFUND, "status": "pending", "amount": 599})
        assert await macropay(api).request_refund(refund_request()) == REFUND
        [sent] = api.requests
        assert (sent.method, str(sent.url)) == (
            "POST",
            "https://sandbox-api.macropay.ai/v1/refunds/",
        )
        assert json.loads(sent.content) == {
            "order_id": ORDER,
            "amount": 599,
            "reason": "customer_request",
            "metadata": {"batch_id": str(BATCH)},
        }

    @pytest.mark.parametrize("status", [400, 422, 500])
    async def test_a_refusal_is_a_provider_error(self, status: int) -> None:
        with pytest.raises(PaymentProviderError, match=str(status)):
            await macropay(Recorder(status, {})).request_refund(refund_request())

    async def test_an_answer_without_its_id_is_a_provider_error(self) -> None:
        with pytest.raises(PaymentProviderError, match="without its id"):
            await macropay(Recorder(201, {"status": "pending"})).request_refund(
                refund_request()
            )


# ── Settings, the registry, the test mode ──────────────────────────────


def test_without_its_token_and_secret_macropay_stops_the_startup() -> None:
    settings = Settings(
        environment=Environment.TESTING, billing_providers=[MACROPAY]
    ).model_copy(update={"environment": Environment.PRODUCTION})
    with pytest.raises(
        PaymentConfigError, match="MACROPAY_API_KEY, MACROPAY_WEBHOOK_SECRET"
    ):
        build_registry(settings)
    half = settings.model_copy(update={"macropay_api_key": SecretStr(TEST_KEY)})
    with pytest.raises(PaymentConfigError, match="MACROPAY_WEBHOOK_SECRET"):
        build_registry(half)


def test_a_secret_that_is_not_whsec_base64_stops_the_startup_unquoted() -> None:
    settings = Settings(
        environment=Environment.TESTING,
        billing_providers=[MACROPAY],
        macropay_api_key=TEST_KEY,
        macropay_webhook_secret="whsec_not base64!",
    )
    with pytest.raises(PaymentConfigError, match="MACROPAY_WEBHOOK_SECRET") as caught:
        build_registry(settings)
    assert "not base64!" not in str(caught.value)


@pytest.mark.parametrize(
    "environment",
    [Environment.PRODUCTION, Environment.STAGING, Environment.DEVELOPMENT],
)
def test_with_them_macropay_is_switched_on_on_the_sandbox_host(
    environment: Environment,
) -> None:
    settings = Settings(
        environment=Environment.TESTING,
        billing_providers=[MACROPAY],
        macropay_api_key=TEST_KEY,
        macropay_webhook_secret=SECRET,
    ).model_copy(update={"environment": environment})
    assert settings.macropay_api_base == SANDBOX_API_BASE
    provider = build_registry(settings).get(MACROPAY)
    assert isinstance(provider, MacropayProvider)
    assert provider.test_mode
    assert SANDBOX_API_BASE in repr(provider)


def test_the_api_host_is_macropays_sandbox_or_live_one_only() -> None:
    assert Settings(macropay_api_base=LIVE_API_BASE).macropay_api_base == LIVE_API_BASE
    with pytest.raises(ValidationError):
        Settings(macropay_api_base="https://macropay.example/v1")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("api_base", "test_mode"),
    [
        (SANDBOX_API_BASE, True),
        (LIVE_API_BASE, False),
        (LIVE_API_BASE + "/", False),
        ("https://sandbox-api.macropay.ai/v1/", True),
        ("https://macropay.example/v1", True),  # anything not live: closed
    ],
)
def test_macropay_is_in_test_mode_unless_on_its_live_host(
    api_base: str, test_mode: bool
) -> None:
    assert macropay(api_base=api_base).test_mode is test_mode
