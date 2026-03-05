"""Tests for ORM model definitions (no DB required)."""

from course_supporter.storage.orm import (
    Base,
    ExternalServiceCall,
    MaterialEntry,
    MaterialNode,
    SlideVideoMapping,
)


class TestORMModels:
    """Verify ORM models are correctly defined."""

    def test_all_tables_registered(self) -> None:
        """All expected tables are in Base metadata."""
        table_names = set(Base.metadata.tables.keys())
        expected = {
            "tenants",
            "api_keys",
            "material_nodes",
            "material_entries",
            "slide_video_mappings",
            "structure_snapshots",
            "jobs",
            "external_service_calls",
        }
        assert expected.issubset(table_names)

    def test_material_node_table_columns(self) -> None:
        """MaterialNode table has expected columns."""
        columns = {c.name for c in MaterialNode.__table__.columns}
        assert "id" in columns
        assert "tenant_id" in columns
        assert "title" in columns
        assert "parent_id" in columns
        assert "created_at" in columns
        assert "updated_at" in columns

    def test_material_entry_fk(self) -> None:
        """MaterialEntry has FK to material_nodes."""
        fks = {fk.target_fullname for fk in MaterialEntry.__table__.foreign_keys}
        assert "material_nodes.id" in fks

    def test_external_service_call_fks(self) -> None:
        """ExternalServiceCall has FK to tenants and jobs."""
        fks = {fk.target_fullname for fk in ExternalServiceCall.__table__.foreign_keys}
        assert "tenants.id" in fks
        assert "jobs.id" in fks

    def test_slide_video_mapping_fk(self) -> None:
        """SlideVideoMapping has FK to material_nodes and material_entries."""
        fks = {fk.target_fullname for fk in SlideVideoMapping.__table__.foreign_keys}
        assert "material_nodes.id" in fks
        assert "material_entries.id" in fks

    def test_ondelete_cascade_on_primary_foreign_keys(self) -> None:
        """Primary FK constraints use CASCADE ondelete."""
        # Check key ownership FKs (not nullable SET NULL FKs like pending_job_id)
        cascade_fks = [
            (MaterialEntry, "node_id"),
            (MaterialNode, "parent_id"),
            (MaterialNode, "tenant_id"),
            (SlideVideoMapping, "node_id"),
        ]
        for model, col_name in cascade_fks:
            col = model.__table__.c[col_name]
            fk = next(iter(col.foreign_keys))
            assert fk.ondelete == "CASCADE", (
                f"{model.__tablename__}.{col_name} missing CASCADE ondelete"
            )
