"""Shadow estimates in the database (BIL-D): the write, the table, the migration."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Table, create_engine, delete, inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.config import get_settings
from course_supporter.cost_estimate import shadow
from course_supporter.cost_estimate.core import ActionEstimate, ActionKind
from course_supporter.cost_estimate.settings import EstimateSettings
from course_supporter.storage.orm import ActionCostEstimate, Tenant

pytestmark = pytest.mark.requires_db

_TABLE: Table = ActionCostEstimate.__table__  # type: ignore[assignment]


def _estimate(_: EstimateSettings, at: datetime) -> ActionEstimate:
    return ActionEstimate.of(
        ActionKind.PROCESSING,
        [],
        catalog_version="2026-10-30.2",
        inputs={"at": at.isoformat()},
        uncalibrated=True,
    )


@pytest.fixture()
async def key(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[str]:
    """A fresh action key; its rows are removed afterwards (the write commits)."""
    value = f"test-{uuid.uuid4()}"
    yield value
    async with session_factory() as session:
        await session.execute(
            delete(ActionCostEstimate).where(ActionCostEstimate.action_key == value)
        )
        await session.commit()


class TestTheWrite:
    async def test_one_row_per_action(
        self, session_factory: async_sessionmaker[AsyncSession], key: str
    ) -> None:
        first = await shadow.record_estimate(
            _estimate, action_key=key, session_factory=session_factory
        )
        again = await shadow.record_estimate(
            _estimate, action_key=key, session_factory=session_factory
        )
        assert (first, again) == (True, False)
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ActionCostEstimate).where(
                            ActionCostEstimate.action_key == key
                        )
                    )
                )
                .scalars()
                .all()
            )
        [row] = rows
        assert row.action_kind == "processing"
        assert row.e_usd == Decimal(0)
        assert row.margin == Decimal("0.2")
        assert (row.units_per_usd, row.p_units, row.reserve_units) == (
            Decimal(1700),
            0,
            0,
        )
        assert row.uncalibrated is True
        assert row.breakdown == []

    async def test_a_failing_estimate_writes_nothing(
        self, session_factory: async_sessionmaker[AsyncSession], key: str
    ) -> None:
        def broken(_: EstimateSettings, __: datetime) -> ActionEstimate:
            raise RuntimeError("broken on purpose")

        assert not await shadow.record_estimate(
            broken, action_key=key, session_factory=session_factory
        )
        async with session_factory() as session:
            count = await session.scalar(
                select(ActionCostEstimate.id).where(
                    ActionCostEstimate.action_key == key
                )
            )
        assert count is None


class TestTheTable:
    async def test_no_foreign_keys(self, db_session: AsyncSession) -> None:
        """Ids are plain columns: no delete can take the row or be blocked by it.

        ``ON DELETE SET NULL`` on several of them made Postgres fail a tenant's
        cascading delete (the row reached by two paths in one statement).
        """
        count = await db_session.scalar(
            text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conrelid = 'action_cost_estimates'::regclass AND contype = 'f'"
            )
        )
        assert count == 0

    async def test_deleting_the_account_keeps_the_estimate(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        row = ActionCostEstimate(
            action_kind="review",
            action_key=f"test-{uuid.uuid4()}",
            tenant_id=seed_tenant.id,
            e_usd=Decimal("0.1"),
            margin=Decimal("0.2"),
            breakdown=[],
            inputs={},
            uncalibrated=True,
        )
        db_session.add(row)
        await db_session.flush()
        await db_session.execute(
            text("DELETE FROM tenants WHERE id = :id"), {"id": seed_tenant.id}
        )
        await db_session.refresh(row)
        assert row.tenant_id == seed_tenant.id  # kept, as the action key is

    async def test_units_need_a_rate(self, db_session: AsyncSession) -> None:
        db_session.add(
            ActionCostEstimate(
                action_kind="review",
                action_key=f"test-{uuid.uuid4()}",
                e_usd=Decimal("0.1"),
                p_units=12,
                margin=Decimal("0.2"),
                breakdown=[],
                inputs={},
                uncalibrated=True,
                created_at=datetime.now(UTC),
            )
        )
        with pytest.raises(Exception, match="ck_action_cost_estimates_units_need_rate"):
            await db_session.flush()

    async def test_comments_are_the_orms(self, db_session: AsyncSession) -> None:
        rows = (
            await db_session.execute(
                text(
                    "SELECT a.attname, col_description(a.attrelid, a.attnum) "
                    "FROM pg_attribute a WHERE a.attrelid = "
                    "'action_cost_estimates'::regclass AND a.attnum > 0 "
                    "AND NOT a.attisdropped"
                )
            )
        ).all()
        live: dict[str, str | None] = {name: comment for name, comment in rows}
        assert live == {c.name: c.comment for c in _TABLE.c}
        table = await db_session.scalar(
            text("SELECT obj_description('action_cost_estimates'::regclass)")
        )
        assert table == _TABLE.comment


def test_the_migration_goes_down_and_up() -> None:
    """Downgrade drops the table, upgrade brings it back (a single head)."""
    cfg = Config("alembic.ini")
    engine = create_engine(get_settings().database_url)
    try:
        command.downgrade(cfg, "course_link_candidates")
        with engine.connect() as conn:
            assert "action_cost_estimates" not in inspect(conn).get_table_names()
    finally:
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert "action_cost_estimates" in inspect(conn).get_table_names()
        # The head moves on with later migrations; it is still one head.
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == (
            ScriptDirectory.from_config(cfg).get_current_head()
        )
    engine.dispose()
