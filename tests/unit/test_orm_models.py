"""Tests for ORM model definitions (no DB required)."""

from course_supporter.storage.orm import (
    AuthoredDocument,
    Base,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
    ExternalServiceCall,
)


class TestORMModels:
    """Verify ORM models are correctly defined."""

    def test_all_tables_registered(self) -> None:
        """All expected tables are in Base metadata."""
        table_names = set(Base.metadata.tables.keys())
        expected = {
            "tenants",
            "api_keys",
            "course_nodes",
            "authored_documents",
            "document_summaries",
            "document_segments",
            "structure_snapshots",
            "jobs",
            "external_service_calls",
        }
        assert expected.issubset(table_names)

    def test_material_node_table_columns(self) -> None:
        """CourseNode table has expected columns."""
        columns = {c.name for c in CourseNode.__table__.columns}
        assert "id" in columns
        assert "tenant_id" in columns
        assert "title" in columns
        assert "parent_id" in columns
        assert "created_at" in columns
        assert "updated_at" in columns

    def test_material_entry_fk(self) -> None:
        """AuthoredDocument has FK to course_nodes."""
        fks = {fk.target_fullname for fk in AuthoredDocument.__table__.foreign_keys}
        assert "course_nodes.id" in fks

    def test_external_service_call_fks(self) -> None:
        """ExternalServiceCall has only the KD5 mandatory FK to jobs."""
        fks = {fk.target_fullname for fk in ExternalServiceCall.__table__.foreign_keys}
        assert fks == {"jobs.id"}

    def test_material_macro_section_fks(self) -> None:
        """DocumentSummary has FKs to entry and LLM call."""
        fks = {fk.target_fullname for fk in DocumentSummary.__table__.foreign_keys}
        assert "authored_documents.id" in fks
        assert "external_service_calls.id" in fks

    def test_material_segment_fks(self) -> None:
        """DocumentSegment has FKs to macro section and LLM call."""
        fks = {fk.target_fullname for fk in DocumentSegment.__table__.foreign_keys}
        assert "document_summaries.id" in fks
        assert "external_service_calls.id" in fks

    def test_ondelete_cascade_on_primary_foreign_keys(self) -> None:
        """Primary FK constraints use CASCADE ondelete."""
        # Check key ownership FKs (not nullable SET NULL FKs like job_id)
        cascade_fks = [
            (AuthoredDocument, "course_node_id"),
            (CourseNode, "parent_id"),
            (CourseNode, "tenant_id"),
            (DocumentSummary, "authored_document_id"),
            (DocumentSegment, "document_summary_id"),
        ]
        for model, col_name in cascade_fks:
            col = model.__table__.c[col_name]
            fk = next(iter(col.foreign_keys))
            assert fk.ondelete == "CASCADE", (
                f"{model.__tablename__}.{col_name} missing CASCADE ondelete"
            )

    def test_llm_call_fk_set_null(self) -> None:
        """llm_call_id FKs use SET NULL (preserves cost audit on call delete)."""
        set_null_fks = [
            (DocumentSummary, "llm_call_id"),
            (DocumentSegment, "llm_call_id"),
        ]
        for model, col_name in set_null_fks:
            col = model.__table__.c[col_name]
            fk = next(iter(col.foreign_keys))
            assert fk.ondelete == "SET NULL", (
                f"{model.__tablename__}.{col_name} must use SET NULL ondelete"
            )
