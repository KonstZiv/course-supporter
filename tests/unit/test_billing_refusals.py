"""The reason codes of the billing routes, without a database (BIL-PAY).

One code, one status; the answer's form is the project's coded refusal; the
screens' purchase reasons are the very codes a checkout refuses with.
"""

from __future__ import annotations

import pytest

from course_supporter.api.routes._billing_shared import (
    STATUS,
    Refusal,
    refusal,
    responses,
)
from course_supporter.billing.payments.checkout import PurchaseClosed


def test_every_code_has_its_status() -> None:
    assert set(STATUS) == set(Refusal)


def test_the_codes_and_statuses() -> None:
    assert {code.value: status for code, status in STATUS.items()} == {
        "payment_unavailable": 503,
        "purchase_not_open": 403,
        "terms_consent_required": 409,
        "terms_changed": 409,
        "package_not_found": 404,
        "provider_not_found": 404,
        "provider_unavailable": 502,
        "checkout_not_found": 404,
        "invalid_cursor": 422,
        "invalid_period": 422,
        "numbers_unavailable": 503,
    }


def test_the_purchase_reasons_are_refusal_codes() -> None:
    assert {r.value for r in PurchaseClosed} <= {c.value for c in Refusal}


@pytest.mark.parametrize("code", list(Refusal))
def test_the_answer_is_the_coded_refusal(code: Refusal) -> None:
    exc = refusal(code, "what happened")
    assert exc.status_code == STATUS[code]
    assert exc.detail == {"code": code.value, "details": "what happened"}


def test_the_openapi_responses_name_the_codes() -> None:
    found = responses(Refusal.TERMS_CONSENT_REQUIRED, Refusal.TERMS_CHANGED)
    assert list(found) == [409]
    assert "terms_consent_required" in found[409]["description"]
    assert "terms_changed" in found[409]["description"]
