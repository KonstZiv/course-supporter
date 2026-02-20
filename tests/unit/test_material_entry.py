"""Tests for MaterialEntry ORM model."""

from __future__ import annotations

from course_supporter.storage.orm import (
    Job,
    MaterialEntry,
    MaterialNode,
    _uuid7,
)


class TestMaterialEntryModel:
    """MaterialEntry ORM column/default tests."""

    def test_create_minimal(self) -> None:
        """Minimal MaterialEntry with required fields only."""
        entry = MaterialEntry(
            node_id=_uuid7(),
            source_type="web",
            source_url="https://example.com/article",
        )

        assert entry.source_type == "web"
        assert entry.source_url == "https://example.com/article"
        assert entry.filename is None
        assert entry.raw_hash is None
        assert entry.raw_size_bytes is None
        assert entry.processed_hash is None
        assert entry.processed_content is None
        assert entry.processed_at is None
        assert entry.pending_job_id is None
        assert entry.pending_since is None
        assert entry.content_fingerprint is None
        assert entry.error_message is None

    def test_create_with_raw_layer(self) -> None:
        """MaterialEntry with raw layer populated (file upload)."""
        entry = MaterialEntry(
            node_id=_uuid7(),
            source_type="video",
            source_url="s3://bucket/video.mp4",
            filename="lecture-1.mp4",
            raw_hash="a" * 64,
            raw_size_bytes=1_048_576,
        )

        assert entry.filename == "lecture-1.mp4"
        assert entry.raw_hash == "a" * 64
        assert entry.raw_size_bytes == 1_048_576

    def test_create_with_processed_layer(self) -> None:
        """MaterialEntry with processed layer populated."""
        entry = MaterialEntry(
            node_id=_uuid7(),
            source_type="text",
            source_url="s3://bucket/notes.md",
            processed_hash="b" * 64,
            processed_content='{"sections": []}',
        )

        assert entry.processed_hash == "b" * 64
        assert entry.processed_content == '{"sections": []}'

    def test_pending_receipt_fields(self) -> None:
        """Pending receipt tracks in-flight job."""
        job_id = _uuid7()
        entry = MaterialEntry(
            node_id=_uuid7(),
            source_type="presentation",
            source_url="s3://bucket/slides.pdf",
            pending_job_id=job_id,
        )

        assert entry.pending_job_id == job_id

    def test_order_default(self) -> None:
        """Order column defaults to 0."""
        col = MaterialEntry.__table__.c.order
        assert col.default.arg == 0

    def test_source_url_max_length(self) -> None:
        """source_url accepts up to 2000 chars."""
        col = MaterialEntry.__table__.c.source_url
        assert col.type.length == 2000  # type: ignore[union-attr]

    def test_filename_max_length(self) -> None:
        """filename accepts up to 500 chars."""
        col = MaterialEntry.__table__.c.filename
        assert col.type.length == 500  # type: ignore[union-attr]

    def test_hash_fields_max_length(self) -> None:
        """Hash fields (raw, processed, fingerprint) are 64 chars (SHA-256)."""
        table = MaterialEntry.__table__
        for col_name in ("raw_hash", "processed_hash", "content_fingerprint"):
            col = table.c[col_name]
            assert col.type.length == 64, f"{col_name} should be 64 chars"  # type: ignore[union-attr]

    def test_repr(self) -> None:
        """__repr__ includes id, source_type, and node_id."""
        node_id = _uuid7()
        entry = MaterialEntry(
            id=_uuid7(),
            node_id=node_id,
            source_type="web",
            source_url="https://example.com",
        )
        r = repr(entry)
        assert "MaterialEntry" in r
        assert "web" in r
        assert str(node_id) in r

    def test_source_type_reuses_enum(self) -> None:
        """source_type uses existing source_type_enum (shared with SourceMaterial)."""
        col = MaterialEntry.__table__.c.source_type
        assert col.type.name == "source_type_enum"  # type: ignore[union-attr]


class TestMaterialEntryForeignKeys:
    """MaterialEntry FK configuration tests."""

    def test_node_id_fk(self) -> None:
        """node_id FK points to material_nodes.id."""
        col = MaterialEntry.__table__.c.node_id
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "material_nodes.id"

    def test_node_id_cascade_delete(self) -> None:
        """node_id FK uses CASCADE ondelete."""
        col = MaterialEntry.__table__.c.node_id
        fk = next(iter(col.foreign_keys))
        assert fk.ondelete == "CASCADE"

    def test_pending_job_id_fk(self) -> None:
        """pending_job_id FK points to jobs.id."""
        col = MaterialEntry.__table__.c.pending_job_id
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "jobs.id"

    def test_pending_job_id_set_null(self) -> None:
        """pending_job_id FK uses SET NULL ondelete."""
        col = MaterialEntry.__table__.c.pending_job_id
        fk = next(iter(col.foreign_keys))
        assert fk.ondelete == "SET NULL"

    def test_pending_job_id_nullable(self) -> None:
        """pending_job_id is nullable."""
        col = MaterialEntry.__table__.c.pending_job_id
        assert col.nullable is True


class TestMaterialEntryRelationships:
    """MaterialEntry relationship configuration tests."""

    def test_node_relationship(self) -> None:
        """node relationship back_populates materials on MaterialNode."""
        rel = MaterialEntry.__mapper__.relationships["node"]
        assert rel.back_populates == "materials"

    def test_pending_job_relationship(self) -> None:
        """pending_job relationship to Job with back_populates."""
        rel = MaterialEntry.__mapper__.relationships["pending_job"]
        assert rel.mapper.class_ is Job
        assert rel.back_populates == "material_entries"

    def test_job_has_material_entries_relationship(self) -> None:
        """Job.material_entries back_populates pending_job."""
        rel = Job.__mapper__.relationships["material_entries"]
        assert rel.back_populates == "pending_job"

    def test_material_node_has_materials(self) -> None:
        """MaterialNode.materials relationship back_populates node."""
        rel = MaterialNode.__mapper__.relationships["materials"]
        assert rel.back_populates == "node"

    def test_material_node_materials_cascade(self) -> None:
        """MaterialNode -> materials cascade includes delete-orphan."""
        rel = MaterialNode.__mapper__.relationships["materials"]
        assert "delete-orphan" in rel.cascade


class TestMaterialEntryIndexes:
    """MaterialEntry index/constraint tests."""

    def test_node_id_indexed(self) -> None:
        """node_id column is indexed."""
        col = MaterialEntry.__table__.c.node_id
        assert col.index is True

    def test_pending_job_id_indexed(self) -> None:
        """pending_job_id column is indexed."""
        col = MaterialEntry.__table__.c.pending_job_id
        assert col.index is True
