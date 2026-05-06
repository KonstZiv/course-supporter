"""Schema-acceptance + FK enforcement for ExternalServiceCall (KD5).

Mirrors the catalog-level pattern from ``test_job_redesign_db.py``
(0.3) — `inspect()` + `pg_constraint` queries verify the migration's
shape stuck. FK enforcement tests verify the runtime behavior (NULL
rejected, NO ACTION blocks Job DELETE).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.config import get_settings
from course_supporter.storage.orm import (
    CourseNode,
    ExternalServiceCall,
    Job,
    Tenant,
)

pytestmark = pytest.mark.requires_db


@pytest.fixture()
def sync_engine() -> Generator[Engine]:
    """Sync engine for catalog inspection."""
    engine = create_engine(get_settings().database_url)
    yield engine
    engine.dispose()


class TestSchemaShape:
    """Migration ``f3a4b5c6d7e8`` shipped the KD5 schema."""

    def test_tenant_id_column_dropped(self, sync_engine: Engine) -> None:
        col_names = {
            c["name"]
            for c in inspect(sync_engine).get_columns("external_service_calls")
        }
        assert "tenant_id" not in col_names

    def test_job_id_column_is_not_null(self, sync_engine: Engine) -> None:
        cols = {
            c["name"]: c
            for c in inspect(sync_engine).get_columns("external_service_calls")
        }
        assert "job_id" in cols
        assert cols["job_id"]["nullable"] is False

    def test_tenant_id_index_dropped(self, sync_engine: Engine) -> None:
        with sync_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'external_service_calls'"
                )
            ).all()
        names = {r[0] for r in rows}
        assert "ix_external_service_calls_tenant_id" not in names

    def test_fk_ondelete_is_no_action(self, sync_engine: Engine) -> None:
        """FK ``external_service_calls_job_id_fkey`` has ``ON DELETE NO ACTION``."""
        with sync_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint "
                    "WHERE conname = 'external_service_calls_job_id_fkey'"
                )
            ).first()
        assert row is not None, "FK constraint not found"
        # PostgreSQL confdeltype: 'a' = NO ACTION, 'r' = RESTRICT,
        # 'c' = CASCADE, 'n' = SET NULL, 'd' = SET DEFAULT.
        assert row[0] == "a"


class TestForeignKeyEnforcement:
    """Runtime FK behavior."""

    async def test_null_job_id_rejected(self, db_session: AsyncSession) -> None:
        """INSERT with NULL job_id violates the NOT NULL constraint."""
        esc = ExternalServiceCall(
            job_id=None,  # type: ignore[arg-type]
            provider="anthropic",
            model_id="claude-sonnet-4",
        )
        db_session.add(esc)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_no_action_blocks_job_hard_delete(
        self, db_session: AsyncSession
    ) -> None:
        """DELETE Job with attached ESC raises IntegrityError (NO ACTION)."""
        tenant = Tenant(name=f"esc-{uuid.uuid4().hex[:6]}")
        db_session.add(tenant)
        await db_session.flush()
        node = CourseNode(tenant_id=tenant.id, title="course", order=0)
        db_session.add(node)
        await db_session.flush()
        job = Job(tenant_id=tenant.id, course_node_id=node.id, job_type="ingest")
        db_session.add(job)
        await db_session.flush()
        esc = ExternalServiceCall(
            job_id=job.id,
            provider="anthropic",
            model_id="claude-sonnet-4",
            cost_usd=1.0,
        )
        db_session.add(esc)
        await db_session.flush()

        # Raw DELETE triggers the FK check inside ``execute`` — not at
        # flush time as ORM-tracked changes do.
        with pytest.raises(IntegrityError):
            await db_session.execute(Job.__table__.delete().where(Job.id == job.id))

    async def test_valid_job_id_accepted(self, db_session: AsyncSession) -> None:
        """ESC with a real Job FK persists cleanly."""
        tenant = Tenant(name=f"esc-{uuid.uuid4().hex[:6]}")
        db_session.add(tenant)
        await db_session.flush()
        job = Job(tenant_id=tenant.id, job_type="ingest")
        db_session.add(job)
        await db_session.flush()
        esc = ExternalServiceCall(
            job_id=job.id,
            provider="anthropic",
            model_id="claude-sonnet-4",
            cost_usd=2.5,
        )
        db_session.add(esc)
        await db_session.flush()
        await db_session.refresh(esc)
        assert esc.cost_usd == pytest.approx(2.5)
