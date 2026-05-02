"""Integration tests for the 0.3 Job schema redesign (vision §3 KD13).

Verifies the post-migration on-disk shape of the ``jobs`` table:

* ``current_stage`` (varchar) and ``stage_progress`` (jsonb) columns exist.
* ``estimated_at`` is dropped.
* ``course_node_id`` is renamed to ``course_node_id`` — column, FK
  constraint (``jobs_course_node_id_fkey``), and index
  (``ix_jobs_course_node_id``) all carry the new names.

ORM-level assertions live in the unit suite; this file confirms
the migration actually shipped these changes (a class of bug where
the migration is missing or partial would slip past the unit tests).

Plus a JSONB round-trip: ``stage_progress`` must persist nested
structures verbatim because reactivate-resume reads back exactly
what the worker wrote on its last checkpoint (KD4a + KD13).
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
from course_supporter.storage.orm import CourseNode, Job, Tenant

pytestmark = pytest.mark.requires_db


@pytest.fixture()
def sync_engine() -> Generator[Engine]:
    """Sync engine for catalog inspection (pg_constraint, pg_indexes)."""
    engine = create_engine(get_settings().database_url)
    yield engine
    engine.dispose()


class TestSchemaShape:
    """Migration ``e2f3a4b5c6d7`` shipped the expected schema."""

    def test_current_stage_column_exists(self, sync_engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(sync_engine).get_columns("jobs")}
        assert "current_stage" in cols
        # VARCHAR(50) per migration
        assert cols["current_stage"]["nullable"] is True

    def test_stage_progress_column_exists_as_jsonb(self, sync_engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(sync_engine).get_columns("jobs")}
        assert "stage_progress" in cols
        assert cols["stage_progress"]["nullable"] is True
        # JSONB type (psycopg / SQLA reports it as JSONB)
        assert "JSON" in str(cols["stage_progress"]["type"]).upper()

    def test_estimated_at_column_dropped(self, sync_engine: Engine) -> None:
        col_names = {c["name"] for c in inspect(sync_engine).get_columns("jobs")}
        assert "estimated_at" not in col_names

    def test_course_node_id_column_renamed(self, sync_engine: Engine) -> None:
        col_names = {c["name"] for c in inspect(sync_engine).get_columns("jobs")}
        assert "course_node_id" in col_names
        assert "course_node_id" not in col_names

    def test_fk_constraint_renamed(self, sync_engine: Engine) -> None:
        """``jobs_course_node_id_fkey`` exists; legacy name does not."""
        with sync_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'jobs'::regclass AND contype = 'f'"
                )
            ).all()
        names = {r[0] for r in rows}
        assert "jobs_course_node_id_fkey" in names
        assert "jobs_course_node_id_fkey" not in names

    def test_index_renamed(self, sync_engine: Engine) -> None:
        """``ix_jobs_course_node_id`` exists; legacy name does not."""
        with sync_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'jobs'")
            ).all()
        names = {r[0] for r in rows}
        assert "ix_jobs_course_node_id" in names
        assert "ix_jobs_course_node_id" not in names


class TestStageProgressRoundTrip:
    """``stage_progress`` round-trips arbitrary nested JSONB structures."""

    async def test_string_round_trip_for_current_stage(
        self, db_session: AsyncSession
    ) -> None:
        """``current_stage`` writes/reads a free-form string identifier."""
        tenant = Tenant(name=f"jrd-{uuid.uuid4().hex[:6]}")
        db_session.add(tenant)
        await db_session.flush()
        job = Job(
            tenant_id=tenant.id,
            job_type="ingest",
            current_stage="pass_2b",
        )
        db_session.add(job)
        await db_session.flush()
        await db_session.refresh(job)
        assert job.current_stage == "pass_2b"

    async def test_nested_dict_round_trip_for_stage_progress(
        self, db_session: AsyncSession
    ) -> None:
        """Nested checkpoint payload reads back byte-identical."""
        tenant = Tenant(name=f"jrd-{uuid.uuid4().hex[:6]}")
        db_session.add(tenant)
        await db_session.flush()
        # Realistic shape: per-pipeline checkpoint state
        progress = {
            "completed_segments": ["seg-1", "seg-2"],
            "next_offset": 12345,
            "pass_2": {
                "summary_done": True,
                "outline_done": False,
                "metrics": {"tokens_in": 1500, "tokens_out": 320},
            },
        }
        job = Job(
            tenant_id=tenant.id,
            job_type="ingest",
            stage_progress=progress,
        )
        db_session.add(job)
        await db_session.flush()
        await db_session.refresh(job)
        assert job.stage_progress == progress

    async def test_both_default_null(self, db_session: AsyncSession) -> None:
        """Newly created Job has both fields NULL by default."""
        tenant = Tenant(name=f"jrd-{uuid.uuid4().hex[:6]}")
        db_session.add(tenant)
        await db_session.flush()
        job = Job(tenant_id=tenant.id, job_type="ingest")
        db_session.add(job)
        await db_session.flush()
        await db_session.refresh(job)
        assert job.current_stage is None
        assert job.stage_progress is None


class TestForeignKeyEnforcement:
    """Renamed FK still enforces referential integrity."""

    async def test_invalid_course_node_id_raises_integrity_error(
        self, db_session: AsyncSession
    ) -> None:
        """Inserting a Job with a non-existent course_node_id is rejected."""
        tenant = Tenant(name=f"jrd-{uuid.uuid4().hex[:6]}")
        db_session.add(tenant)
        await db_session.flush()
        job = Job(
            tenant_id=tenant.id,
            course_node_id=uuid.uuid4(),  # not in course_nodes
            job_type="ingest",
        )
        db_session.add(job)
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_valid_course_node_id_accepted(
        self, db_session: AsyncSession
    ) -> None:
        """Job with a real CourseNode FK persists cleanly."""
        tenant = Tenant(name=f"jrd-{uuid.uuid4().hex[:6]}")
        db_session.add(tenant)
        await db_session.flush()
        node = CourseNode(tenant_id=tenant.id, title="n", order=0)
        db_session.add(node)
        await db_session.flush()
        job = Job(
            tenant_id=tenant.id,
            course_node_id=node.id,
            job_type="ingest",
        )
        db_session.add(job)
        await db_session.flush()
        await db_session.refresh(job)
        assert job.course_node_id == node.id
