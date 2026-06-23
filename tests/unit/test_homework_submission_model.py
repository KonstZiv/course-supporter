"""Tests for HomeworkSubmission ORM model and status machine."""

from __future__ import annotations

import pytest

from course_supporter.storage.homework_repository import HOMEWORK_TRANSITIONS
from course_supporter.storage.orm import (
    CourseNode,
    HomeworkStatus,
    HomeworkSubmission,
    Job,
    Student,
    Tenant,
    _uuid7,
)


class TestHomeworkSubmissionModel:
    """HomeworkSubmission ORM column/default tests."""

    def test_create_minimal(self) -> None:
        """Minimal HomeworkSubmission with required fields only."""
        sub = HomeworkSubmission(
            tenant_id=_uuid7(),
            student_id=_uuid7(),
            course_node_id=_uuid7(),
            node_id=_uuid7(),
            file_url="s3://bucket/homework/file.py",
            file_type="text/x-python",
        )
        assert sub.file_url == "s3://bucket/homework/file.py"
        assert sub.file_type == "text/x-python"
        assert sub.original_filename is None
        assert sub.task_hint_id is None
        assert sub.matched_task_id is None
        assert sub.safety_result is None
        assert sub.review_result is None
        assert sub.review_markdown is None
        assert sub.webhook_url is None
        assert sub.webhook_delivered_at is None
        assert sub.error_message is None
        assert sub.job_id is None

    def test_create_with_all_fields(self) -> None:
        """HomeworkSubmission with all optional fields populated."""
        task_hint = _uuid7()
        webhook = "https://example.com/webhook"
        sub = HomeworkSubmission(
            tenant_id=_uuid7(),
            student_id=_uuid7(),
            course_node_id=_uuid7(),
            node_id=_uuid7(),
            file_url="s3://bucket/homework/archive.zip",
            file_type="application/zip",
            original_filename="homework-1.zip",
            task_hint_id=task_hint,
            webhook_url=webhook,
        )
        assert sub.original_filename == "homework-1.zip"
        assert sub.task_hint_id == task_hint
        assert sub.webhook_url == webhook

    def test_jsonb_fields_round_trip(self) -> None:
        """JSONB result fields accept and return dicts."""
        safety = {"safe": True, "flags": []}
        review = {"score": 85, "passed": True, "sections": ["good"]}
        sub = HomeworkSubmission(
            tenant_id=_uuid7(),
            student_id=_uuid7(),
            course_node_id=_uuid7(),
            node_id=_uuid7(),
            file_url="s3://bucket/file.py",
            file_type="text/x-python",
            safety_result=safety,
            review_result=review,
        )
        assert sub.safety_result == safety
        assert sub.review_result == review

    def test_file_url_max_length(self) -> None:
        """file_url column accepts up to 2000 chars."""
        col = HomeworkSubmission.__table__.c.file_url
        assert col.type.length == 2000  # type: ignore[union-attr]

    def test_original_filename_max_length(self) -> None:
        """original_filename column accepts up to 500 chars."""
        col = HomeworkSubmission.__table__.c.original_filename
        assert col.type.length == 500  # type: ignore[union-attr]

    def test_status_max_length(self) -> None:
        """status column accepts up to 30 chars."""
        col = HomeworkSubmission.__table__.c.status
        assert col.type.length == 30  # type: ignore[union-attr]

    def test_status_default(self) -> None:
        """status column default is 'received'."""
        col = HomeworkSubmission.__table__.c.status
        assert col.default.arg == "received"

    def test_repr(self) -> None:
        """__repr__ includes id, status, and student_id."""
        sid = _uuid7()
        sub = HomeworkSubmission(
            id=_uuid7(),
            tenant_id=_uuid7(),
            student_id=sid,
            course_node_id=_uuid7(),
            node_id=_uuid7(),
            file_url="s3://bucket/file.py",
            file_type="text/x-python",
            status="received",
        )
        r = repr(sub)
        assert "HomeworkSubmission" in r
        assert "received" in r
        assert str(sid) in r


class TestHomeworkSubmissionForeignKeys:
    """HomeworkSubmission FK configuration tests."""

    def test_tenant_id_fk(self) -> None:
        """tenant_id FK points to tenants.id with CASCADE."""
        col = HomeworkSubmission.__table__.c.tenant_id
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "tenants.id"
        assert fk.ondelete == "CASCADE"

    def test_student_id_fk(self) -> None:
        """student_id FK points to students.id with CASCADE."""
        col = HomeworkSubmission.__table__.c.student_id
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "students.id"
        assert fk.ondelete == "CASCADE"

    def test_course_node_id_fk(self) -> None:
        """course_node_id FK points to course_nodes.id with CASCADE."""
        col = HomeworkSubmission.__table__.c.course_node_id
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "course_nodes.id"
        assert fk.ondelete == "CASCADE"

    def test_node_id_fk(self) -> None:
        """node_id FK points to course_nodes.id with CASCADE."""
        col = HomeworkSubmission.__table__.c.node_id
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "course_nodes.id"
        assert fk.ondelete == "CASCADE"

    def test_matched_task_id_no_fk_post_c9_4(self) -> None:
        """matched_task_id has no FK post-C9.4 (orphan column per DD-2.1-AG)."""
        col = HomeworkSubmission.__table__.c.matched_task_id
        assert not col.foreign_keys, (
            "matched_task_id FK to structure_nodes_editable dropped in C9.4 "
            "with the table; column retained per DD-2.1-AG for Phase 4 reroute."
        )

    def test_job_id_fk(self) -> None:
        """job_id FK with SET NULL."""
        col = HomeworkSubmission.__table__.c.job_id
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "jobs.id"
        assert fk.ondelete == "SET NULL"

    def test_matched_task_id_nullable(self) -> None:
        """matched_task_id is nullable."""
        col = HomeworkSubmission.__table__.c.matched_task_id
        assert col.nullable is True

    def test_job_id_nullable(self) -> None:
        """job_id is nullable."""
        col = HomeworkSubmission.__table__.c.job_id
        assert col.nullable is True


class TestHomeworkSubmissionIndexes:
    """HomeworkSubmission index tests."""

    @pytest.mark.parametrize(
        "col_name",
        [
            "tenant_id",
            "student_id",
            "course_node_id",
            "node_id",
            "matched_task_id",
            "status",
            "job_id",
        ],
    )
    def test_indexed_columns(self, col_name: str) -> None:
        """Key columns are indexed for query performance."""
        col = HomeworkSubmission.__table__.c[col_name]
        assert col.index is True, f"{col_name} should be indexed"


class TestHomeworkSubmissionRelationships:
    """HomeworkSubmission relationship configuration tests."""

    def test_tenant_relationship(self) -> None:
        """tenant relationship exists."""
        rel = HomeworkSubmission.__mapper__.relationships["tenant"]
        assert rel.mapper.class_ is Tenant

    def test_student_relationship(self) -> None:
        """student relationship back_populates submissions."""
        rel = HomeworkSubmission.__mapper__.relationships["student"]
        assert rel.mapper.class_ is Student
        assert rel.back_populates == "submissions"

    def test_course_node_relationship(self) -> None:
        """course_node relationship to CourseNode."""
        rel = HomeworkSubmission.__mapper__.relationships["course_node"]
        assert rel.mapper.class_ is CourseNode

    def test_node_relationship(self) -> None:
        """node relationship to CourseNode."""
        rel = HomeworkSubmission.__mapper__.relationships["node"]
        assert rel.mapper.class_ is CourseNode

    def test_job_relationship(self) -> None:
        """job relationship to Job."""
        rel = HomeworkSubmission.__mapper__.relationships["job"]
        assert rel.mapper.class_ is Job


class TestHomeworkStatusEnum:
    """HomeworkStatus StrEnum tests."""

    def test_all_statuses_in_transitions(self) -> None:
        """Every HomeworkStatus value appears in HOMEWORK_TRANSITIONS."""
        for status in HomeworkStatus:
            assert status.value in HOMEWORK_TRANSITIONS, (
                f"Status '{status.value}' missing from HOMEWORK_TRANSITIONS"
            )

    def test_all_transition_targets_are_valid(self) -> None:
        """Every transition target is a valid HomeworkStatus value."""
        valid = {s.value for s in HomeworkStatus}
        for source, targets in HOMEWORK_TRANSITIONS.items():
            assert source in valid, f"Source '{source}' not a valid status"
            for target in targets:
                assert target in valid, (
                    f"Target '{target}' (from '{source}') not a valid status"
                )

    def test_terminal_states(self) -> None:
        """delivered and rejected are terminal (no outgoing transitions)."""
        assert HOMEWORK_TRANSITIONS["delivered"] == set()
        assert HOMEWORK_TRANSITIONS["rejected"] == set()

    def test_failed_can_retry(self) -> None:
        """failed can transition back to received (retry)."""
        assert "received" in HOMEWORK_TRANSITIONS["failed"]

    def test_any_active_state_can_fail(self) -> None:
        """All non-terminal states can transition to failed."""
        excluded = {
            HomeworkStatus.DELIVERED.value,
            HomeworkStatus.REJECTED.value,
            HomeworkStatus.FAILED.value,
        }
        non_terminal = {s.value for s in HomeworkStatus if s.value not in excluded}
        for state in non_terminal:
            assert "failed" in HOMEWORK_TRANSITIONS[state], (
                f"State '{state}' should be able to transition to 'failed'"
            )
