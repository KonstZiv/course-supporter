"""BIL-B arithmetic without a database: calendar months, the prorated limit, refunds.

The journal on a live database is ``tests/integration/test_billing_ledger_db.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from course_supporter.billing.benefit import (
    SettingsChange,
    counts_for_limit,
    month_limit,
    setting_on,
)
from course_supporter.billing.errors import BillingInvalidError
from course_supporter.billing.journal import add_months
from course_supporter.billing.kinds import Benefit
from course_supporter.billing.refunds import refund_amount
from course_supporter.storage.orm import BillingBatch


class TestAddMonths:
    @pytest.mark.parametrize(
        ("at", "expected"),
        [
            (
                datetime(2026, 11, 5, 14, 30, tzinfo=UTC),
                datetime(2027, 5, 5, 14, 30, tzinfo=UTC),
            ),
            (datetime(2026, 8, 31, tzinfo=UTC), datetime(2027, 2, 28, tzinfo=UTC)),
            (datetime(2027, 8, 31, tzinfo=UTC), datetime(2028, 2, 29, tzinfo=UTC)),
            (datetime(2026, 12, 31, tzinfo=UTC), datetime(2027, 6, 30, tzinfo=UTC)),
            (datetime(2026, 7, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)),
        ],
    )
    def test_six_calendar_months_clamped_to_the_months_end(
        self, at: datetime, expected: datetime
    ) -> None:
        assert add_months(at, 6) == expected

    def test_the_zone_stays(self) -> None:
        kyiv = timezone(timedelta(hours=2))
        assert add_months(datetime(2026, 1, 31, 23, 0, tzinfo=kyiv), 1).tzinfo is kyiv


def _change(at: datetime, benefit: Benefit, limit: int | None = None) -> SettingsChange:
    return SettingsChange(at=at, benefit=benefit, monthly_limit_units=limit)


class TestMonthLimit:
    def test_no_history_no_limit(self) -> None:
        assert month_limit([], date(2026, 11, 1)) == 0
        assert setting_on([], datetime(2026, 11, 1, tzinfo=UTC)).benefit is Benefit.NONE

    def test_a_full_month(self) -> None:
        history = [
            _change(datetime(2026, 10, 20, tzinfo=UTC), Benefit.MONTHLY_LIMIT, 300)
        ]
        assert month_limit(history, date(2026, 11, 17)) == 300

    def test_the_day_granted_counts_for_the_new_setting(self) -> None:
        late = datetime(2026, 11, 5, 23, 59, tzinfo=UTC)
        history = [_change(late, Benefit.MONTHLY_LIMIT, 300)]
        assert month_limit(history, date(2026, 11, 1)) == 260  # 300 x 26 / 30
        assert month_limit(history, date(2026, 10, 1)) == 0

    def test_rounded_down(self) -> None:
        history = [
            _change(datetime(2026, 11, 30, tzinfo=UTC), Benefit.MONTHLY_LIMIT, 29)
        ]
        assert month_limit(history, date(2026, 11, 1)) == 0  # 29 / 30
        history = [
            _change(datetime(2026, 1, 2, tzinfo=UTC), Benefit.MONTHLY_LIMIT, 100)
        ]
        assert month_limit(history, date(2026, 1, 1)) == 96  # 100 x 30 / 31 = 96.8

    def test_a_change_mid_month(self) -> None:
        history = [
            _change(datetime(2025, 12, 1, tzinfo=UTC), Benefit.MONTHLY_LIMIT, 100),
            _change(datetime(2026, 1, 11, 10, tzinfo=UTC), Benefit.MONTHLY_LIMIT, 200),
        ]
        assert month_limit(history, date(2026, 1, 1)) == 167  # 5200 / 31

    def test_unlimited_and_none_days_bring_no_limit(self) -> None:
        history = [
            _change(datetime(2025, 12, 1, tzinfo=UTC), Benefit.UNLIMITED),
            _change(datetime(2026, 1, 11, tzinfo=UTC), Benefit.MONTHLY_LIMIT, 310),
            _change(datetime(2026, 1, 21, tzinfo=UTC), Benefit.NONE),
        ]
        assert month_limit(history, date(2026, 1, 1)) == 100  # 10 x 310 / 31

    def test_a_unit_counts_by_its_days_setting(self) -> None:
        history = [
            _change(datetime(2026, 1, 1, tzinfo=UTC), Benefit.UNLIMITED),
            _change(datetime(2026, 1, 11, 18, tzinfo=UTC), Benefit.MONTHLY_LIMIT, 310),
        ]
        assert not counts_for_limit(history, datetime(2026, 1, 10, 9, tzinfo=UTC))
        # 11 January belongs to monthly_limit, the morning included.
        assert counts_for_limit(history, datetime(2026, 1, 11, 9, tzinfo=UTC))


def _batch(
    source: str = "purchase", units: int = 300, amount: int | None = 1000
) -> BillingBatch:
    return BillingBatch(
        id=uuid.uuid4(), source=source, units=units, amount_paid_minor=amount
    )


class TestRefundAmount:
    @pytest.mark.parametrize(
        ("units", "amount", "refund", "expected"),
        [
            (300, 1000, 100, 333),  # 333.33 -> 333
            (300, 1000, 300, 1000),
            (100, 1999, 40, 799),  # 799.6 -> 799, down to the cent
            (3, 100, 1, 33),
            (100, 0, 50, 0),
        ],
    )
    def test_proportional_rounded_down_to_a_cent(
        self, units: int, amount: int, refund: int, expected: int
    ) -> None:
        assert refund_amount(_batch(units=units, amount=amount), refund) == expected

    def test_an_admin_credit_is_never_refunded(self) -> None:
        with pytest.raises(BillingInvalidError, match="never refunded"):
            refund_amount(_batch(source="admin_credit", amount=None), 10)

    @pytest.mark.parametrize("units", [0, -1, 301])
    def test_within_the_batch(self, units: int) -> None:
        with pytest.raises(BillingInvalidError, match="1 to 300"):
            refund_amount(_batch(), units)
