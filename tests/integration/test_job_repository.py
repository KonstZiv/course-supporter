"""Integration tests for JobRepository against real PostgreSQL.

Requires ``docker compose up -d`` (PostgreSQL).
Run with: ``uv run pytest tests/integration/test_job_repository.py --run-db -v``
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import AuthoredDocument, CourseNode, Tenant

pytestmark = pytest.mark.requires_db


class TestJobCreate:
    """Job creation against real PostgreSQL."""

    async def test_create_generates_uuidv7(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Job.id is a valid UUIDv7 (version 7)."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )

        assert job.id is not None
        assert job.id.version == 7

    async def test_create_stores_jsonb_input_params(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """input_params dict round-trips through JSONB correctly."""
        params = {
            "material_id": str(uuid.uuid4()),
            "source_type": "web",
            "source_url": "https://example.com",
        }
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
            input_params=params,
        )

        fetched = await repo.get_by_id(job.id)
        assert fetched is not None
        assert fetched.input_params == params

    async def test_create_server_default_queued_at(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """queued_at is set by server_default=func.now()."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )

        # Force reload from DB to see server_default
        await db_session.refresh(job, attribute_names=["queued_at"])

        assert job.queued_at is not None
        # Should be within 5 seconds of now
        delta = abs((datetime.now(UTC) - job.queued_at).total_seconds())
        assert delta < 5


class TestJobLifecycleSuccess:
    """Full success lifecycle: queued -> active -> complete."""

    async def test_full_success_lifecycle(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """queued -> active -> complete with all timestamps and result."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )
        assert job.status == "queued"
        assert job.started_at is None
        assert job.completed_at is None

        # queued -> active
        active_job = await repo.update_status(job.id, "active")
        assert active_job.status == "active"
        assert active_job.started_at is not None

        # active -> complete
        complete_job = await repo.update_status(job.id, "complete")
        assert complete_job.status == "complete"
        assert complete_job.completed_at is not None

    async def test_active_sets_started_at(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Transition to active populates started_at timestamp."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )

        active_job = await repo.update_status(job.id, "active")

        assert active_job.started_at is not None
        delta = abs((datetime.now(UTC) - active_job.started_at).total_seconds())
        assert delta < 5

    async def test_obsolete_sets_completed_at_without_error_fields(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """L2 ``obsolete`` terminal ("subject vanished"): stamps
        ``completed_at`` (termination time) but writes NO error fields — it is
        not a failure, the world changed (Рат.1). Reachable from both in-flight
        states (queued directly, and via active)."""
        repo = JobRepository(db_session)

        # queued -> obsolete (subject gone before the worker ever picked it up)
        job_q = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )
        obs_q = await repo.update_status(job_q.id, "obsolete")
        assert obs_q.status == "obsolete"
        assert obs_q.completed_at is not None
        assert obs_q.error_message is None
        assert obs_q.error_category is None

        # active -> obsolete (subject died while the job was in flight)
        job_a = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )
        await repo.update_status(job_a.id, "active")
        obs_a = await repo.update_status(job_a.id, "obsolete")
        assert obs_a.status == "obsolete"
        assert obs_a.completed_at is not None
        assert obs_a.error_message is None


class TestJobLifecycleFailureRetry:
    """Failure + retry lifecycle."""

    async def test_full_failure_retry_lifecycle(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """queued -> active -> failed -> queued (retry)."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )

        # queued -> active
        await repo.update_status(job.id, "active")

        # active -> failed
        failed_job = await repo.update_status(
            job.id, "failed", error_message="Timeout connecting to LLM"
        )
        assert failed_job.status == "failed"
        assert failed_job.error_message == "Timeout connecting to LLM"
        assert failed_job.completed_at is not None

        # failed -> queued (retry)
        retried_job = await repo.update_status(job.id, "queued")
        assert retried_job.status == "queued"


class TestJobTransitionValidation:
    """Invalid transitions enforced by repository."""

    async def test_invalid_transition_raises(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """queued -> complete raises ValueError (must go through active)."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )

        with pytest.raises(ValueError, match="Invalid job status transition"):
            await repo.update_status(job.id, "complete")

    async def test_update_status_idempotent_active_to_active(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Same-state transition is a no-op and preserves ``started_at``.

        ARQ task replay (worker restart mid-await, ``max_tries`` retry,
        manual rerun) would otherwise hit
        ``Invalid job status transition: active → active`` and crash the
        second attempt before the real work runs (TASK-2.4.18).
        """
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )

        first = await repo.update_status(job.id, "active")
        first_started_at = first.started_at
        assert first.status == "active"
        assert first_started_at is not None

        # Replay: no exception, same row returned, ``started_at`` frozen.
        second = await repo.update_status(job.id, "active")
        assert second.status == "active"
        assert second.started_at == first_started_at


class TestJobQueries:
    """Query methods against real data."""

    async def test_get_by_id_for_tenant_correct(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """Returns job when tenant matches."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_tenant.id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )

        found = await repo.get_by_id_for_tenant(job.id, seed_tenant.id)
        assert found is not None
        assert found.id == job.id

    async def test_get_by_id_for_tenant_wrong_tenant(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
    ) -> None:
        """Returns None when tenant_id does not match."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )

        wrong_tenant_id = uuid.uuid4()
        found = await repo.get_by_id_for_tenant(job.id, wrong_tenant_id)
        assert found is None

    async def test_count_pending(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """count_pending returns accurate count with mixed statuses."""
        repo = JobRepository(db_session)

        # Count before
        initial_count = await repo.count_pending()

        # Add 3 queued
        for _ in range(3):
            await repo.create(
                tenant_id=seed_root_node.tenant_id,
                course_node_id=seed_root_node.id,
                job_type="document_processing",
            )

        # Add 1 active (not counted)
        job_active = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )
        await repo.update_status(job_active.id, "active")

        count = await repo.count_pending()
        assert count == initial_count + 3

    async def test_set_arq_job_id_persists(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """set_arq_job_id updates and the value is readable after flush."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )
        assert job.arq_job_id is None

        await repo.set_arq_job_id(job.id, "arq:test:abc123")

        fetched = await repo.get_by_id(job.id)
        assert fetched is not None
        assert fetched.arq_job_id == "arq:test:abc123"


class TestJobReactivate:
    """``reactivate`` re-queues failed Jobs for retry (vision §3 KD13).

    Covers happy-path field semantics (cleared vs preserved) and the
    rejection branches the API layer maps to 4xx (404 / 409). Resume
    semantics live in ``stage_progress``: it MUST survive reactivate
    because that is the field the worker reads to skip already-done
    pipeline stages on retry.
    """

    async def test_failed_to_queued_clears_run_attempt_state(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
    ) -> None:
        """status, error_message, started_at, completed_at, arq_job_id reset."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )
        await repo.set_arq_job_id(job.id, "arq:old:xyz")
        await repo.update_status(job.id, "active")
        await repo.update_status(job.id, "failed", error_message="boom")

        # Sanity: pre-state is failed with all the run-attempt fields set
        before = await repo.get_by_id(job.id)
        assert before is not None
        assert before.status == "failed"
        assert before.error_message == "boom"
        assert before.started_at is not None
        assert before.completed_at is not None
        assert before.arq_job_id == "arq:old:xyz"

        reactivated = await repo.reactivate(job.id)

        assert reactivated.status == "queued"
        assert reactivated.error_message is None
        assert reactivated.started_at is None
        assert reactivated.completed_at is None
        # New ARQ job_id is the caller's responsibility — reactivate clears
        # the old one so a stale value can't leak into the next attempt.
        assert reactivated.arq_job_id is None

    async def test_preserves_stage_progress_and_queued_at(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
    ) -> None:
        """``stage_progress`` + ``queued_at`` survive reactivate.

        Worker resumes from KD4a checkpoint, so dropping ``stage_progress``
        would re-do already-completed pipeline stages.
        """
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )
        # Simulate worker writing a checkpoint mid-flight
        progress = {"completed_segments": ["s1"], "next_offset": 4096}
        job.stage_progress = progress
        await db_session.flush()
        original_queued_at = job.queued_at

        await repo.update_status(job.id, "active")
        await repo.update_status(job.id, "failed")

        reactivated = await repo.reactivate(job.id)

        # The whole point of reactivate (vs creating a new Job): resume
        assert reactivated.stage_progress == progress
        # First-queued metadata is more meaningful than per-attempt time
        assert reactivated.queued_at == original_queued_at

    async def test_missing_job_raises_value_error(
        self, db_session: AsyncSession
    ) -> None:
        """Non-existent job_id → ValueError (API maps to 404)."""
        repo = JobRepository(db_session)
        with pytest.raises(ValueError, match="not found"):
            await repo.reactivate(uuid.uuid4())

    @pytest.mark.parametrize("blocking_status", ["queued", "active", "complete"])
    async def test_rejects_non_failed_states(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        blocking_status: str,
    ) -> None:
        """Only ``failed`` reactivates — every other state is a 409.

        ``cancelled`` is a deliberate omission: rerunning the worker for
        a cancelled Job would defeat the cancel signal.
        """
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )
        if blocking_status == "active":
            await repo.update_status(job.id, "active")
        elif blocking_status == "complete":
            await repo.update_status(job.id, "active")
            await repo.update_status(job.id, "complete")
        # else: blocking_status == "queued" → no transitions needed

        with pytest.raises(ValueError, match="Cannot reactivate Job"):
            await repo.reactivate(job.id)

    async def test_rejects_cancelled(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
    ) -> None:
        """Reactivating a cancelled Job would defeat the cancel signal."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )
        await repo.update_status(job.id, "cancelled")
        with pytest.raises(ValueError, match="Cannot reactivate Job"):
            await repo.reactivate(job.id)

    async def test_rejects_soft_deleted(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
    ) -> None:
        """Soft-deleted Job → ValueError even if status would otherwise allow.

        The check fires before the status check; reactivating a
        soft-deleted row would also collide with the 0.1
        ``block_update_on_soft_deleted`` trigger when the worker
        attempts its first ``update_status``.
        """
        from datetime import UTC, datetime

        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )
        await repo.update_status(job.id, "active")
        await repo.update_status(job.id, "failed")
        # Soft-delete via direct attribute write (cascade service is 0.1
        # scope; here we just need the deleted_at to be set).
        job.deleted_at = datetime(2026, 1, 1, tzinfo=UTC)
        await db_session.flush()

        with pytest.raises(ValueError, match="soft-deleted"):
            await repo.reactivate(job.id)


class TestUpdateStage:
    """``JobRepository.update_stage`` -- current_stage checkpoint marker.

    Covers Phase 2.1 C4 (KD-2.1-B). Method is infrastructure-only --
    production callers are added in C5 / C6 / C7. Tests verify atomic
    UPDATE with ``deleted_at IS NULL`` filter + silent skip behavior
    on missing/filtered rows + log.warning emission for observability.
    """

    async def test_successful_update_persists_value(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
    ) -> None:
        """update_stage writes stage_name to current_stage column."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )
        assert job.current_stage is None

        await repo.update_stage(job.id, "extracting_structure")

        fetched = await repo.get_by_id(job.id)
        assert fetched is not None
        assert fetched.current_stage == "extracting_structure"

    async def test_filter_deleted_at_skips_soft_deleted(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
    ) -> None:
        """Soft-deleted Job is excluded by deleted_at IS NULL filter."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )
        # Set initial stage on a live Job.
        await repo.update_stage(job.id, "initial_stage")
        # Soft-delete via direct attribute write (cascade service out of
        # scope; here we only need deleted_at to be set on this row).
        job.deleted_at = datetime(2026, 1, 1, tzinfo=UTC)
        await db_session.flush()

        with capture_logs() as logs:
            await repo.update_stage(job.id, "should_not_apply")

        # WHERE filter excludes soft-deleted row → rowcount=0 → warning.
        warnings = [log for log in logs if log["log_level"] == "warning"]
        assert len(warnings) == 1
        assert warnings[0]["event"] == "update_stage_no_job_found"
        assert warnings[0]["job_id"] == str(job.id)
        assert warnings[0]["stage_name"] == "should_not_apply"

        # current_stage on the soft-deleted row remains the initial value.
        await db_session.refresh(job)
        assert job.current_stage == "initial_stage"

    async def test_no_job_found_silent_skip_with_warning(
        self,
        db_session: AsyncSession,
    ) -> None:
        """Random UUID → no exception, warning log emitted з context."""
        repo = JobRepository(db_session)
        nonexistent = uuid.uuid4()

        with capture_logs() as logs:
            await repo.update_stage(nonexistent, "extracting_structure")

        warnings = [log for log in logs if log["log_level"] == "warning"]
        assert len(warnings) == 1
        assert warnings[0]["event"] == "update_stage_no_job_found"
        assert warnings[0]["job_id"] == str(nonexistent)
        assert warnings[0]["stage_name"] == "extracting_structure"

    async def test_idempotent_update(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
    ) -> None:
        """Writing the same stage_name twice succeeds (no exception)."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )

        with capture_logs() as logs:
            await repo.update_stage(job.id, "checking_safety")
            await repo.update_stage(job.id, "checking_safety")

        # Both calls match the live row → rowcount=1 each → no warnings.
        warnings = [log for log in logs if log["log_level"] == "warning"]
        assert warnings == []

        fetched = await repo.get_by_id(job.id)
        assert fetched is not None
        assert fetched.current_stage == "checking_safety"


class TestGetInFlightJobs:
    """get_in_flight_jobs — global sweep of queued OR active (task 3.3c-B; L2 F11
    extends the reconcile from active-only to in-flight)."""

    async def test_returns_queued_and_active_excludes_soft_deleted(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Returns queued + active (non-deleted); excludes soft-deleted."""
        repo = JobRepository(db_session)
        tid = seed_root_node.tenant_id
        nid = seed_root_node.id

        active_one = await repo.create(
            tenant_id=tid, course_node_id=nid, job_type="document_processing"
        )
        await repo.update_status(active_one.id, "active")
        active_two = await repo.create(
            tenant_id=tid, course_node_id=nid, job_type="document_processing"
        )
        await repo.update_status(active_two.id, "active")

        # Stays queued — now INCLUDED (L2 F11: queued orphans are reconcilable).
        queued = await repo.create(
            tenant_id=tid, course_node_id=nid, job_type="document_processing"
        )

        # Active but soft-deleted — must be excluded.
        deleted = await repo.create(
            tenant_id=tid, course_node_id=nid, job_type="document_processing"
        )
        await repo.update_status(deleted.id, "active")
        deleted_row = await repo.get_by_id(deleted.id)
        assert deleted_row is not None
        deleted_row.deleted_at = datetime.now(UTC)
        await db_session.flush()

        result = await repo.get_in_flight_jobs()
        ids = {job.id for job in result}

        assert active_one.id in ids
        assert active_two.id in ids
        assert queued.id in ids
        assert deleted.id not in ids


class TestAdmissionAggregate:
    """DD-3.3c-I-B intake-duration column, queue aggregate, retry reuse."""

    async def test_create_stores_duration_sec(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """duration_sec round-trips through create + reload."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
            duration_sec=5400.0,
        )

        fetched = await repo.get_by_id(job.id)
        assert fetched is not None
        assert fetched.duration_sec == 5400.0

    async def test_create_duration_defaults_null(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """duration_sec is NULL when omitted (non-video / pre-feature rows)."""
        repo = JobRepository(db_session)
        job = await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
        )
        fetched = await repo.get_by_id(job.id)
        assert fetched is not None
        assert fetched.duration_sec is None

    async def test_sum_empty_queue_is_zero(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """COALESCE returns 0.0 for an empty queue (no ingest jobs)."""
        repo = JobRepository(db_session)
        assert await repo.sum_active_ingest_duration_sec() == 0.0

    async def test_sum_counts_queued_and_active_only(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """SUM covers queued + active ingest durations; terminal states excluded."""
        repo = JobRepository(db_session)
        tid = seed_root_node.tenant_id
        nid = seed_root_node.id

        # queued (counts) + active (counts)
        await repo.create(
            tenant_id=tid,
            course_node_id=nid,
            job_type="document_processing",
            duration_sec=1000.0,
        )
        active = await repo.create(
            tenant_id=tid,
            course_node_id=nid,
            job_type="document_processing",
            duration_sec=2000.0,
        )
        await repo.update_status(active.id, "active")

        # complete / failed / cancelled — must NOT count.
        complete = await repo.create(
            tenant_id=tid,
            course_node_id=nid,
            job_type="document_processing",
            duration_sec=9999.0,
        )
        await repo.update_status(complete.id, "active")
        await repo.update_status(complete.id, "complete")
        failed = await repo.create(
            tenant_id=tid,
            course_node_id=nid,
            job_type="document_processing",
            duration_sec=9999.0,
        )
        await repo.update_status(failed.id, "failed")
        cancelled = await repo.create(
            tenant_id=tid,
            course_node_id=nid,
            job_type="document_processing",
            duration_sec=9999.0,
        )
        await repo.update_status(cancelled.id, "cancelled")

        assert await repo.sum_active_ingest_duration_sec() == 3000.0

    async def test_sum_excludes_non_ingest_and_null(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Non-ingest job_type and NULL durations are excluded from the sum."""
        repo = JobRepository(db_session)
        tid = seed_root_node.tenant_id
        nid = seed_root_node.id

        await repo.create(
            tenant_id=tid,
            course_node_id=nid,
            job_type="document_processing",
            duration_sec=1500.0,
        )
        # Non-ingest job with a duration — excluded by job_type filter.
        await repo.create(
            tenant_id=tid,
            course_node_id=nid,
            job_type="homework_processing",
            duration_sec=9999.0,
        )
        # Ingest job with NULL duration (non-video) — ignored by SUM.
        await repo.create(
            tenant_id=tid, course_node_id=nid, job_type="document_processing"
        )

        assert await repo.sum_active_ingest_duration_sec() == 1500.0

    async def test_sum_excludes_soft_deleted(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Soft-deleted ingest jobs do not contribute to the queue sum."""
        repo = JobRepository(db_session)
        tid = seed_root_node.tenant_id
        nid = seed_root_node.id

        deleted = await repo.create(
            tenant_id=tid,
            course_node_id=nid,
            job_type="document_processing",
            duration_sec=4000.0,
        )
        row = await repo.get_by_id(deleted.id)
        assert row is not None
        row.deleted_at = datetime.now(UTC)
        await db_session.flush()

        assert await repo.sum_active_ingest_duration_sec() == 0.0

    async def test_latest_duration_for_material_reuses_prior(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Retry lookup returns the most recent non-NULL duration for a material."""
        repo = JobRepository(db_session)
        tid = seed_root_node.tenant_id
        nid = seed_root_node.id
        mid = uuid.uuid4()
        params = {
            "material_id": str(mid),
            "source_type": "video",
            "source_url": "https://youtu.be/x",
        }

        await repo.create(
            tenant_id=tid,
            course_node_id=nid,
            job_type="document_processing",
            input_params=params,
            duration_sec=3300.0,
            subject_type="authored_document",
            subject_id=mid,
        )

        assert await repo.get_latest_ingest_duration_for_material(mid) == 3300.0

    async def test_latest_duration_for_material_none_when_absent(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Lookup returns None when no prior job carried a duration (legacy)."""
        repo = JobRepository(db_session)
        mid = uuid.uuid4()
        # A prior job for the material (matched by subject) but with NULL
        # duration — skipped by the ``duration_sec IS NOT NULL`` filter.
        await repo.create(
            tenant_id=seed_root_node.tenant_id,
            course_node_id=seed_root_node.id,
            job_type="document_processing",
            input_params={"material_id": str(mid), "source_type": "video"},
            subject_type="authored_document",
            subject_id=mid,
        )
        assert await repo.get_latest_ingest_duration_for_material(mid) is None
