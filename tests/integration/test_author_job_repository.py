"""Integration tests for the author work-list storage methods (step A §4).

Requires ``docker compose up -d`` (PostgreSQL). Run with ``uv run pytest
--run-db``. Covers the filter/bound mechanics of ``JobRepository``:

* ``list_author_jobs`` / ``count_author_jobs`` — tenant scope, the four-type
  universe + job_types intersection, ``material_id`` anchor (doc AND base),
  ``state_class`` partition, ``completed_after`` (live vs terminal), ordering
  and pagination.
* ``list_author_material_history`` / ``count_author_material_anchors`` — SQL
  grouping (count, latest job, last activity), node-summary exclusion, phase
  eager-load without ``PendingJobNotLoadedError``, deleted-material phase NULL,
  page-boundary keeps a group whole.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.jobs import JobType
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import CourseNode, Tenant
from tests._helpers.job_factory import (
    make_authored_document,
    make_job,
    make_project_base,
)

pytestmark = pytest.mark.requires_db


# ── door 1: flat list ─────────────────────────────────────────────────────


class TestListAuthorJobs:
    async def test_tenant_isolation(
        self, db_session: AsyncSession, seed_root_node: CourseNode, seed_tenant: Tenant
    ) -> None:
        repo = JobRepository(db_session)
        doc = await make_authored_document(db_session, seed_root_node)
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=doc.id,
        )
        # Another tenant's job must never surface.
        other = Tenant(name=f"other-{uuid.uuid4().hex[:8]}")
        db_session.add(other)
        await db_session.flush()
        await make_job(
            repo,
            tenant_id=other.id,
            node_id=None,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=uuid.uuid4(),
        )

        rows = await repo.list_author_jobs(seed_tenant.id)
        assert {r.job.tenant_id for r in rows} == {seed_tenant.id}
        assert await repo.count_author_jobs(seed_tenant.id) == 1

    async def test_universe_excludes_homework_and_s3(
        self, db_session: AsyncSession, seed_root_node: CourseNode, seed_tenant: Tenant
    ) -> None:
        repo = JobRepository(db_session)
        doc = await make_authored_document(db_session, seed_root_node)
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=doc.id,
        )
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.HOMEWORK_PROCESSING,
            subject_id=uuid.uuid4(),
        )
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.S3_CLEANUP,
            subject_id=None,
        )
        rows = await repo.list_author_jobs(seed_tenant.id)
        assert {r.job.job_type for r in rows} == {"document_processing"}

    async def test_job_types_intersection_drops_out_of_universe(
        self, db_session: AsyncSession, seed_root_node: CourseNode, seed_tenant: Tenant
    ) -> None:
        repo = JobRepository(db_session)
        doc = await make_authored_document(db_session, seed_root_node)
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=doc.id,
        )
        # A request for only homework intersects to empty (defensive twin of the
        # route's 422): nothing author-facing comes back.
        assert (
            await repo.list_author_jobs(
                seed_tenant.id, job_types=["homework_processing"]
            )
            == []
        )
        rows = await repo.list_author_jobs(
            seed_tenant.id, job_types=["document_processing"]
        )
        assert len(rows) == 1

    async def test_material_id_catches_doc_and_base(
        self, db_session: AsyncSession, seed_root_node: CourseNode, seed_tenant: Tenant
    ) -> None:
        repo = JobRepository(db_session)
        task = await make_authored_document(
            db_session, seed_root_node, filename="project.zip", task_type="project"
        )
        base = await make_project_base(db_session, task)
        # A processing job on the task itself + a base-normalize job on its base
        # version. Both anchor to the same task id.
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=task.id,
            status="complete",
        )
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.BASE_NORMALIZE,
            subject_id=base.id,
        )
        rows = await repo.list_author_jobs(seed_tenant.id, material_id=task.id)
        assert {r.job.job_type for r in rows} == {
            "document_processing",
            "base_normalize",
        }
        # The base row carries the parent task's filename + version.
        base_row = next(r for r in rows if r.job.job_type == "base_normalize")
        assert base_row.material_id == task.id
        assert base_row.display_name == "project.zip"
        assert base_row.base_version == 1

    async def test_state_class_partition(
        self, db_session: AsyncSession, seed_root_node: CourseNode, seed_tenant: Tenant
    ) -> None:
        repo = JobRepository(db_session)
        live_doc = await make_authored_document(db_session, seed_root_node)
        done_doc = await make_authored_document(db_session, seed_root_node)
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=live_doc.id,
            status="active",
        )
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=done_doc.id,
            status="complete",
        )
        live = await repo.list_author_jobs(seed_tenant.id, state_class="in_flight")
        assert {r.job.status for r in live} == {"active"}
        rest = await repo.list_author_jobs(seed_tenant.id, state_class="at_rest")
        assert {r.job.status for r in rest} == {"complete"}

    async def test_completed_after_keeps_live_filters_terminal(
        self, db_session: AsyncSession, seed_root_node: CourseNode, seed_tenant: Tenant
    ) -> None:
        repo = JobRepository(db_session)
        live_doc = await make_authored_document(db_session, seed_root_node)
        old_doc = await make_authored_document(db_session, seed_root_node)
        recent_doc = await make_authored_document(db_session, seed_root_node)
        old = datetime(2020, 1, 1, tzinfo=UTC)
        recent = datetime(2020, 6, 1, tzinfo=UTC)
        cutoff = datetime(2020, 3, 1, tzinfo=UTC)
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=live_doc.id,
            status="active",
        )
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=old_doc.id,
            status="complete",
            completed_now=old,
        )
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=recent_doc.id,
            status="complete",
            completed_now=recent,
        )
        rows = await repo.list_author_jobs(seed_tenant.id, completed_after=cutoff)
        statuses = sorted(r.job.status for r in rows)
        # live (always) + recent terminal; the old terminal is filtered out.
        assert statuses == ["active", "complete"]
        assert await repo.count_author_jobs(seed_tenant.id, completed_after=cutoff) == 2

    async def test_ordering_and_pagination(
        self, db_session: AsyncSession, seed_root_node: CourseNode, seed_tenant: Tenant
    ) -> None:
        repo = JobRepository(db_session)
        created = []
        for _ in range(3):
            doc = await make_authored_document(db_session, seed_root_node)
            job = await make_job(
                repo,
                tenant_id=seed_tenant.id,
                node_id=seed_root_node.id,
                job_type=JobType.DOCUMENT_PROCESSING,
                subject_id=doc.id,
                status="complete",
            )
            created.append(job.id)
        # queued_at ties inside one transaction; id DESC breaks it → newest first.
        rows = await repo.list_author_jobs(seed_tenant.id)
        assert [r.job.id for r in rows] == list(reversed(created))
        assert await repo.count_author_jobs(seed_tenant.id) == 3
        page = await repo.list_author_jobs(seed_tenant.id, limit=1, offset=1)
        assert [r.job.id for r in page] == [created[1]]


# ── door 2: grouped history ───────────────────────────────────────────────


class TestMaterialHistory:
    async def test_grouping_count_and_latest(
        self, db_session: AsyncSession, seed_root_node: CourseNode, seed_tenant: Tenant
    ) -> None:
        repo = JobRepository(db_session)
        doc = await make_authored_document(db_session, seed_root_node, filename="m.txt")
        # prep (complete) → processing (complete) → processing (live). Three jobs,
        # one anchor. Only one may be in-flight at a time (uq index).
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PREPARATION,
            subject_id=doc.id,
            status="complete",
        )
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=doc.id,
            status="complete",
        )
        live = await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=doc.id,
            status="active",
        )
        # Wire the material receipt to the live job — the material phase reads the
        # material's own pending_job (its receipt), a different axis from the
        # history's last_job (PROBE-A q Ф).
        doc.job_id = live.id
        await db_session.flush()

        history = await repo.list_author_material_history(seed_tenant.id)
        assert len(history) == 1
        row = history[0]
        assert row.material_id == doc.id
        assert row.jobs_count == 3
        assert row.last_job.job.id == live.id  # latest by queued_at, id
        assert row.display_name == "m.txt"
        # Phase resolves from the eager-loaded receipt (no PendingJobNotLoaded).
        assert row.processing_phase == "processing"
        assert await repo.count_author_material_anchors(seed_tenant.id) == 1

    async def test_node_summary_excluded_from_history(
        self, db_session: AsyncSession, seed_root_node: CourseNode, seed_tenant: Tenant
    ) -> None:
        repo = JobRepository(db_session)
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.NODE_SUMMARY_REGENERATION,
            subject_id=seed_root_node.id,
            status="complete",
        )
        assert await repo.list_author_material_history(seed_tenant.id) == []
        assert await repo.count_author_material_anchors(seed_tenant.id) == 0

    async def test_deleted_material_phase_null(
        self, db_session: AsyncSession, seed_root_node: CourseNode, seed_tenant: Tenant
    ) -> None:
        repo = JobRepository(db_session)
        doc = await make_authored_document(
            db_session, seed_root_node, filename="gone.txt"
        )
        await make_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=doc.id,
            status="complete",
        )
        # Soft-delete the anchor (marker scrub not needed for the phase-null check).
        doc.deleted_at = datetime(2021, 1, 1, tzinfo=UTC)
        await db_session.flush()
        history = await repo.list_author_material_history(seed_tenant.id)
        assert len(history) == 1
        assert history[0].processing_phase is None
        assert history[0].material_deleted_at is not None

    async def test_page_boundary_keeps_group_whole(
        self, db_session: AsyncSession, seed_root_node: CourseNode, seed_tenant: Tenant
    ) -> None:
        repo = JobRepository(db_session)
        # Two materials, each with two terminal jobs.
        for _ in range(2):
            doc = await make_authored_document(db_session, seed_root_node)
            for _ in range(2):
                await make_job(
                    repo,
                    tenant_id=seed_tenant.id,
                    node_id=seed_root_node.id,
                    job_type=JobType.DOCUMENT_PROCESSING,
                    subject_id=doc.id,
                    status="complete",
                )
        assert await repo.count_author_material_anchors(seed_tenant.id) == 2
        # A limit of one material returns ONE anchor with its FULL count (2),
        # not a half-group split at the page edge.
        page = await repo.list_author_material_history(seed_tenant.id, limit=1)
        assert len(page) == 1
        assert page[0].jobs_count == 2
