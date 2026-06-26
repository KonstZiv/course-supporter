"""Tests for StudentCredential ORM model (Phase 6 T1, KD17)."""

from __future__ import annotations

from course_supporter.storage.orm import StudentCredential, _uuid7


class TestStudentCredentialModel:
    """StudentCredential column/default tests."""

    def test_create_minimal(self) -> None:
        """Minimal credential with required fields."""
        sid = _uuid7()
        tid = _uuid7()
        cred = StudentCredential(
            student_id=sid,
            tenant_id=tid,
            login="alice",
            password_hash="$argon2id$v=19$...",
        )
        assert cred.student_id == sid
        assert cred.tenant_id == tid
        assert cred.login == "alice"
        assert cred.password_hash == "$argon2id$v=19$..."

    def test_login_max_length(self) -> None:
        """login column accepts up to 200 chars."""
        col = StudentCredential.__table__.c.login
        assert col.type.length == 200  # type: ignore[attr-defined]

    def test_is_active_defaults_true(self) -> None:
        """is_active defaults to True (active credential on create)."""
        col = StudentCredential.__table__.c.is_active
        assert col.default is not None
        assert col.default.arg is True  # type: ignore[union-attr]

    def test_password_hash_not_nullable(self) -> None:
        """password_hash is a required (Text) column."""
        col = StudentCredential.__table__.c.password_hash
        assert col.nullable is False

    def test_no_soft_delete(self) -> None:
        """Plain Base, NOT SoftDeleteMixin — lifecycle is is_active, not deleted_at."""
        columns = {c.name for c in StudentCredential.__table__.columns}
        assert "deleted_at" not in columns

    def test_repr(self) -> None:
        """__repr__ includes id, student_id, login, is_active."""
        cid = _uuid7()
        sid = _uuid7()
        cred = StudentCredential(id=cid, student_id=sid, login="bob", password_hash="x")
        r = repr(cred)
        assert "StudentCredential" in r
        assert "bob" in r


class TestStudentCredentialForeignKeys:
    """StudentCredential FK configuration tests."""

    def test_student_id_fk_cascade(self) -> None:
        """student_id FK → students.id, CASCADE ondelete."""
        col = StudentCredential.__table__.c.student_id
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "students.id"
        assert fk.ondelete == "CASCADE"

    def test_tenant_id_fk_cascade(self) -> None:
        """tenant_id FK → tenants.id, CASCADE ondelete."""
        col = StudentCredential.__table__.c.tenant_id
        fk = next(iter(col.foreign_keys))
        assert fk.target_fullname == "tenants.id"
        assert fk.ondelete == "CASCADE"


class TestStudentCredentialConstraints:
    """StudentCredential index/uniqueness tests."""

    def test_student_id_unique(self) -> None:
        """student_id is UNIQUE (1:1-optional credential per Student)."""
        col = StudentCredential.__table__.c.student_id
        assert col.unique is True

    def test_composite_unique_tenant_login(self) -> None:
        """Composite unique index on (tenant_id, login) — login unique per tenant."""
        indexes = {idx.name: idx for idx in StudentCredential.__table__.indexes}
        assert "uq_student_credential_tenant_login" in indexes
        idx = indexes["uq_student_credential_tenant_login"]
        assert idx.unique is True
        assert [c.name for c in idx.columns] == ["tenant_id", "login"]
