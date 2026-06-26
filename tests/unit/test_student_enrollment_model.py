"""Tests for StudentEnrollment ORM model (Phase 6 T1, KD17)."""

from __future__ import annotations

from course_supporter.storage.orm import StudentEnrollment, _uuid7


class TestStudentEnrollmentModel:
    """StudentEnrollment column tests."""

    def test_create_minimal(self) -> None:
        """Minimal enrollment with required fields."""
        sid = _uuid7()
        nid = _uuid7()
        enrollment = StudentEnrollment(student_id=sid, course_node_id=nid)
        assert enrollment.student_id == sid
        assert enrollment.course_node_id == nid

    def test_no_soft_delete(self) -> None:
        """Plain Base, NOT SoftDeleteMixin — unbind is a row DELETE, not soft-delete."""
        columns = {c.name for c in StudentEnrollment.__table__.columns}
        assert "deleted_at" not in columns

    def test_repr(self) -> None:
        """__repr__ includes id, student_id, course_node_id."""
        eid = _uuid7()
        sid = _uuid7()
        nid = _uuid7()
        enrollment = StudentEnrollment(id=eid, student_id=sid, course_node_id=nid)
        r = repr(enrollment)
        assert "StudentEnrollment" in r
        assert str(sid) in r


class TestStudentEnrollmentForeignKeys:
    """StudentEnrollment FK configuration tests."""

    def test_student_id_fk_cascade(self) -> None:
        """student_id FK → students.id, CASCADE ondelete."""
        col = StudentEnrollment.__table__.c.student_id
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "students.id"
        assert fk.ondelete == "CASCADE"

    def test_course_node_id_fk_cascade(self) -> None:
        """course_node_id FK → course_nodes.id, CASCADE ondelete."""
        col = StudentEnrollment.__table__.c.course_node_id
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "course_nodes.id"
        assert fk.ondelete == "CASCADE"


class TestStudentEnrollmentConstraints:
    """StudentEnrollment uniqueness tests."""

    def test_composite_unique_student_course(self) -> None:
        """Unique index on (student_id, course_node_id) — one grant per pair."""
        indexes = {idx.name: idx for idx in StudentEnrollment.__table__.indexes}
        assert "uq_student_enrollment" in indexes
        idx = indexes["uq_student_enrollment"]
        assert idx.unique is True
        assert [c.name for c in idx.columns] == ["student_id", "course_node_id"]
