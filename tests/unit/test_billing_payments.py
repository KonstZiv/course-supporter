"""The payment port without a database (BIL-PAY).

The fake provider's signature and events, the registry of adapters — the
fake never registered in production — the normalized events' rules, the
refund request's figure, the checkout service's refusals, and the lock that
no ``paddle_*`` name is left in ``src/``. The database side is
``tests/integration/test_billing_payments_db.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from course_supporter.billing.errors import BillingInvalidError
from course_supporter.billing.kinds import EventKind
from course_supporter.billing.payments.catalog import Catalog, Package
from course_supporter.billing.payments.checkout import (
    CheckoutNotFoundError,
    PaymentUnavailableError,
    return_urls,
    start_checkout,
)
from course_supporter.billing.payments.fake import (
    FAKE,
    SIGNATURE_HEADER,
    FakeProvider,
)
from course_supporter.billing.payments.invoice import INVOICE_PROVIDER
from course_supporter.billing.payments.port import (
    Chargeback,
    Checkout,
    CheckoutRequest,
    CheckoutUnavailableError,
    InvalidWebhookError,
    PurchaseCompleted,
    RefundApproved,
    RefundRejected,
    Unknown,
    refund_request,
)
from course_supporter.billing.payments.registry import (
    PaymentConfigError,
    ProviderRegistry,
    adapters,
    build_registry,
)
from course_supporter.config import Environment, Settings
from course_supporter.storage.orm import BillingBatch

SRC = Path(__file__).resolve().parents[2] / "src"
ACCOUNT = uuid.uuid4()
STARTER = Package(id="starter", units=1000, prices_minor={"EUR": 990}, active=True)
RETIRED = Package(id="retired", units=10, prices_minor={"EUR": 100}, active=False)
CATALOG = Catalog(packages=(STARTER, RETIRED), providers={FAKE: {"starter": "x"}})


def _request(package: Package = STARTER) -> CheckoutRequest:
    return CheckoutRequest(
        account_id=ACCOUNT,
        package=package,
        provider_item=None,
        language="ukr",
        success_url="https://campus.example/t/billing?checkout=success",
        cancel_url="https://campus.example/t/billing?checkout=cancelled",
    )


# ── The fake provider ──────────────────────────────────────────────────


class TestFakeSignature:
    def test_a_signed_body_reads(self) -> None:
        fake = FakeProvider()
        headers, body = fake.webhook("something.else", {}, event_id="evt_1")
        [event] = fake.parse_webhook(headers, body)
        assert event == Unknown(
            event_id="evt_1",
            provider_type="something.else",
            occurred_at=event.occurred_at,
        )

    @pytest.mark.parametrize(
        "headers",
        [{}, {SIGNATURE_HEADER: ""}, {SIGNATURE_HEADER: "0" * 64}],
        ids=["absent", "empty", "wrong"],
    )
    def test_without_a_valid_signature_it_is_refused(
        self, headers: dict[str, str]
    ) -> None:
        fake = FakeProvider()
        _, body = fake.webhook("purchase.completed", {})
        with pytest.raises(InvalidWebhookError, match="signature"):
            fake.parse_webhook(headers, body)

    def test_a_body_changed_after_signing_is_refused(self) -> None:
        fake = FakeProvider()
        headers, body = fake.webhook("refund.approved", {"adjustment_id": "a"})
        with pytest.raises(InvalidWebhookError, match="wrong signature"):
            fake.parse_webhook(headers, body.replace(b'"a"', b'"b"'))

    def test_another_secret_is_refused(self) -> None:
        headers, body = FakeProvider(secret=b"other").webhook("x", {})
        with pytest.raises(InvalidWebhookError):
            FakeProvider().parse_webhook(headers, body)

    @pytest.mark.parametrize(
        "body",
        [b"not json", b"[]", b'{"event_type": "x"}', b"\xff\xfe"],
        ids=["text", "a list", "no event id", "not utf-8"],
    )
    def test_a_signed_body_of_another_shape_is_refused(self, body: bytes) -> None:
        fake = FakeProvider()
        headers = {SIGNATURE_HEADER: fake.sign(body)}
        with pytest.raises(InvalidWebhookError, match="not an event"):
            fake.parse_webhook(headers, body)


class TestFakeEvents:
    async def test_a_checkout_comes_back_paid_with_its_account(self) -> None:
        """The account id goes to the provider as custom data and returns in
        the purchase's webhook."""
        fake = FakeProvider()
        checkout = await fake.create_checkout(_request())
        assert checkout.url.endswith(checkout.checkout_id)
        assert ".invalid/" in checkout.url
        headers, body = fake.purchase_webhook(checkout.checkout_id, event_id="e1")
        [event] = fake.parse_webhook(headers, body)
        assert isinstance(event, PurchaseCompleted)
        assert (event.account_id, event.package_id) == (ACCOUNT, "starter")
        assert (event.amount_paid_minor, event.currency) == (990, "EUR")
        assert event.transaction_id.startswith("fake_txn_")
        assert event.line_id.startswith("fake_line_")
        assert event.kind is EventKind.PURCHASE_COMPLETED
        assert event.occurred_at is not None and event.occurred_at.tzinfo

    def test_refund_decisions(self) -> None:
        fake = FakeProvider()
        for approved, kind in ((True, RefundApproved), (False, RefundRejected)):
            [event] = fake.parse_webhook(*fake.refund_webhook("adj", approved=approved))
            assert isinstance(event, kind)
            assert event.adjustment_id == "adj"

    def test_a_chargeback(self) -> None:
        fake = FakeProvider()
        headers, body = fake.webhook(
            "chargeback.created",
            {
                "custom_data": {"account_id": str(ACCOUNT)},
                "transaction_id": "txn",
                "amount_minor": 990,
                "currency": "EUR",
            },
        )
        [event] = fake.parse_webhook(headers, body)
        assert isinstance(event, Chargeback)
        assert (event.account_id, event.amount_minor) == (ACCOUNT, 990)

    @pytest.mark.parametrize("account", [None, "", "not-a-uuid"])
    def test_a_purchase_naming_no_account_reads_as_none(
        self, account: str | None
    ) -> None:
        fake = FakeProvider()
        data = {
            "custom_data": {"account_id": account, "package_id": "starter"},
            "transaction_id": "t",
            "line_id": "l",
            "amount_minor": 1,
            "currency": "EUR",
        }
        [event] = fake.parse_webhook(*fake.webhook("purchase.completed", data))
        assert isinstance(event, PurchaseCompleted) and event.account_id is None

    def test_a_purchase_naming_no_package_is_refused(self) -> None:
        fake = FakeProvider()
        data = {
            "custom_data": {"account_id": str(ACCOUNT)},
            "transaction_id": "t",
            "line_id": "l",
            "amount_minor": 1,
            "currency": "EUR",
        }
        with pytest.raises(InvalidWebhookError, match="names no package"):
            fake.parse_webhook(*fake.webhook("purchase.completed", data))

    async def test_a_refund_gets_an_adjustment_id(self) -> None:
        fake = FakeProvider()
        batch = _batch()
        adjustment = await fake.request_refund(refund_request(batch, 25))
        assert adjustment.startswith("fake_adj_")
        assert fake.refunds[adjustment].amount_minor == 247


# ── Normalized events ──────────────────────────────────────────────────


def _purchase(**overrides: Any) -> PurchaseCompleted:
    fields: dict[str, Any] = {
        "event_id": "evt",
        "provider_type": "transaction.completed",
        "occurred_at": datetime(2026, 11, 12, tzinfo=UTC),
        "account_id": ACCOUNT,
        "package_id": "starter",
        "amount_paid_minor": 990,
        "currency": "EUR",
        "transaction_id": "txn",
        "line_id": "line",
    }
    return PurchaseCompleted(**(fields | overrides))


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"event_id": ""}, "event id"),
        ({"event_id": "e" * 65}, "event id"),
        ({"line_id": "l" * 65}, "line id"),
        ({"transaction_id": ""}, "transaction id"),
        ({"provider_type": ""}, "provider's type"),
        ({"amount_paid_minor": -1}, "negative"),
        ({"currency": "eur"}, "ISO 4217"),
        ({"occurred_at": datetime(2026, 11, 12)}, "zone"),
    ],
)
def test_an_event_that_breaks_its_shape_is_invalid(
    overrides: dict[str, Any], error: str
) -> None:
    with pytest.raises(InvalidWebhookError, match=error):
        _purchase(**overrides)


def test_an_event_id_of_64_characters_is_kept() -> None:
    assert _purchase(event_id="e" * 64).event_id == "e" * 64


# ── Refund request ─────────────────────────────────────────────────────


def _batch(**overrides: Any) -> BillingBatch:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "account_id": ACCOUNT,
        "source": "purchase",
        "units": 100,
        "created_at": datetime(2026, 11, 1, tzinfo=UTC),
        "expires_at": datetime(2026, 11, 1, tzinfo=UTC) + timedelta(days=180),
        "amount_paid_minor": 990,
        "currency": "EUR",
        "provider": FAKE,
        "provider_transaction_id": "txn",
        "provider_line_id": "line",
    }
    return BillingBatch(**(fields | overrides))


def test_a_refund_request_carries_the_journals_figure() -> None:
    request = refund_request(_batch(), 33)
    # floor(990 x 33 / 100) = 326 (326.7 rounded down)
    assert (request.amount_minor, request.currency, request.units) == (326, "EUR", 33)
    assert (request.transaction_id, request.line_id) == ("txn", "line")


@pytest.mark.parametrize(
    ("overrides", "units", "error"),
    [
        (
            {
                "source": "admin_credit",
                "amount_paid_minor": None,
                "currency": None,
                "provider": None,
                "provider_transaction_id": None,
                "provider_line_id": None,
            },
            10,
            "never refunded",
        ),
        ({}, 101, "1 to 100 units"),
        (
            {"provider_transaction_id": None, "provider_line_id": None},
            10,
            "no transaction",
        ),
    ],
)
def test_a_refund_request_is_refused(
    overrides: dict[str, Any], units: int, error: str
) -> None:
    with pytest.raises(BillingInvalidError, match=error):
        refund_request(_batch(**overrides), units)


# ── Registry ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("environment", [Environment.PRODUCTION, Environment.STAGING])
def test_the_fake_is_never_registered_outside_development_and_testing(
    environment: Environment,
) -> None:
    """Lock: the fake's secret is in the code; production never has it."""
    assert FAKE not in adapters(environment)
    settings = Settings(environment=Environment.TESTING, billing_providers=[FAKE])
    settings_there = settings.model_copy(update={"environment": environment})
    with pytest.raises(PaymentConfigError, match="no provider"):
        build_registry(settings_there)


@pytest.mark.parametrize("environment", [Environment.DEVELOPMENT, Environment.TESTING])
def test_the_fake_is_there_for_development_and_tests(environment: Environment) -> None:
    settings = Settings(environment=environment, billing_providers=[FAKE])
    registry = build_registry(settings)
    assert registry.names == (FAKE,)
    assert isinstance(registry.get(FAKE), FakeProvider)


def test_no_provider_is_switched_on_by_default() -> None:
    settings = Settings(environment=Environment.TESTING)
    assert settings.billing_providers == []
    registry = build_registry(settings)
    assert len(registry) == 0
    assert registry.default() is None
    assert registry.get(FAKE) is None


@pytest.mark.parametrize(
    ("names", "error"),
    [(["paddle"], "names \\['paddle'\\]"), ([FAKE, FAKE], "switched on twice")],
)
def test_a_bad_setting_is_refused(names: list[str], error: str) -> None:
    settings = Settings(environment=Environment.TESTING, billing_providers=names)
    with pytest.raises(PaymentConfigError, match=error):
        build_registry(settings)


@pytest.mark.parametrize("name", [INVOICE_PROVIDER, "Fake", ""])
def test_a_provider_cannot_take_a_reserved_or_bad_name(name: str) -> None:
    provider: Any = SimpleNamespace(name=name)  # the registry reads the name only
    with pytest.raises(PaymentConfigError, match="cannot name"):
        ProviderRegistry([provider])


# ── Checkout service ───────────────────────────────────────────────────


async def _start(
    registry: ProviderRegistry, package_id: str = "starter", provider: str | None = None
) -> Any:
    return await start_checkout(
        registry=registry,
        catalog=CATALOG,
        account_id=ACCOUNT,
        package_id=package_id,
        provider_name=provider,
        language="ukr",
        return_page="https://campus.example/t/billing/",
    )


async def test_a_checkout_through_the_default_provider() -> None:
    fake = FakeProvider()
    started = await _start(ProviderRegistry([fake]))
    assert started.provider == FAKE
    request = fake.checkouts[started.checkout_id]
    assert (request.account_id, request.package, request.provider_item) == (
        ACCOUNT,
        STARTER,
        "x",
    )
    assert request.language == "ukr"
    assert (request.success_url, request.cancel_url) == return_urls(
        "https://campus.example/t/billing"
    )
    assert request.success_url.endswith("/t/billing?checkout=success")


async def test_a_checkout_without_providers_is_unavailable() -> None:
    with pytest.raises(PaymentUnavailableError):
        await _start(ProviderRegistry())


@pytest.mark.parametrize(
    ("package", "provider", "error"),
    [
        ("nope", None, "'nope' is not on sale"),
        ("retired", None, "'retired' is not on sale"),
        ("starter", "paddle", "'paddle' is not switched on"),
    ],
)
async def test_a_checkout_of_nothing_on_sale_is_not_found(
    package: str, provider: str | None, error: str
) -> None:
    with pytest.raises(CheckoutNotFoundError, match=error):
        await _start(ProviderRegistry([FakeProvider()]), package, provider)


async def test_a_provider_that_cannot_sell_makes_payment_unavailable() -> None:
    class Unsellable(FakeProvider):
        async def create_checkout(self, request: CheckoutRequest) -> Checkout:
            raise CheckoutUnavailableError("no price for this package")

    with pytest.raises(PaymentUnavailableError, match="no price"):
        await _start(ProviderRegistry([Unsellable()]))


# ── Lock: the journal names no provider ────────────────────────────────


def test_src_has_no_paddle_names() -> None:
    """BIL-PAY: the journal is provider-neutral — no ``paddle_*`` identifier
    is left in ``src/`` (a provider is a value of ``provider``, never a
    column)."""
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1)
        if re.search(r"paddle_", line, re.IGNORECASE)
    ]
    assert offenders == []


def test_the_fake_signs_with_hmac_sha256_hex() -> None:
    body = json.dumps({"event_id": "e"}).encode()
    expected = hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert FakeProvider(secret=b"k").sign(body) == expected
