"""Tests for Student ORM model."""

from __future__ import annotations

from course_supporter.storage.orm import Student, Tenant, _uuid7


class TestStudentModel:
    """Student ORM column/default tests."""

    def test_create_minimal(self) -> None:
        """Minimal Student with required fields only."""
        tid = _uuid7()
        student = Student(
            tenant_id=tid,
            external_id="ext-123",
        )
        assert student.tenant_id == tid
        assert student.external_id == "ext-123"
        assert student.metadata_ is None

    def test_create_with_metadata(self) -> None:
        """Student with metadata populated."""
        student = Student(
            tenant_id=_uuid7(),
            external_id="ext-456",
            metadata_={"name": "John", "email": "john@example.com"},
        )
        assert student.metadata_ == {"name": "John", "email": "john@example.com"}

    def test_external_id_max_length(self) -> None:
        """external_id column accepts up to 200 chars."""
        col = Student.__table__.c.external_id
        assert col.type.length == 200  # type: ignore[union-attr]

    def test_repr(self) -> None:
        """__repr__ includes id, tenant_id, and external_id."""
        sid = _uuid7()
        tid = _uuid7()
        student = Student(id=sid, tenant_id=tid, external_id="ext-789")
        r = repr(student)
        assert "Student" in r
        assert str(tid) in r
        assert "ext-789" in r


class TestStudentForeignKeys:
    """Student FK configuration tests."""

    def test_tenant_id_fk(self) -> None:
        """tenant_id FK points to tenants.id."""
        col = Student.__table__.c.tenant_id
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "tenants.id"

    def test_tenant_id_cascade_delete(self) -> None:
        """tenant_id FK uses CASCADE ondelete."""
        col = Student.__table__.c.tenant_id
        fk = next(iter(col.foreign_keys))
        assert fk.ondelete == "CASCADE"


class TestStudentIndexes:
    """Student index/constraint tests."""

    def test_tenant_id_indexed(self) -> None:
        """tenant_id column is indexed."""
        col = Student.__table__.c.tenant_id
        assert col.index is True

    def test_unique_tenant_external_id(self) -> None:
        """Unique composite index on (tenant_id, external_id) exists."""
        indexes = {idx.name for idx in Student.__table__.indexes}
        assert "uq_student_tenant_external" in indexes


class TestStudentRelationships:
    """Student relationship configuration tests."""

    def test_tenant_relationship(self) -> None:
        """tenant relationship exists."""
        rel = Student.__mapper__.relationships["tenant"]
        assert rel.mapper.class_ is Tenant

    def test_submissions_relationship(self) -> None:
        """submissions relationship back_populates student."""
        rel = Student.__mapper__.relationships["submissions"]
        assert rel.back_populates == "student"
        assert "delete-orphan" in rel.cascade
