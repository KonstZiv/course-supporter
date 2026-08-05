"""Shared Job factories for integration tests (L1b typed subject).

``make_document_job`` builds a raw ORM Job at a given status. The
``make_authored_document`` / ``make_project_base`` / ``make_job`` trio below
drives the author-work-list tests (step A): they build a material (or its base
version) and walk a Job through the real ``JobRepository`` state machine so
``started_at`` / ``completed_at`` are stamped exactly as production would.
Shared here (not copied per file) — ReviewBot PR#534 §3.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.jobs import JOB_SUBJECT_TYPE, JobType
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    Job,
    ProjectBase,
)


def make_document_job(
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
    subject_id: uuid.UUID,
    *,
    status: str = "queued",
    **kw: object,
) -> Job:
    """A ``document_processing`` Job with a typed ``authored_document`` subject.

    ``subject_type`` must pair with ``job_type`` per ``ck_jobs_subject_type_legal``.
    Extra column kwargs (``deleted_at``, ``completed_at``, ...) pass through.
    """
    return Job(
        tenant_id=tenant_id,
        course_node_id=node_id,
        job_type="document_processing",
        status=status,
        subject_type="authored_document",
        subject_id=subject_id,
        **kw,
    )


async def make_authored_document(
    session: AsyncSession,
    node: CourseNode,
    *,
    source_type: str = "text",
    filename: str | None = "lesson.txt",
    task_type: str | None = None,
) -> AuthoredDocument:
    """Flush an AuthoredDocument under ``node`` (both FKs point at the node)."""
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


async def make_project_base(
    session: AsyncSession, task: AuthoredDocument, *, version: int = 1
) -> ProjectBase:
    """Flush a ``ready`` ProjectBase version under a project-task document."""
    base = ProjectBase(
        authored_document_id=task.id,
        version=version,
        archive_key=f"bases/{uuid.uuid4().hex[:6]}/original.zip",
        state="ready",
    )
    session.add(base)
    await session.flush()
    return base


async def make_job(
    repo: JobRepository,
    *,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID | None,
    job_type: JobType,
    subject_id: uuid.UUID | None,
    status: str = "queued",
    completed_now: datetime | None = None,
) -> Job:
    """Create a Job and walk it to ``status`` through the real state machine.

    ``subject_type`` is derived from ``JOB_SUBJECT_TYPE`` (NULL for s3_cleanup).
    ``completed_now`` pins ``completed_at`` for the terminal transition; walking
    through ``update_status`` is what stamps ``started_at`` (on ``active``) and
    ``completed_at`` exactly as production does.
    """
    job = await repo.create(
        tenant_id=tenant_id,
        course_node_id=node_id,
        job_type=job_type,
        subject_type=JOB_SUBJECT_TYPE[job_type],
        subject_id=subject_id,
    )
    if status == "queued":
        return job
    if status == "cancelled":  # terminated straight from the queue (never active)
        return await repo.update_status(job.id, "cancelled", now=completed_now)
    if status == "active":
        return await repo.update_status(job.id, "active")
    # complete / failed go through active first
    await repo.update_status(job.id, "active")
    return await repo.update_status(job.id, status, now=completed_now)
