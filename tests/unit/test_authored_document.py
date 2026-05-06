"""Unit tests for AuthoredDocument ORM model — MagicMock-based, fast.

Tests model behavior including state derivation, FK relationships, and
field constraints. Does NOT verify DB-level behavior (real INSERT/UPDATE
paths, PostgreSQL trigger interactions) — those are covered in
``tests/storage/test_authored_document_repository.py`` (real-DB,
Amendment 33-aligned).
"""

from __future__ import annotations

from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    Job,
    _uuid7,
)


class TestAuthoredDocumentModel:
    """AuthoredDocument ORM column/default tests."""

    def test_create_minimal(self) -> None:
        """Minimal AuthoredDocument with required fields only."""
        entry = AuthoredDocument(
            course_node_id=_uuid7(),
            source_type="web",
            source_url="https://example.com/article",
        )

        assert entry.source_type == "web"
        assert entry.source_url == "https://example.com/article"
        assert entry.filename is None
        assert entry.raw_hash is None
        assert entry.raw_size_bytes is None
        assert entry.processed_at is None
        assert entry.job_id is None
        assert entry.pending_since is None
        assert entry.error_message is None

    def test_create_with_raw_layer(self) -> None:
        """AuthoredDocument with raw layer populated (file upload)."""
        entry = AuthoredDocument(
            course_node_id=_uuid7(),
            source_type="video",
            source_url="s3://bucket/video.mp4",
            filename="lecture-1.mp4",
            raw_hash="a" * 64,
            raw_size_bytes=1_048_576,
        )

        assert entry.filename == "lecture-1.mp4"
        assert entry.raw_hash == "a" * 64
        assert entry.raw_size_bytes == 1_048_576

    def test_pending_receipt_fields(self) -> None:
        """Pending receipt tracks in-flight job."""
        job_id = _uuid7()
        entry = AuthoredDocument(
            course_node_id=_uuid7(),
            source_type="presentation",
            source_url="s3://bucket/slides.pdf",
            job_id=job_id,
        )

        assert entry.job_id == job_id

    def test_order_default(self) -> None:
        """Order column defaults to 0."""
        col = AuthoredDocument.__table__.c.order
        assert col.default.arg == 0

    def test_source_url_max_length(self) -> None:
        """source_url accepts up to 2000 chars."""
        col = AuthoredDocument.__table__.c.source_url
        assert col.type.length == 2000  # type: ignore[union-attr]

    def test_filename_max_length(self) -> None:
        """filename accepts up to 500 chars."""
        col = AuthoredDocument.__table__.c.filename
        assert col.type.length == 500  # type: ignore[union-attr]

    def test_raw_hash_max_length(self) -> None:
        """raw_hash is 64 chars (SHA-256)."""
        col = AuthoredDocument.__table__.c.raw_hash
        assert col.type.length == 64  # type: ignore[union-attr]

    def test_repr(self) -> None:
        """__repr__ includes id, source_type, and node_id."""
        node_id = _uuid7()
        entry = AuthoredDocument(
            id=_uuid7(),
            course_node_id=node_id,
            source_type="web",
            source_url="https://example.com",
        )
        r = repr(entry)
        assert "AuthoredDocument" in r
        assert "web" in r
        assert str(node_id) in r

    def test_source_type_reuses_enum(self) -> None:
        """source_type uses source_type_enum."""
        col = AuthoredDocument.__table__.c.source_type
        assert col.type.name == "source_type_enum"  # type: ignore[union-attr]


class TestAuthoredDocumentForeignKeys:
    """AuthoredDocument FK configuration tests."""

    def test_node_id_fk(self) -> None:
        """node_id FK points to course_nodes.id."""
        col = AuthoredDocument.__table__.c.course_node_id
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "course_nodes.id"

    def test_node_id_cascade_delete(self) -> None:
        """node_id FK uses CASCADE ondelete."""
        col = AuthoredDocument.__table__.c.course_node_id
        fk = next(iter(col.foreign_keys))
        assert fk.ondelete == "CASCADE"

    def test_job_id_fk(self) -> None:
        """job_id FK points to jobs.id."""
        col = AuthoredDocument.__table__.c.job_id
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "jobs.id"

    def test_job_id_set_null(self) -> None:
        """job_id FK uses SET NULL ondelete."""
        col = AuthoredDocument.__table__.c.job_id
        fk = next(iter(col.foreign_keys))
        assert fk.ondelete == "SET NULL"

    def test_job_id_nullable(self) -> None:
        """job_id is nullable."""
        col = AuthoredDocument.__table__.c.job_id
        assert col.nullable is True


class TestAuthoredDocumentRelationships:
    """AuthoredDocument relationship configuration tests."""

    def test_node_relationship(self) -> None:
        """node relationship back_populates documents on CourseNode."""
        rel = AuthoredDocument.__mapper__.relationships["node"]
        assert rel.back_populates == "documents"

    def test_pending_job_relationship(self) -> None:
        """pending_job relationship to Job with back_populates."""
        rel = AuthoredDocument.__mapper__.relationships["pending_job"]
        assert rel.mapper.class_ is Job
        assert rel.back_populates == "authored_documents"

    def test_job_has_authored_documents_relationship(self) -> None:
        """Job.authored_documents back_populates pending_job."""
        rel = Job.__mapper__.relationships["authored_documents"]
        assert rel.back_populates == "pending_job"

    def test_course_node_has_documents(self) -> None:
        """CourseNode.documents relationship back_populates node."""
        rel = CourseNode.__mapper__.relationships["documents"]
        assert rel.back_populates == "node"


class TestMaterialState:
    """AuthoredDocument.state derived property tests."""

    def test_state_pending(self) -> None:
        """PENDING when job_id is set."""
        entry = AuthoredDocument(
            course_node_id=_uuid7(),
            source_type="web",
            source_url="https://example.com",
            job_id=_uuid7(),
        )
        assert entry.state.value == "pending"

    def test_state_error(self) -> None:
        """ERROR when error_message is set (highest priority)."""
        entry = AuthoredDocument(
            course_node_id=_uuid7(),
            source_type="web",
            source_url="https://example.com",
            error_message="LLM timeout",
        )
        assert entry.state.value == "error"

    def test_state_error_takes_priority_over_pending(self) -> None:
        """ERROR takes priority over PENDING."""
        entry = AuthoredDocument(
            course_node_id=_uuid7(),
            source_type="web",
            source_url="https://example.com",
            job_id=_uuid7(),
            error_message="failed",
        )
        assert entry.state.value == "error"


class TestAuthoredDocumentIndexes:
    """AuthoredDocument index/constraint tests."""

    def test_node_id_indexed(self) -> None:
        """node_id column is indexed."""
        col = AuthoredDocument.__table__.c.course_node_id
        assert col.index is True

    def test_job_id_indexed(self) -> None:
        """job_id column is indexed."""
        col = AuthoredDocument.__table__.c.job_id
        assert col.index is True
