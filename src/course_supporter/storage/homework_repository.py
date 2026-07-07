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

# Homework lifecycle, reconciled to the KD15 §1298 vocabulary in sprint-mentor
# T7. The status is the MILESTONE the submission has REACHED (so ``safety_ok`` /
# ``sanity_ok``, not the old in-progress ``safety_check``). Pipeline:
# safety → sanity → review → delivery (task matching was dropped in T1 — the
# submission is anchored to its task, KD15; the cheap sanity gate replaces
# match). Four clean terminals:
#   - ``delivered``  — review delivered to the caller (success);
#   - ``rejected``   — safety violation (kept distinct from ``failed`` so a
#                       safety refusal is never confused with a processing error);
#   - ``mismatch``   — sanity gate judged the submission off-task (high
#                       confidence);
#   - ``failed``     — processing error (re-activate via ``failed → received``).
# Safety runs while the status is still ``received`` (it sets ``safety_ok`` only
# on pass), so ``received`` can go to ``safety_ok`` / ``rejected`` / ``failed``.
HOMEWORK_TRANSITIONS: dict[str, set[str]] = {
    "received": {"safety_ok", "rejected", "failed"},
    "safety_ok": {"sanity_ok", "mismatch", "failed"},
    "sanity_ok": {"reviewing", "failed"},
    "reviewing": {"completed", "failed"},
    "completed": {"delivered", "failed"},
    "delivered": set(),
    "rejected": set(),
    "mismatch": set(),
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
        student_note: str | None = None,
        delivery_mode: str = "webhook",
        base_id: uuid.UUID | None = None,
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
            student_note: D7-local free-text note the student attached to this
                submission (steers the Mentor review of this attempt).
            delivery_mode: ``'webhook'`` (mode-1, default) or ``'in_app'``
                (mode-2 portal session submission; Phase 6 T2, KD17).
            base_id: FK → the ProjectBase version this project submission was
                echo-matched against (KD18 P3), resolved at submit-time. ``None``
                for single-file / non-project / no-base project submissions. The
                worker-time snapshot columns are set later via
                :meth:`store_snapshot`.

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
            student_note=student_note,
            delivery_mode=delivery_mode,
            base_id=base_id,
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

    async def list_active_submissions_in_course(
        self,
        student_id: uuid.UUID,
        course_root_id: uuid.UUID,
    ) -> list[HomeworkSubmission]:
        """List a student's ACTIVE submissions across a whole course (read-path).

        Course-scoped (``course_node_id`` is the course ROOT, KD15) and
        ownership-scoped, EXCLUDING soft-deleted rows (Q7 — a soft-deleted
        attempt is invisible to the student). Powers the materials-tree overlay:
        one query per course, grouped by ``authored_document_id`` in Python (no
        N+1, Phase 6 T4a c2). Newest-first so the first row in each group is the
        latest attempt.

        Deliberately distinct from the soft-delete-AGNOSTIC
        :meth:`get_for_student` (shared with the Mentor review-graph history
        reconciliation, ``review_graph.py``), which is left untouched — the
        ``active`` in the name flags the ``deleted_at`` filter that the curated
        read-path requires but the review path must not (Q1).
        """
        stmt = (
            select(HomeworkSubmission)
            .where(
                HomeworkSubmission.student_id == student_id,
                HomeworkSubmission.course_node_id == course_root_id,
                HomeworkSubmission.deleted_at.is_(None),
            )
            .order_by(HomeworkSubmission.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_for_student_and_task(
        self,
        student_id: uuid.UUID,
        authored_document_id: uuid.UUID,
    ) -> list[HomeworkSubmission]:
        """List a student's attempts on one task, newest first (read-path).

        Ownership-scoped (``student_id``) and task-scoped
        (``authored_document_id``, the KD15 anchor), excluding soft-deleted
        rows (Q7: a soft-deleted submission is invisible to the student). The
        portal read-path lists attempts per task; mode-1's :meth:`get_for_student`
        is course-scoped and is left untouched.
        """
        stmt = (
            select(HomeworkSubmission)
            .where(
                HomeworkSubmission.student_id == student_id,
                HomeworkSubmission.authored_document_id == authored_document_id,
                HomeworkSubmission.deleted_at.is_(None),
            )
            .order_by(HomeworkSubmission.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_owned(
        self,
        submission_id: uuid.UUID,
        student_id: uuid.UUID,
    ) -> HomeworkSubmission | None:
        """Get a non-deleted submission owned by this student (read-path detail).

        Ownership-only authorization (Q1): a student reads only their own
        submissions, regardless of current enrollment (own history survives an
        un-enrollment). A soft-deleted submission is not returned (Q7 — the
        route maps the None to a generic 404).
        """
        stmt = select(HomeworkSubmission).where(
            HomeworkSubmission.id == submission_id,
            HomeworkSubmission.student_id == student_id,
            HomeworkSubmission.deleted_at.is_(None),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

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

    async def store_sanity_result(
        self,
        submission_id: uuid.UUID,
        result: dict[str, Any],
    ) -> None:
        """Store the sanity gate result (sprint-mentor T7, KD15).

        Mirrors :meth:`store_safety_result` — a single-column UPDATE of the
        ``sanity_result`` JSONB ({verdict, confidence, reason}). Decoupled from
        the status transition so the result is persisted before the gate
        decision is acted on (the worker stores, then routes to ``mismatch``
        or ``sanity_ok``).
        """
        stmt = (
            update(HomeworkSubmission)
            .where(HomeworkSubmission.id == submission_id)
            .values(sanity_result=result)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def store_review_result(
        self,
        submission_id: uuid.UUID,
        *,
        result: dict[str, Any],
        review_markdown: str,
        score: int | None = None,
    ) -> None:
        """Store Mentor review result, rendered Markdown, and the score.

        The score is written in the SAME UPDATE as ``review_result`` so the
        typed ``score`` column and the ``denoised_score`` inside the JSONB
        never drift (D1 / KD15). ``score`` defaults to ``None`` (left
        unchanged) for the placeholder path; the T6 graph always passes it.
        """
        values: dict[str, Any] = {
            "review_result": result,
            "review_markdown": review_markdown,
        }
        if score is not None:
            values["score"] = score
        stmt = (
            update(HomeworkSubmission)
            .where(HomeworkSubmission.id == submission_id)
            .values(**values)
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def store_snapshot(
        self,
        submission_id: uuid.UUID,
        *,
        snapshot_key: str,
        snapshot_hash: str,
        snapshot_manifest: dict[str, Any],
    ) -> None:
        """Persist the project submission's normalized snapshot (KD18 P3).

        Sets the three worker-time snapshot columns (``base_id`` is written at
        submit-time by :meth:`create`). Mirror of :meth:`store_safety_result` /
        :meth:`store_review_result` — a single UPDATE, no status transition. The
        manifest arrives already JSONB-serialized (via
        ``normalizer.manifest_to_jsonb``); this method only writes. Called once
        by the delta-submit worker before the safety stage (Commit 5).
        """
        stmt = (
            update(HomeworkSubmission)
            .where(HomeworkSubmission.id == submission_id)
            .values(
                snapshot_key=snapshot_key,
                snapshot_hash=snapshot_hash,
                snapshot_manifest=snapshot_manifest,
            )
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
