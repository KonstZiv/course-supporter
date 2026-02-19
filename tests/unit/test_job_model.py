"""Tests for Job ORM model and JobRepository logic."""

from course_supporter.storage.job_repository import JOB_TRANSITIONS, JobRepository
from course_supporter.storage.orm import Base, Job


class TestJobModel:
    """Verify Job ORM model definition (no DB required)."""

    def test_table_registered(self) -> None:
        assert "jobs" in Base.metadata.tables

    def test_columns(self) -> None:
        columns = {c.name for c in Job.__table__.columns}
        expected = {
            "id",
            "course_id",
            "node_id",
            "job_type",
            "priority",
            "status",
            "arq_job_id",
            "input_params",
            "result_material_id",
            "result_snapshot_id",
            "depends_on",
            "error_message",
            "queued_at",
            "started_at",
            "completed_at",
            "estimated_at",
        }
        assert expected.issubset(columns)

    def test_course_fk(self) -> None:
        fks = {fk.target_fullname for fk in Job.__table__.foreign_keys}
        assert "courses.id" in fks

    def test_check_constraint_exists(self) -> None:
        constraints = {c.name for c in Job.__table__.constraints if c.name}
        assert "chk_job_result_exclusive" in constraints

    def test_indexes(self) -> None:
        indexed_cols = set()
        for idx in Job.__table__.indexes:
            for col in idx.columns:
                indexed_cols.add(col.name)
        assert "course_id" in indexed_cols
        assert "node_id" in indexed_cols
        assert "status" in indexed_cols

    def test_default_status(self) -> None:
        col = Job.__table__.columns["status"]
        assert col.default is not None
        assert col.default.arg == "queued"

    def test_default_priority(self) -> None:
        col = Job.__table__.columns["priority"]
        assert col.default is not None
        assert col.default.arg == "normal"


class TestJobTransitions:
    """Test job status transition rules."""

    def test_queued_to_active(self) -> None:
        assert "active" in JOB_TRANSITIONS["queued"]

    def test_queued_to_cancelled(self) -> None:
        assert "cancelled" in JOB_TRANSITIONS["queued"]

    def test_active_to_complete(self) -> None:
        assert "complete" in JOB_TRANSITIONS["active"]

    def test_active_to_failed(self) -> None:
        assert "failed" in JOB_TRANSITIONS["active"]

    def test_complete_is_terminal(self) -> None:
        assert JOB_TRANSITIONS["complete"] == set()

    def test_cancelled_is_terminal(self) -> None:
        assert JOB_TRANSITIONS["cancelled"] == set()

    def test_failed_can_retry(self) -> None:
        assert "queued" in JOB_TRANSITIONS["failed"]

    def test_invalid_transition_not_allowed(self) -> None:
        assert "complete" not in JOB_TRANSITIONS["queued"]
        assert "queued" not in JOB_TRANSITIONS["active"]

    def test_all_statuses_covered(self) -> None:
        expected = {"queued", "active", "complete", "failed", "cancelled"}
        assert set(JOB_TRANSITIONS.keys()) == expected


class TestJobRepositoryInit:
    """Test JobRepository initialization."""

    def test_accepts_session(self) -> None:
        from unittest.mock import MagicMock

        session = MagicMock()
        repo = JobRepository(session)
        assert repo._session is session
