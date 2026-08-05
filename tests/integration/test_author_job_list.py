"""Integration tests for the author work-list doors (step A §6-§7).

Requires ``docker compose up -d`` (PostgreSQL). Run with ``uv run pytest
--run-db``. Exercises the real routes over a real DB (session + tenant
dependency-overridden) and checks CONTENT, not just status (vision-rules#10) —
the eight ratified acceptance assertions of TASK-A §7 plus the 422.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.jobs import JOB_SUBJECT_TYPE, JobType
from course_supporter.storage.cascade import scrub_authored_document
from course_supporter.storage.database import get_session
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    Job,
    ProjectBase,
    Tenant,
)

pytestmark = pytest.mark.requires_db

_JOBS = "/api/v1/jobs"
_HISTORY = "/api/v1/jobs/history"


@pytest.fixture
async def client(
    db_session: AsyncSession, seed_tenant: Tenant
) -> AsyncGenerator[AsyncClient]:
    """HTTP client bound to the app, with the route session + author tenant
    dependency-overridden to the test transaction (PREP scope)."""
    ctx = TenantContext(
        tenant_id=seed_tenant.id,
        tenant_name=seed_tenant.name,
        scopes=["prep"],
        plan_id="basic",
        key_prefix="cs_test",
    )
    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_current_tenant] = lambda: ctx
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


# ── builders ──────────────────────────────────────────────────────────────


async def _mk_doc(
    session: AsyncSession,
    node: CourseNode,
    *,
    source_type: str = "text",
    filename: str | None = "lesson.txt",
    task_type: str | None = None,
) -> AuthoredDocument:
    doc = AuthoredDocument(
        course_node_id=node.id,
        course_root_id=node.id,
        source_type=source_type,
        source_url=f"https://example.com/{uuid.uuid4().hex[:6]}",
        filename=filename,
        task_type=task_type,
    )
    session.add(doc)
    await session.flush()
    return doc


async def _mk_base(
    session: AsyncSession, task: AuthoredDocument, *, version: int = 1
) -> ProjectBase:
    base = ProjectBase(
        authored_document_id=task.id,
        version=version,
        archive_key=f"bases/{uuid.uuid4().hex[:6]}/original.zip",
        state="ready",
    )
    session.add(base)
    await session.flush()
    return base


async def _mk_job(
    repo: JobRepository,
    *,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID | None,
    job_type: JobType,
    subject_id: uuid.UUID | None,
    status: str = "queued",
) -> Job:
    job = await repo.create(
        tenant_id=tenant_id,
        course_node_id=node_id,
        job_type=job_type,
        subject_type=JOB_SUBJECT_TYPE[job_type],
        subject_id=subject_id,
    )
    if status == "queued":
        return job
    if status == "cancelled":
        return await repo.update_status(job.id, "cancelled")
    if status == "active":
        return await repo.update_status(job.id, "active")
    await repo.update_status(job.id, "active")
    return await repo.update_status(job.id, status)


# ── §7 acceptance assertions ──────────────────────────────────────────────


class TestAuthorJobList:
    async def test_a1_a2_isolation_and_universe(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_tenant: Tenant,
    ) -> None:
        """A1: another owner's work never returns. A2: homework/cleanup excluded
        by default; all four allowed kinds present."""
        repo = JobRepository(db_session)
        # one of each allowed kind under this tenant
        doc = await _mk_doc(db_session, seed_root_node)
        task = await _mk_doc(db_session, seed_root_node, task_type="project")
        base = await _mk_base(db_session, task)
        await _mk_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=doc.id,
            status="complete",
        )
        await _mk_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PREPARATION,
            subject_id=task.id,
            status="complete",
        )
        await _mk_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.BASE_NORMALIZE,
            subject_id=base.id,
        )
        await _mk_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.NODE_SUMMARY_REGENERATION,
            subject_id=seed_root_node.id,
            status="complete",
        )
        # excluded kinds under the same tenant
        await _mk_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.HOMEWORK_PROCESSING,
            subject_id=uuid.uuid4(),
        )
        await _mk_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.S3_CLEANUP,
            subject_id=None,
        )
        # another tenant's work
        other = Tenant(name=f"other-{uuid.uuid4().hex[:8]}")
        db_session.add(other)
        await db_session.flush()
        await _mk_job(
            repo,
            tenant_id=other.id,
            node_id=None,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=uuid.uuid4(),
        )

        resp = await client.get(_JOBS)
        assert resp.status_code == 200
        body = resp.json()
        kinds = {i["job_type"] for i in body["items"]}
        assert kinds == {
            "document_processing",
            "document_preparation",
            "base_normalize",
            "node_summary_regeneration",
        }
        assert body["total"] == 4  # excludes homework, s3_cleanup, other tenant

    async def test_a2_bad_job_types_422(self, client: AsyncClient) -> None:
        resp = await client.get(_JOBS, params={"job_types": ["homework_processing"]})
        assert resp.status_code == 422

    async def test_a3_deleted_material(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_tenant: Tenant,
    ) -> None:
        """A3: a deleted document — door 1 carries the marker + deleted flags;
        door 2 shows ``processing_phase = null`` and the deletion marks."""
        repo = JobRepository(db_session)
        doc = await _mk_doc(db_session, seed_root_node, source_type="presentation")
        await _mk_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=doc.id,
            status="complete",
        )
        await scrub_authored_document(doc)  # stamps filename with the KD3 marker
        doc.deleted_at = datetime(2021, 5, 5, tzinfo=UTC)
        await db_session.flush()

        item = (await client.get(_JOBS)).json()["items"][0]
        assert item["display_deleted"] is True
        assert item["display_deleted_at"] is not None
        assert item["display_name"].startswith("інформація видалена автором")
        assert item["material_source_type"] == "presentation"

        hist = (await client.get(_HISTORY)).json()["items"][0]
        assert hist["material_deleted"] is True
        assert hist["processing_phase"] is None

    async def test_a4_base_job(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_tenant: Tenant,
    ) -> None:
        """A4: base-normalize work carries the parent task's name + version;
        ``material_id`` is the parent task and its filter returns both kinds."""
        repo = JobRepository(db_session)
        task = await _mk_doc(
            db_session, seed_root_node, filename="proj.zip", task_type="project"
        )
        base = await _mk_base(db_session, task, version=2)
        await _mk_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=task.id,
            status="complete",
        )
        await _mk_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.BASE_NORMALIZE,
            subject_id=base.id,
        )
        items = (await client.get(_JOBS, params={"material_id": str(task.id)})).json()[
            "items"
        ]
        assert {i["job_type"] for i in items} == {
            "document_processing",
            "base_normalize",
        }
        base_item = next(i for i in items if i["job_type"] == "base_normalize")
        assert base_item["display_name"] == "proj.zip"
        assert base_item["base_version"] == 2
        assert base_item["material_id"] == str(task.id)

    async def test_a5_node_summary_shape(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_tenant: Tenant,
    ) -> None:
        """A5: node-summary work — ``material_id`` empty, name is the node title;
        it never appears on the history door."""
        repo = JobRepository(db_session)
        await _mk_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.NODE_SUMMARY_REGENERATION,
            subject_id=seed_root_node.id,
            status="complete",
        )
        item = (await client.get(_JOBS)).json()["items"][0]
        assert item["material_id"] is None
        assert item["display_name"] == seed_root_node.title
        assert (await client.get(_HISTORY)).json()["items"] == []

    async def test_a6_cancelled_from_queue(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_tenant: Tenant,
    ) -> None:
        """A6: work cancelled straight from the queue has no ``started_at`` but a
        ``completed_at``."""
        repo = JobRepository(db_session)
        doc = await _mk_doc(db_session, seed_root_node)
        await _mk_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=doc.id,
            status="cancelled",
        )
        item = (await client.get(_JOBS)).json()["items"][0]
        assert item["job_state"] == "cancelled"
        assert item["started_at"] is None
        assert item["completed_at"] is not None

    async def test_a7_pagination_totals(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_tenant: Tenant,
    ) -> None:
        """A7: ``total`` correct on both doors; a page limit on door 2 does not
        split a group (anchor count independent of ``limit``)."""
        repo = JobRepository(db_session)
        for _ in range(2):  # two materials, two terminal jobs each
            doc = await _mk_doc(db_session, seed_root_node)
            for _ in range(2):
                await _mk_job(
                    repo,
                    tenant_id=seed_tenant.id,
                    node_id=seed_root_node.id,
                    job_type=JobType.DOCUMENT_PROCESSING,
                    subject_id=doc.id,
                    status="complete",
                )
        jobs = (await client.get(_JOBS, params={"limit": 2})).json()
        assert jobs["total"] == 4
        assert len(jobs["items"]) == 2
        hist = (await client.get(_HISTORY, params={"limit": 1})).json()
        assert hist["total"] == 2  # two anchors
        assert len(hist["items"]) == 1
        assert hist["items"][0]["jobs_count"] == 2  # full group, not split

    async def test_a8_history_phase_and_latest(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_tenant: Tenant,
    ) -> None:
        """A8: door 2 phase resolves without the receipt-load guard tripping, and
        the last job is genuinely the newest by ``queued_at``."""
        repo = JobRepository(db_session)
        doc = await _mk_doc(db_session, seed_root_node)
        older = await _mk_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PREPARATION,
            subject_id=doc.id,
            status="complete",
        )
        live = await _mk_job(
            repo,
            tenant_id=seed_tenant.id,
            node_id=seed_root_node.id,
            job_type=JobType.DOCUMENT_PROCESSING,
            subject_id=doc.id,
            status="active",
        )
        # Pin distinct queued_at so ordering is by time, not the id tiebreak;
        # the live job is the newer one.
        older.queued_at = datetime(2022, 1, 1, tzinfo=UTC)
        live.queued_at = datetime(2022, 2, 2, tzinfo=UTC)
        doc.job_id = live.id  # wire the receipt → material phase reads it
        await db_session.flush()

        hist = (await client.get(_HISTORY)).json()["items"]
        assert len(hist) == 1
        assert hist[0]["processing_phase"] == "processing"  # eager-loaded, no error
        assert hist[0]["last_job"]["id"] == str(live.id)
        assert hist[0]["jobs_count"] == 2
