"""S3 cleanup orchestration helper (vision §3 KD13, §6 QQ5).

Shared helper consumed by KD3-adoption handlers (``delete_document``,
``delete_node``, ``delete_file``) to schedule post-cascade S3 hard-
delete work via the :func:`s3_cleanup_task` ARQ worker.

Per QQ5 ordering — DB → commit → ARQ enqueue — the helper is a
*transaction-completing* call: it persists a ``Job`` row in the
caller's session, COMMITS the session (folding the upstream cascade
soft-delete into the same atomic commit as the s3_cleanup intent),
then enqueues the ARQ task post-commit, then a second commit records
the resolved ``arq_job_id``. This ordering ensures the DB visibly
records the cleanup intent before any ARQ side-effect is observable;
on transaction rollback before commit no ARQ task is queued and no
``Job`` row exists. On a partial failure between commit and ARQ
enqueue (network blip), the ``Job`` row is reactivate-eligible per
KD13 — a future operator-triggered retry hits the dispatch arm at
``api/routes/jobs.py:120-129``.

Reactivate-flow compatibility: the produced ``Job`` row carries
``job_type="s3_cleanup"`` and ``input_params={"file_keys": [...]}``.
The ARQ task is enqueued with the same ``file_keys`` and
``job_id=str(job.id)`` for audit traceability — the worker logs the
``job_id`` but does NOT read the ``Job`` row (the cascade transaction
may have scrubbed neighbours that the worker would otherwise touch;
the explicit ``file_keys`` payload is the source of truth).
"""

from __future__ import annotations

import uuid

from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.jobs import JobType
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import Job


async def enqueue_s3_cleanup(
    *,
    session: AsyncSession,
    arq: ArqRedis,
    file_keys: list[str],
    tenant_id: uuid.UUID | None = None,
    course_node_id: uuid.UUID | None = None,
) -> Job:
    """Persist an ``s3_cleanup`` Job + dispatch the ARQ task.

    The caller is expected to have STAGED any upstream cascade work
    in ``session`` (typically via
    :meth:`CascadeDeleteService.soft_delete_with_cascade` which
    flushes — but does NOT commit — the soft-delete UPDATEs). This
    helper:

    1. Creates the ``Job`` row with ``job_type="s3_cleanup"`` and
       ``input_params={"file_keys": file_keys}``.
    2. Commits the session — the QQ5 boundary. All staged cascade
       work + the ``Job`` row land in one atomic commit.
    3. Enqueues :func:`s3_cleanup_task` on the default ARQ queue
       with the supplied ``file_keys`` and ``job_id=str(job.id)``.
    4. Persists the resolved ``arq_job_id`` back onto the ``Job``
       row and commits a second time. If ``arq.enqueue_job`` returns
       ``None`` (rare — duplicate-id rejection or pool exhaustion),
       ``arq_job_id`` stays NULL and the ``Job`` remains reactivate-
       eligible per KD13.

    Args:
        session: Async session with staged cascade work that is NOT
            yet committed. The helper takes ownership of the commit.
        arq: ARQ Redis connection — typically obtained via the
            FastAPI dependency ``get_arq_redis``.
        file_keys: Bucket-relative S3 keys to hard-delete. Empty
            list is permitted but wasteful — the worker short-
            circuits on empty input; callers should avoid calling
            this helper at all when no cleanup work is pending.
        tenant_id: Owning tenant for the ``Job`` row. Optional
            because cascade-rooted calls have an implicit
            ``course_node_id`` anchor and the
            ``Job.course_node_id → course_node.tenant_id`` join is
            sufficient for tenant scope; the storage.py force-
            orphan path passes ``tenant_id`` explicitly since
            there is no DB anchor at all.
        course_node_id: Anchor ``CourseNode`` for tenant-scoped
            ``Job`` queries; optional for the same reason.

    Returns:
        The persisted ``Job`` row — ``arq_job_id`` is populated when
        ARQ accepted the enqueue, ``None`` when ARQ rejected.
    """
    job_repo = JobRepository(session)
    job = await job_repo.create(
        tenant_id=tenant_id,
        course_node_id=course_node_id,
        job_type=JobType.S3_CLEANUP,
        input_params={"file_keys": file_keys},
    )
    # QQ5 boundary: commit DB before issuing the ARQ side-effect so
    # observers never see an enqueued ARQ task pointing at a Job row
    # that does not yet exist.
    await session.commit()

    arq_job = await arq.enqueue_job(
        "s3_cleanup_task",
        file_keys=file_keys,
        job_id=str(job.id),
    )
    if arq_job is not None:
        await job_repo.set_arq_job_id(job.id, arq_job.job_id)
        await session.commit()
    return job
