"""The Creem adapter without a network (BIL-PAY part 2).

The webhook bodies are Creem's own samples, as its documentation shows them
(https://docs.creem.io/code/webhooks, read 2026-11-14) — ``checkout.completed``,
``refund.created``, ``dispute.created`` — with our checkout's metadata where the
adapter reads it, signed here with a test secret. The checkout talks to an
``httpx.MockTransport``: nothing leaves the process.
"""

from __future__ import annotations

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
from course_supporter.billing.payments.creem import (
    CREEM,
    LIVE_API_BASE,
    SIGNATURE_HEADER,
    TEST_API_BASE,
    CreemProvider,
)
from course_supporter.billing.payments.port import (
    Chargeback,
    CheckoutRequest,
    CheckoutUnavailableError,
    InvalidWebhookError,
    PaymentProviderError,
    PurchaseCompleted,
    RefundApproved,
    RefundRequest,
    RefundUnavailableError,
    Unknown,
)
from course_supporter.billing.payments.registry import (
    PaymentConfigError,
    build_registry,
)
from course_supporter.config import Environment, Settings

TEST_KEY = "creem_test_key_for_unit_tests_only"
TEST_SECRET = "whsec_test_secret_for_unit_tests_only"
ACCOUNT = uuid.UUID("0190a0a0-0000-7000-8000-000000000001")
STARTER = Package(id="starter", units=1000, prices_minor={"EUR": 990}, active=True)
ORDER = "ord_4aDwWXjMLpes4Kj4XqNnUA"

# ── Creem's samples (https://docs.creem.io/code/webhooks) ───────────────

CHECKOUT_COMPLETED: dict[str, Any] = {
    "id": "evt_5WHHcZPv7VS0YUsberIuOz",
    "eventType": "checkout.completed",
    "created_at": 1728734327412,
    "object": {
        "id": "ch_4l0N34kxo16AhRKUHFUuXr",
        "object": "checkout",
        "request_id": "my-request-id",
        "order": {
            "object": "order",
            "id": ORDER,
            "customer": "cust_1OcIK1GEuVvXZwD19tjq2z",
            "product": "prod_d1AY2Sadk9YAvLI0pj97f",
            "amount": 1000,
            "currency": "EUR",
            "sub_total": 1000,
            "tax_amount": 210,
            "amount_due": 1210,
            "amount_paid": 1210,
            "status": "paid",
            "type": "recurring",
            "transaction": "tran_5yMaWzAl3jxuGJMCOrYWwk",
            "created_at": "2024-10-12T11:58:33.097Z",
            "updated_at": "2024-10-12T11:58:33.097Z",
            "mode": "local",
        },
        "product": {
            "id": "prod_d1AY2Sadk9YAvLI0pj97f",
            "object": "product",
            "name": "Monthly",
            "price": 1000,
            "currency": "EUR",
            "billing_type": "recurring",
            "status": "active",
            "tax_mode": "exclusive",
            "mode": "local",
        },
        "units": 1,
        "customer": {
            "id": "cust_1OcIK1GEuVvXZwD19tjq2z",
            "object": "customer",
            "email": "customer@emaildomain",
            "name": "Tester Test",
            "metadata": None,
            "country": "NL",
            "mode": "local",
        },
        "custom_fields": [],
        "status": "completed",
        # The docs' sample carries {"custom_data": ...}; ours carries this.
        "metadata": {"account_id": str(ACCOUNT), "package_id": "starter"},
        "mode": "local",
    },
}

REFUND_CREATED: dict[str, Any] = {
    "id": "evt_61eTsJHUgInFw2BQKhTiPV",
    "eventType": "refund.created",
    "created_at": 1728734351631,
    "object": {
        "id": "ref_3DB9NQFvk18TJwSqd0N6bd",
        "object": "refund",
        "status": "succeeded",
        "refund_amount": 1210,
        "refund_currency": "EUR",
        "reason": "requested_by_customer",
        "transaction": {
            "id": "tran_5yMaWzAl3jxuGJMCOrYWwk",
            "object": "transaction",
            "amount": 1000,
            "amount_paid": 1210,
            "currency": "EUR",
            "type": "invoice",
            "tax_country": "NL",
            "tax_amount": 210,
            "status": "refunded",
            "refunded_amount": 1210,
            "order": ORDER,
            "customer": "cust_1OcIK1GEuVvXZwD19tjq2z",
            "created_at": 1728734327109,
            "mode": "local",
        },
        "checkout": {
            "id": "ch_4l0N34kxo16AhRKUHFUuXr",
            "object": "checkout",
            "product": "prod_d1AY2Sadk9YAvLI0pj97f",
            "units": 1,
            "status": "completed",
            "metadata": {"account_id": str(ACCOUNT), "package_id": "starter"},
            "mode": "local",
        },
        "order": {
            "object": "order",
            "id": ORDER,
            "amount": 1000,
            "currency": "EUR",
            "amount_paid": 1210,
            "status": "paid",
            "transaction": "tran_5yMaWzAl3jxuGJMCOrYWwk",
            "mode": "local",
        },
        "customer": {"id": "cust_1OcIK1GEuVvXZwD19tjq2z", "object": "customer"},
        "created_at": 1728734351525,
        "mode": "local",
    },
}

DISPUTE_CREATED: dict[str, Any] = {
    "id": "evt_6mfLDL7P0NYwYQqCrICvDH",
    "eventType": "dispute.created",
    "created_at": 1750941264812,
    "object": {
        "id": "disp_6vSsOdTANP5PhOzuDlUuXE",
        "object": "dispute",
        "amount": 1331,
        "currency": "EUR",
        "transaction": {
            "id": "tran_4Dk8CxWFdceRUQgMFhCCXX",
            "object": "transaction",
            "amount": 1100,
            "amount_paid": 1331,
            "currency": "EUR",
            "status": "chargeback",
            "refunded_amount": 1331,
            "order": "ord_57bf8042UmG8fFypxZrfnj",
            "mode": "sandbox",
        },
        "checkout": {
            "id": "ch_1bJMvqGGzHIftf4ewLXJeq",
            "object": "checkout",
            "product": "prod_3EFtQRQ9SNIizK3xwfxZHu",
            "units": 1,
            "custom_fields": [],
            "status": "completed",
            "mode": "sandbox",
        },
        "order": {
            "object": "order",
            "id": "ord_57bf8042UmG8fFypxZrfnj",
            "amount": 1100,
            "currency": "EUR",
            "amount_paid": 1331,
            "status": "paid",
            "transaction": "tran_4Dk8CxWFdceRUQgMFhCCXX",
            "mode": "sandbox",
        },
        "created_at": 1750941264728,
        "mode": "sandbox",
    },
}


def creem(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    *,
    api_base: str = TEST_API_BASE,
) -> CreemProvider:
    transport = httpx.MockTransport(handler) if handler is not None else None
    return CreemProvider(
        api_key=TEST_KEY,
        webhook_secret=TEST_SECRET,
        api_base=api_base,
        transport=transport,
    )


def signed(payload: dict[str, Any]) -> tuple[dict[str, str], bytes]:
    body = json.dumps(payload).encode()
    signature = hmac.new(TEST_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {SIGNATURE_HEADER: signature}, body


def with_(payload: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """A deep copy with ``object`` fields changed (None removes one)."""
    out = copy.deepcopy(payload)
    for name, value in changes.items():
        if value is None:
            out["object"].pop(name, None)
        else:
            out["object"][name] = value
    return out


def checkout_request(provider_item: str | None = "prod_starter") -> CheckoutRequest:
    return CheckoutRequest(
        account_id=ACCOUNT,
        package=STARTER,
        provider_item=provider_item,
        language="ukr",
        success_url="https://campus.example/t/billing?checkout=success",
        cancel_url="https://campus.example/t/billing?checkout=cancelled",
    )


# ── Signature ──────────────────────────────────────────────────────────


class TestSignature:
    def test_a_body_signed_with_the_secret_is_read(self) -> None:
        headers, body = signed(CHECKOUT_COMPLETED)
        [event] = creem().parse_webhook(headers, body)
        assert isinstance(event, PurchaseCompleted)

    def test_the_documented_algorithm_hmac_sha256_hex_of_the_raw_body(self) -> None:
        body = b'{"any": "bytes"}'
        expected = hmac.new(TEST_SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert creem().sign(body) == expected

    @pytest.mark.parametrize(
        "headers",
        [{}, {SIGNATURE_HEADER: ""}, {"x-fake-signature": "abc"}],
        ids=["none", "empty", "another-header"],
    )
    def test_no_signature_is_refused(self, headers: dict[str, str]) -> None:
        _, body = signed(CHECKOUT_COMPLETED)
        with pytest.raises(InvalidWebhookError, match="no signature"):
            creem().parse_webhook(headers, body)

    def test_a_wrong_signature_is_refused(self) -> None:
        headers, body = signed(CHECKOUT_COMPLETED)
        with pytest.raises(InvalidWebhookError, match="wrong signature"):
            creem().parse_webhook(headers, body + b" ")

    def test_another_secret_is_refused(self) -> None:
        _, body = signed(CHECKOUT_COMPLETED)
        other = hmac.new(b"whsec_other", body, hashlib.sha256).hexdigest()
        with pytest.raises(InvalidWebhookError, match="wrong signature"):
            creem().parse_webhook({SIGNATURE_HEADER: other}, body)

    @pytest.mark.parametrize(
        "body", [b"not json", b"[]", b'{"id": "evt_1"}', b"\xff\xfe"]
    )
    def test_a_signed_body_that_is_not_an_event_is_refused(self, body: bytes) -> None:
        provider = creem()
        with pytest.raises(InvalidWebhookError, match="not an event of creem"):
            provider.parse_webhook({SIGNATURE_HEADER: provider.sign(body)}, body)


# ── Events ─────────────────────────────────────────────────────────────


class TestCheckoutCompleted:
    def test_a_paid_order_is_a_purchase_of_the_package(self) -> None:
        [event] = creem().parse_webhook(*signed(CHECKOUT_COMPLETED))
        assert event == PurchaseCompleted(
            event_id="evt_5WHHcZPv7VS0YUsberIuOz",
            provider_type="checkout.completed",
            occurred_at=datetime(2024, 10, 12, 11, 58, 47, 412000, tzinfo=UTC),
            account_id=ACCOUNT,
            package_id="starter",
            amount_paid_minor=1210,  # order.amount_paid: tax included
            currency="EUR",
            transaction_id=ORDER,
            line_id=ORDER,
        )

    def test_without_an_account_the_purchase_names_none(self) -> None:
        payload = with_(CHECKOUT_COMPLETED, metadata={"package_id": "starter"})
        [event] = creem().parse_webhook(*signed(payload))
        assert isinstance(event, PurchaseCompleted) and event.account_id is None

    @pytest.mark.parametrize(
        "metadata", [None, {}, {"account_id": str(ACCOUNT)}, {"package_id": 7}]
    )
    def test_a_purchase_that_names_no_package_is_refused(
        self, metadata: dict[str, Any] | None
    ) -> None:
        payload = with_(CHECKOUT_COMPLETED)
        payload["object"]["metadata"] = metadata
        with pytest.raises(InvalidWebhookError, match="names no package"):
            creem().parse_webhook(*signed(payload))

    def test_an_order_not_paid_is_ignored(self) -> None:
        payload = with_(CHECKOUT_COMPLETED)
        payload["object"]["order"]["status"] = "pending"
        [event] = creem().parse_webhook(*signed(payload))
        assert isinstance(event, Unknown)
        assert event.provider_type == "checkout.completed"

    def test_more_than_one_unit_is_refused(self) -> None:
        with pytest.raises(InvalidWebhookError, match="2 units"):
            creem().parse_webhook(*signed(with_(CHECKOUT_COMPLETED, units=2)))

    def test_no_amount_paid_is_refused(self) -> None:
        payload = with_(CHECKOUT_COMPLETED)
        del payload["object"]["order"]["amount_paid"]
        with pytest.raises(InvalidWebhookError, match="amount paid"):
            creem().parse_webhook(*signed(payload))

    def test_the_payers_data_never_reaches_the_refusal(self) -> None:
        payload = with_(CHECKOUT_COMPLETED)
        payload["object"]["order"]["amount_paid"] = "customer@emaildomain"
        with pytest.raises(InvalidWebhookError) as caught:
            creem().parse_webhook(*signed(payload))
        assert "emaildomain" not in str(caught.value)


class TestRefundCreated:
    def test_a_refund_is_an_approval_of_its_adjustment(self) -> None:
        [event] = creem().parse_webhook(*signed(REFUND_CREATED))
        assert event == RefundApproved(
            event_id="evt_61eTsJHUgInFw2BQKhTiPV",
            provider_type="refund.created",
            occurred_at=datetime(2024, 10, 12, 11, 59, 11, 631000, tzinfo=UTC),
            adjustment_id="ref_3DB9NQFvk18TJwSqd0N6bd",
            transaction_id=ORDER,
            amount_minor=1210,
            currency="EUR",
        )

    def test_without_the_order_the_transaction_names_it(self) -> None:
        [event] = creem().parse_webhook(*signed(with_(REFUND_CREATED, order=None)))
        assert isinstance(event, RefundApproved) and event.transaction_id == ORDER

    def test_with_ids_only_the_order_id_is_read(self) -> None:
        payload = with_(
            REFUND_CREATED, order=ORDER, transaction="tran_5yMaWzAl3jxuGJMCOrYWwk"
        )
        [event] = creem().parse_webhook(*signed(payload))
        assert isinstance(event, RefundApproved) and event.transaction_id == ORDER

    def test_a_refund_not_succeeded_is_ignored(self) -> None:
        payload = with_(REFUND_CREATED, status="pending")
        [event] = creem().parse_webhook(*signed(payload))
        assert isinstance(event, Unknown)


class TestDisputeCreated:
    def test_a_dispute_is_a_chargeback_of_its_order(self) -> None:
        [event] = creem().parse_webhook(*signed(DISPUTE_CREATED))
        assert event == Chargeback(
            event_id="evt_6mfLDL7P0NYwYQqCrICvDH",
            provider_type="dispute.created",
            occurred_at=datetime(2025, 6, 26, 12, 34, 24, 812000, tzinfo=UTC),
            dispute_id="disp_6vSsOdTANP5PhOzuDlUuXE",
            account_id=None,  # the sample's checkout carries no metadata
            transaction_id="ord_57bf8042UmG8fFypxZrfnj",
            amount_minor=1331,
            currency="EUR",
        )

    def test_the_account_comes_from_the_checkouts_metadata(self) -> None:
        payload = with_(DISPUTE_CREATED)
        payload["object"]["checkout"]["metadata"] = {"account_id": str(ACCOUNT)}
        [event] = creem().parse_webhook(*signed(payload))
        assert isinstance(event, Chargeback) and event.account_id == ACCOUNT

    def test_a_dispute_that_names_no_order_is_refused(self) -> None:
        payload = with_(DISPUTE_CREATED, order=None, transaction=None)
        with pytest.raises(InvalidWebhookError, match="names no order"):
            creem().parse_webhook(*signed(payload))


@pytest.mark.parametrize(
    "event_type", ["subscription.paid", "subscription.canceled", "credits.granted"]
)
def test_any_other_event_is_unknown(event_type: str) -> None:
    payload = {"id": "evt_x", "eventType": event_type, "object": {"id": "sub_1"}}
    [event] = creem().parse_webhook(*signed(payload))
    assert event == Unknown(event_id="evt_x", provider_type=event_type)


# ── Checkout ───────────────────────────────────────────────────────────


class Recorder:
    """A fake Creem API: answers with ``status`` and ``body``; keeps requests."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status = status
        self.body = (
            {
                "id": "ch_1QyIQDw9cbFWdA1ry5Qc6I",
                "object": "checkout",
                "status": "pending",
                "product": "prod_starter",
                "units": 1,
                "checkout_url": "https://checkout.creem.io/ch_1QyIQDw9cbFWdA1ry5Qc6I",
                "mode": "test",
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
        checkout = await creem(api).create_checkout(checkout_request())
        assert checkout.checkout_id == "ch_1QyIQDw9cbFWdA1ry5Qc6I"
        assert checkout.url == "https://checkout.creem.io/ch_1QyIQDw9cbFWdA1ry5Qc6I"
        [sent] = api.requests
        assert (sent.method, str(sent.url)) == (
            "POST",
            "https://test-api.creem.io/v1/checkouts",
        )
        assert sent.headers["x-api-key"] == TEST_KEY
        assert json.loads(sent.content) == {
            "product_id": "prod_starter",
            "units": 1,
            "success_url": "https://campus.example/t/billing?checkout=success",
            "metadata": {"account_id": str(ACCOUNT), "package_id": "starter"},
        }

    async def test_the_live_host_only_when_named(self) -> None:
        api = Recorder()
        await creem(api, api_base=LIVE_API_BASE).create_checkout(checkout_request())
        assert str(api.requests[0].url) == "https://api.creem.io/v1/checkouts"

    async def test_a_package_without_a_creem_product_is_unavailable(self) -> None:
        api = Recorder()
        with pytest.raises(CheckoutUnavailableError, match="no product"):
            await creem(api).create_checkout(checkout_request(provider_item=None))
        assert api.requests == []

    async def test_a_product_creem_does_not_know_is_unavailable(self) -> None:
        api = Recorder(404, {"trace_id": "t-1", "message": "Not Found"})
        with pytest.raises(CheckoutUnavailableError, match="knows no product"):
            await creem(api).create_checkout(checkout_request())

    @pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 502, 503])
    async def test_a_refusal_or_failure_is_a_provider_error_asked_once(
        self, status: int
    ) -> None:
        api = Recorder(status, {"trace_id": "t-2"})
        with pytest.raises(PaymentProviderError, match=str(status)):
            await creem(api).create_checkout(checkout_request())
        assert len(api.requests) == 1  # no retries inside the adapter

    @pytest.mark.parametrize(
        "body", [b"<html>", {"id": "ch_1"}, {"checkout_url": "https://x"}]
    )
    async def test_an_answer_without_id_or_link_is_a_provider_error(
        self, body: Any
    ) -> None:
        with pytest.raises(PaymentProviderError):
            await creem(Recorder(200, body)).create_checkout(checkout_request())

    @pytest.mark.parametrize(
        "error",
        [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.ConnectError("c")],
    )
    async def test_the_network_failing_is_a_provider_error(
        self, error: httpx.HTTPError
    ) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise error

        with pytest.raises(PaymentProviderError, match=type(error).__name__):
            await creem(handler).create_checkout(checkout_request())
        assert calls == 1

    async def test_the_key_and_the_secret_are_never_logged(self) -> None:
        provider = creem(Recorder(500, {"trace_id": "t-3"}))
        with capture_logs() as logs, pytest.raises(PaymentProviderError):
            await provider.create_checkout(checkout_request())
        assert logs, "the failure is logged"
        text = repr(logs) + repr(provider)
        assert TEST_KEY not in text and TEST_SECRET not in text


async def test_creem_refunds_in_its_dashboard_only() -> None:
    request = RefundRequest(
        batch_id=uuid.uuid4(),
        transaction_id=ORDER,
        line_id=ORDER,
        units=10,
        amount_minor=100,
        currency="EUR",
    )
    with pytest.raises(RefundUnavailableError, match="dashboard"):
        await creem().request_refund(request)


# ── Settings and the registry ──────────────────────────────────────────


def test_without_its_key_and_secret_creem_stops_the_startup() -> None:
    settings = Settings(
        environment=Environment.TESTING, billing_providers=[CREEM]
    ).model_copy(update={"environment": Environment.PRODUCTION})
    with pytest.raises(PaymentConfigError, match="CREEM_API_KEY, CREEM_WEBHOOK_SECRET"):
        build_registry(settings)
    half = settings.model_copy(update={"creem_api_key": SecretStr(TEST_KEY)})
    with pytest.raises(PaymentConfigError, match="CREEM_WEBHOOK_SECRET"):
        build_registry(half)


@pytest.mark.parametrize(
    "environment",
    [Environment.PRODUCTION, Environment.STAGING, Environment.DEVELOPMENT],
)
def test_with_them_creem_is_switched_on_on_the_test_host(
    environment: Environment,
) -> None:
    settings = Settings(
        environment=Environment.TESTING,
        billing_providers=[CREEM],
        creem_api_key=TEST_KEY,
        creem_webhook_secret=TEST_SECRET,
    ).model_copy(update={"environment": environment})
    assert settings.creem_api_base == TEST_API_BASE
    provider = build_registry(settings).get(CREEM)
    assert isinstance(provider, CreemProvider)
    assert TEST_API_BASE in repr(provider)


def test_the_api_host_is_creems_test_or_live_one_only() -> None:
    assert Settings(creem_api_base=LIVE_API_BASE).creem_api_base == LIVE_API_BASE
    with pytest.raises(ValidationError):
        Settings(creem_api_base="https://creem.example/v1")  # type: ignore[arg-type]
