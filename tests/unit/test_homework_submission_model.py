"""Tests for HomeworkSubmission ORM model and status machine."""

from __future__ import annotations

import pytest

from course_supporter.storage.homework_repository import HOMEWORK_TRANSITIONS
from course_supporter.storage.orm import (
    AuthoredDocument,
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
            authored_document_id=_uuid7(),
            file_url="s3://bucket/homework/file.py",
            file_type="text/x-python",
        )
        assert sub.file_url == "s3://bucket/homework/file.py"
        assert sub.file_type == "text/x-python"
        assert sub.original_filename is None
        assert sub.safety_result is None
        assert sub.review_result is None
        assert sub.review_markdown is None
        assert sub.score is None
        assert sub.student_note is None
        assert sub.webhook_url is None
        assert sub.webhook_delivered_at is None
        assert sub.error_message is None
        assert sub.job_id is None

    def test_create_with_all_fields(self) -> None:
        """HomeworkSubmission with all optional fields populated."""
        authored_document = _uuid7()
        webhook = "https://example.com/webhook"
        sub = HomeworkSubmission(
            tenant_id=_uuid7(),
            student_id=_uuid7(),
            course_node_id=_uuid7(),
            node_id=_uuid7(),
            authored_document_id=authored_document,
            file_url="s3://bucket/homework/archive.zip",
            file_type="application/zip",
            original_filename="homework-1.zip",
            webhook_url=webhook,
        )
        assert sub.original_filename == "homework-1.zip"
        assert sub.authored_document_id == authored_document
        assert sub.webhook_url == webhook

    def test_jsonb_fields_round_trip(self) -> None:
        """JSONB result fields accept and return dicts."""
        safety = {"safe": True, "flags": []}
        sanity = {"verdict": "match", "confidence": 0.92, "reason": "on task"}
        review = {"score": 85, "passed": True, "sections": ["good"]}
        sub = HomeworkSubmission(
            tenant_id=_uuid7(),
            student_id=_uuid7(),
            course_node_id=_uuid7(),
            node_id=_uuid7(),
            authored_document_id=_uuid7(),
            file_url="s3://bucket/file.py",
            file_type="text/x-python",
            safety_result=safety,
            sanity_result=sanity,
            review_result=review,
        )
        assert sub.safety_result == safety
        assert sub.sanity_result == sanity
        assert sub.review_result == review

    def test_sanity_result_nullable(self) -> None:
        """sanity_result (T7 gate verdict) is None until the sanity stage runs."""
        sub = HomeworkSubmission(
            tenant_id=_uuid7(),
            student_id=_uuid7(),
            course_node_id=_uuid7(),
            node_id=_uuid7(),
            authored_document_id=_uuid7(),
            file_url="s3://bucket/file.py",
            file_type="text/x-python",
        )
        assert sub.sanity_result is None

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

    def test_score_nullable(self) -> None:
        """score (D1, final post-D10 grade) is nullable until reviewed."""
        col = HomeworkSubmission.__table__.c.score
        assert col.nullable is True

    def test_student_note_nullable(self) -> None:
        """student_note (D7-local) is a nullable free-text column."""
        col = HomeworkSubmission.__table__.c.student_note
        assert col.nullable is True

    def test_score_range_check_constraint(self) -> None:
        """ck_homework_score_range guards score to NULL or 0-100 (Q2)."""
        from sqlalchemy import CheckConstraint

        checks = {
            c.name
            for c in HomeworkSubmission.__table__.constraints
            if isinstance(c, CheckConstraint)
        }
        assert "ck_homework_score_range" in checks

    def test_delivery_mode_default(self) -> None:
        """delivery_mode defaults to 'webhook' (mode-1, unchanged) — Phase 6 T2."""
        col = HomeworkSubmission.__table__.c.delivery_mode
        assert col.default.arg == "webhook"  # type: ignore[union-attr]
        assert col.server_default.arg == "webhook"  # type: ignore[union-attr]

    def test_delivery_mode_not_nullable(self) -> None:
        """delivery_mode is required (NOT NULL — every submission has a mode)."""
        col = HomeworkSubmission.__table__.c.delivery_mode
        assert col.nullable is False

    def test_delivery_mode_max_length(self) -> None:
        """delivery_mode column accepts up to 20 chars."""
        col = HomeworkSubmission.__table__.c.delivery_mode
        assert col.type.length == 20  # type: ignore[union-attr]

    def test_delivery_mode_check_constraint(self) -> None:
        """ck_homework_delivery_mode restricts delivery_mode to webhook/in_app."""
        from sqlalchemy import CheckConstraint

        checks = {
            c.name
            for c in HomeworkSubmission.__table__.constraints
            if isinstance(c, CheckConstraint)
        }
        assert "ck_homework_delivery_mode" in checks

    def test_repr(self) -> None:
        """__repr__ includes id, status, and student_id."""
        sid = _uuid7()
        sub = HomeworkSubmission(
            id=_uuid7(),
            tenant_id=_uuid7(),
            student_id=sid,
            course_node_id=_uuid7(),
            node_id=_uuid7(),
            authored_document_id=_uuid7(),
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

    def test_authored_document_id_fk(self) -> None:
        """authored_document_id FK points to authored_documents.id with RESTRICT."""
        col = HomeworkSubmission.__table__.c.authored_document_id
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "authored_documents.id"
        assert fk.ondelete == "RESTRICT"

    def test_job_id_fk(self) -> None:
        """job_id FK with SET NULL."""
        col = HomeworkSubmission.__table__.c.job_id
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "jobs.id"
        assert fk.ondelete == "SET NULL"

    def test_authored_document_id_not_nullable(self) -> None:
        """authored_document_id is the required anchor (NOT NULL)."""
        col = HomeworkSubmission.__table__.c.authored_document_id
        assert col.nullable is False

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
            "authored_document_id",
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

    def test_authored_document_relationship(self) -> None:
        """authored_document relationship to AuthoredDocument (the task anchor)."""
        rel = HomeworkSubmission.__mapper__.relationships["authored_document"]
        assert rel.mapper.class_ is AuthoredDocument

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
        """delivered, rejected, and mismatch are terminal (no outgoing)."""
        assert HOMEWORK_TRANSITIONS["delivered"] == set()
        assert HOMEWORK_TRANSITIONS["rejected"] == set()
        assert HOMEWORK_TRANSITIONS["mismatch"] == set()

    def test_pipeline_milestones_chain(self) -> None:
        """The happy-path pipeline chains safety_ok → sanity_ok → reviewing."""
        assert "safety_ok" in HOMEWORK_TRANSITIONS["received"]
        assert "sanity_ok" in HOMEWORK_TRANSITIONS["safety_ok"]
        assert "reviewing" in HOMEWORK_TRANSITIONS["sanity_ok"]

    def test_gate_terminals_reachable(self) -> None:
        """Safety rejects from received; sanity mismatches from safety_ok."""
        assert "rejected" in HOMEWORK_TRANSITIONS["received"]
        assert "mismatch" in HOMEWORK_TRANSITIONS["safety_ok"]

    def test_failed_can_retry(self) -> None:
        """failed can transition back to received (retry)."""
        assert "received" in HOMEWORK_TRANSITIONS["failed"]

    def test_any_active_state_can_fail(self) -> None:
        """All non-terminal states can transition to failed."""
        excluded = {
            HomeworkStatus.DELIVERED.value,
            HomeworkStatus.REJECTED.value,
            HomeworkStatus.MISMATCH.value,
            HomeworkStatus.FAILED.value,
        }
        non_terminal = {s.value for s in HomeworkStatus if s.value not in excluded}
        for state in non_terminal:
            assert "failed" in HOMEWORK_TRANSITIONS[state], (
                f"State '{state}' should be able to transition to 'failed'"
            )
