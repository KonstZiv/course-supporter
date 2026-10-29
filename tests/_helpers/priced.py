"""A register price for test doubles that write rows (BIL-C).

A double that stands in for a router still writes a paid call's row, and since
BIL-C the database refuses a row whose sum does not name its kind
(``ck_esc_cost_kind_amount``) or whose exact sum does not name its catalog
version and tariff window (``ck_esc_exact_is_priced``). This is the price such a
double hands to ``_persist``: an exact sum under a test catalog, flat tariff.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from course_supporter.cost_kinds import CostKind, TariffWindow
from course_supporter.llm.pricing import PricedCall

TEST_CATALOG_VERSION = "test-catalog"


def priced_exact(cost_usd: float | str | Decimal) -> PricedCall:
    """An exact price of ``cost_usd`` dollars, as a real successful call leaves."""
    return PricedCall(
        cost_usd=Decimal(str(cost_usd)),
        cost_kind=CostKind.EXACT,
        catalog_version=TEST_CATALOG_VERSION,
        tariff_window=TariffWindow.FLAT,
        call_started_at=datetime.now(UTC),
    )


def priced_as(kind: CostKind, cost_usd: float | str | Decimal | None) -> PricedCall:
    """A price of the given kind; ``None`` for an unknown one."""
    return PricedCall(
        cost_usd=None if cost_usd is None else Decimal(str(cost_usd)),
        cost_kind=kind,
        catalog_version=TEST_CATALOG_VERSION,
        tariff_window=TariffWindow.FLAT,
        call_started_at=datetime.now(UTC),
    )
