"""Repository for HomeworkSubmission CRUD and lifecycle management."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import HomeworkSubmission

logger = structlog.get_logger()

# Task matching was removed in sprint-mentor T1 (KD15: a submission is
# anchored to its task, so the matching/matched states are gone). The
# safety check now hands off straight to review.
HOMEWORK_TRANSITIONS: dict[str, set[str]] = {
    "received": {"safety_check", "failed"},
    "safety_check": {"reviewing", "rejected", "failed"},
    "reviewing": {"completed", "failed"},
    "completed": {"delivered", "failed"},
    "delivered": set(),
    "rejected": set(),
    "failed": {"received"},
}


class HomeworkRepository:
    """Repository for homework submission operations.

    Handles CRUD, status transitions, and result storage.
    Tenant isolation is enforced at the API layer.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        tenant_id: uuid.UUID,
        student_id: uuid.UUID,
        course_node_id: uuid.UUID,
        node_id: uuid.UUID,
        authored_document_id: uuid.UUID,
        file_url: str,
        file_type: str,
        original_filename: str | None = None,
        webhook_url: str | None = None,
        file_hash: str | None = None,
        response_language: str | None = None,
    ) -> HomeworkSubmission:
        """Create a new homework submission.

        Args:
            tenant_id: Owning tenant UUID.
            student_id: FK to the student record.
            course_node_id: Root CourseNode (the course).
            node_id: Specific course node the submission targets.
            authored_document_id: FK to the AuthoredDocument (task) the
                submission answers — the single submission↔task anchor (KD15).
            file_url: S3/B2 path to the uploaded file.
            file_type: MIME type of the uploaded file.
            original_filename: Original filename from the upload.
            webhook_url: Per-submission webhook URL override.
            file_hash: SHA-256 hex digest of the uploaded file.
            response_language: Requested ISO 639-1 language for review.

        Returns:
            The newly created HomeworkSubmission.
        """
        submission = HomeworkSubmission(
            tenant_id=tenant_id,
            student_id=student_id,
            course_node_id=course_node_id,
            node_id=node_id,
            authored_document_id=authored_document_id,
            file_url=file_url,
            file_type=file_type,
            original_filename=original_filename,
            webhook_url=webhook_url,
            file_hash=file_hash,
            response_language=response_language,
        )
        self._session.add(submission)
        await self._session.flush()
        return submission

    async def find_duplicate(
        self,
        *,
        student_id: uuid.UUID,
        authored_document_id: uuid.UUID,
        file_hash: str,
    ) -> HomeworkSubmission | None:
        """Find a completed submission with the same file hash.

        Keyed on the task anchor: an existing submission from the same
        student, for the same task (``authored_document_id``), with an
        identical file hash that already has a terminal result (completed
        or delivered). This catches a genuine re-submit of the same file
        without constraining attempts — different file content for the
        same task is a new attempt and is allowed (D2).

        Args:
            student_id: Student UUID.
            authored_document_id: Task anchor UUID (the AuthoredDocument).
            file_hash: SHA-256 hex digest to match.

        Returns:
            The existing submission if found, None otherwise.
        """
        stmt = (
            select(HomeworkSubmission)
            .where(
                HomeworkSubmission.student_id == student_id,
                HomeworkSubmission.authored_document_id == authored_document_id,
                HomeworkSubmission.file_hash == file_hash,
                HomeworkSubmission.status.in_({"completed", "delivered"}),
            )
            .order_by(HomeworkSubmission.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, submission_id: uuid.UUID) -> HomeworkSubmission | None:
        """Get a submission by primary key."""
        return await self._session.get(HomeworkSubmission, submission_id)

    async def get_by_id_for_tenant(
        self,
        submission_id: uuid.UUID,
        tenant_id: uuid.UUID,
    ) -> HomeworkSubmission | None:
        """Get a submission by ID with tenant isolation."""
        stmt = select(HomeworkSubmission).where(
            HomeworkSubmission.id == submission_id,
            HomeworkSubmission.tenant_id == tenant_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_for_student(
        self,
        student_id: uuid.UUID,
        *,
        course_node_id: uuid.UUID | None = None,
    ) -> list[HomeworkSubmission]:
        """Get submissions for a student, optionally filtered by course.

        Args:
            student_id: Student UUID.
            course_node_id: Optional filter by course root node.
        """
        stmt = (
            select(HomeworkSubmission)
            .where(HomeworkSubmission.student_id == student_id)
            .order_by(HomeworkSubmission.created_at.desc())
        )
        if course_node_id is not None:
            stmt = stmt.where(HomeworkSubmission.course_node_id == course_node_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_status(
        self,
        submission_id: uuid.UUID,
        status: str,
        *,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> HomeworkSubmission:
        """Transition submission to a new status with validation.

        Args:
            submission_id: Submission to update.
            status: Target status.
            error_message: Error description (for failed/rejected).
            now: Override for current time (testing).

        Raises:
            ValueError: If submission not found or transition invalid.
        """
        # Compute valid source statuses for the target atomically
        valid_sources = [
            src for src, targets in HOMEWORK_TRANSITIONS.items() if status in targets
        ]

        now = now or datetime.now(UTC)
        values: dict[str, object] = {"status": status}

        if error_message is not None:
            values["error_message"] = error_message

        if status == "delivered":
            values["webhook_delivered_at"] = now

        # Atomic UPDATE: only transitions from a valid source status
        stmt = (
            update(HomeworkSubmission)
            .where(
                HomeworkSubmission.id == submission_id,
                HomeworkSubmission.status.in_(valid_sources),
            )
            .values(**values)
        )
        result: CursorResult[Any] = await self._session.execute(stmt)  # type: ignore[assignment]
        await self._session.flush()

        if result.rowcount == 0:
            # Distinguish "not found" from "invalid transition"
            existing = await self.get_by_id(submission_id)
            if existing is None:
                msg = f"HomeworkSubmission {submission_id} not found"
                raise ValueError(msg)
            allowed = HOMEWORK_TRANSITIONS.get(existing.status, set())
            msg = (
                f"Invalid homework status transition: "
                f"'{existing.status}' → '{status}'. "
                f"Allowed: {allowed or 'none (terminal state)'}"
            )
            raise ValueError(msg)

        updated = await self.get_by_id(submission_id)
        if updated is None:
            msg = f"HomeworkSubmission {submission_id} disappeared after update"
            raise RuntimeError(msg)
        return updated

    async def store_safety_result(
        self,
        submission_id: uuid.UUID,
        result: dict[str, Any],
    ) -> None:
        """Store safety check result."""
        stmt = (
            update(HomeworkSubmission)
            .where(HomeworkSubmission.id == submission_id)
            .values(safety_result=result)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def store_review_result(
        self,
        submission_id: uuid.UUID,
        *,
        result: dict[str, Any],
        review_markdown: str,
    ) -> None:
        """Store Mentor review result and rendered Markdown."""
        stmt = (
            update(HomeworkSubmission)
            .where(HomeworkSubmission.id == submission_id)
            .values(review_result=result, review_markdown=review_markdown)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def set_job_id(
        self,
        submission_id: uuid.UUID,
        job_id: uuid.UUID,
    ) -> None:
        """Link submission to a background job."""
        stmt = (
            update(HomeworkSubmission)
            .where(HomeworkSubmission.id == submission_id)
            .values(job_id=job_id)
        )
        await self._session.execute(stmt)
        await self._session.flush()
