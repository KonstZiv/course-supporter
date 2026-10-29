"""The price columns of the call register, as the database holds them (BIL-C).

Requires ``docker compose up -d`` (PostgreSQL), migrated to head.

Two things only a live database answers:

* the comments the migration copied by hand are the ORM's — ``test_schema_sync``
  compares tables and columns, not comments (``DD-L4-A``; the register table's
  older columns keep their drift, so only the columns this task added or
  changed are compared);
* the rules a priced row must keep are the DATABASE's: each ``CHECK`` refuses
  the one row that breaks it and admits every member of the vocabulary it is
  built from. A rule held by the database cannot be proven by mutating the
  code — only a mutation of the schema (dropping the constraint from the
  migration) turns these red.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Table, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.cost_kinds import CostKind, TariffWindow
from course_supporter.storage.orm import ExternalServiceCall, Job, Tenant

pytestmark = pytest.mark.requires_db

_TABLE: Table = ExternalServiceCall.__table__  # type: ignore[assignment]

_BIL_C_COLUMNS = (
    "cost_usd",
    "cost_kind",
    "unit_in_cached",
    "unit_in_estimate",
    "unit_out_estimate",
    "catalog_version",
    "tariff_window",
    "call_started_at",
    "price_in_per_1k_usd",
    "price_in_cached_per_1k_usd",
    "price_out_per_1k_usd",
    "price_per_minute_usd",
    "unit_out_reasoning",
)

_MONEY_COLUMNS = (
    "cost_usd",
    "ceiling_estimate_usd",
    "price_in_per_1k_usd",
    "price_in_cached_per_1k_usd",
    "price_out_per_1k_usd",
    "price_per_minute_usd",
)


async def _column_comments(session: AsyncSession) -> dict[str, str | None]:
    result = await session.execute(
        text(
            "SELECT a.attname, col_description(a.attrelid, a.attnum) "
            "FROM pg_attribute a "
            "WHERE a.attrelid = CAST(:table AS regclass) "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": _TABLE.name},
    )
    return {str(name): comment for name, comment in result.all()}


class TestCommentsAndTypes:
    @pytest.mark.parametrize("column", _BIL_C_COLUMNS)
    async def test_comment_is_the_orms(
        self, db_session: AsyncSession, column: str
    ) -> None:
        live = await _column_comments(db_session)
        assert live[column] == _TABLE.c[column].comment

    @pytest.mark.parametrize("column", _MONEY_COLUMNS)
    async def test_money_is_numeric_18_12(
        self, db_session: AsyncSession, column: str
    ) -> None:
        result = await db_session.execute(
            text(
                "SELECT data_type, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE.name, "column": column},
        )
        assert tuple(result.one()) == ("numeric", 18, 12)


async def _job_id(session: AsyncSession) -> uuid.UUID:
    tenant = Tenant(name=f"bilc-{uuid.uuid4().hex[:6]}")
    session.add(tenant)
    await session.flush()
    job = Job(tenant_id=tenant.id, job_type="document_processing")
    session.add(job)
    await session.flush()
    return job.id


def _exact_token_row(job_id: uuid.UUID, **overrides: Any) -> ExternalServiceCall:
    """A row every CHECK admits: an exact, cached-aware token call."""
    row: dict[str, Any] = {
        "job_id": job_id,
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "success": True,
        "unit_type": "tokens",
        "unit_in": 100,
        "unit_in_cached": 60,
        "unit_out": 10,
        "cost_usd": Decimal("0.000123"),
        "cost_kind": CostKind.EXACT.value,
        "catalog_version": "2026-10-29.1",
        "tariff_window": TariffWindow.PEAK.value,
        "price_in_per_1k_usd": Decimal("0.0003"),
        "price_out_per_1k_usd": Decimal("0.0012"),
    }
    row.update(overrides)
    return ExternalServiceCall(**row)


class TestTheValidRowIsAdmitted:
    async def test_exact_token_row(self, db_session: AsyncSession) -> None:
        db_session.add(_exact_token_row(await _job_id(db_session)))
        await db_session.flush()

    @pytest.mark.parametrize(
        ("kind", "cost"),
        [
            (CostKind.EXACT, Decimal(0)),
            (CostKind.ESTIMATED, Decimal("0.5")),
            (CostKind.UNKNOWN, None),
        ],
    )
    async def test_every_cost_kind(
        self, db_session: AsyncSession, kind: CostKind, cost: Decimal | None
    ) -> None:
        db_session.add(
            _exact_token_row(
                await _job_id(db_session), cost_kind=kind.value, cost_usd=cost
            )
        )
        await db_session.flush()

    @pytest.mark.parametrize("window", list(TariffWindow))
    async def test_every_tariff_window(
        self, db_session: AsyncSession, window: TariffWindow
    ) -> None:
        db_session.add(
            _exact_token_row(await _job_id(db_session), tariff_window=window.value)
        )
        await db_session.flush()

    async def test_row_of_no_paid_call_carries_no_kind(
        self, db_session: AsyncSession
    ) -> None:
        # A webhook delivery: a call, but nothing the register prices.
        db_session.add(
            ExternalServiceCall(
                job_id=await _job_id(db_session),
                provider="httpx",
                model_id="webhook_post",
                success=True,
            )
        )
        await db_session.flush()

    async def test_twelve_places_are_kept(self, db_session: AsyncSession) -> None:
        row = _exact_token_row(
            await _job_id(db_session), cost_usd=Decimal("0.000000000029")
        )
        db_session.add(row)
        await db_session.flush()
        await db_session.refresh(row)
        assert row.cost_usd == Decimal("0.000000000029")


# Each case breaks exactly one CHECK; every other column keeps the valid row.
_BREAKS: list[tuple[str, dict[str, Any]]] = [
    ("ck_esc_cost_kind", {"cost_kind": "free", "cost_usd": None, "unit_type": None}),
    ("ck_esc_tariff_window", {"tariff_window": "dusk"}),
    ("ck_esc_cost_kind_amount", {"cost_kind": CostKind.UNKNOWN.value}),
    ("ck_esc_cost_kind_amount", {"cost_usd": None}),
    ("ck_esc_cost_kind_amount", {"cost_kind": None, "unit_type": None}),
    ("ck_esc_priced_call", {"cost_kind": None, "cost_usd": None}),
    ("ck_esc_exact_is_priced", {"catalog_version": None}),
    ("ck_esc_exact_is_priced", {"tariff_window": None}),
    ("ck_esc_cached_within_input", {"unit_in_cached": 101}),
    ("ck_esc_cached_within_input", {"unit_in_cached": -1}),
    ("ck_esc_cached_within_input", {"unit_in": None}),
]


class TestEachCheckRefusesItsRow:
    @pytest.mark.parametrize(
        ("constraint", "overrides"),
        _BREAKS,
        ids=[f"{name}-{'-'.join(o)}" for name, o in _BREAKS],
    )
    async def test_refused(
        self, db_session: AsyncSession, constraint: str, overrides: dict[str, Any]
    ) -> None:
        db_session.add(_exact_token_row(await _job_id(db_session), **overrides))
        with pytest.raises(IntegrityError, match=constraint):
            await db_session.flush()
