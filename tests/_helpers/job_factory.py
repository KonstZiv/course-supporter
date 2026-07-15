"""Shared Job factories for integration tests (L1b typed subject)."""

from __future__ import annotations

import uuid

from course_supporter.storage.orm import Job


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
