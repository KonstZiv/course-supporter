"""Tests for ORM model definitions (no DB required)."""

from course_supporter.storage.orm import (
    Base,
    Concept,
    Course,
    Exercise,
    Lesson,
    LLMCall,
    Module,
    SlideVideoMapping,
    SourceMaterial,
)


class TestORMModels:
    """Verify ORM models are correctly defined."""

    def test_all_tables_registered(self) -> None:
        """All expected tables are in Base metadata."""
        table_names = set(Base.metadata.tables.keys())
        expected = {
            "courses",
            "source_materials",
            "slide_video_mappings",
            "modules",
            "lessons",
            "concepts",
            "exercises",
            "llm_calls",
        }
        assert expected.issubset(table_names)

    def test_course_table_columns(self) -> None:
        """Course table has expected columns."""
        columns = {c.name for c in Course.__table__.columns}
        assert "id" in columns
        assert "title" in columns
        assert "created_at" in columns
        assert "updated_at" in columns

    def test_source_material_fk(self) -> None:
        """SourceMaterial has FK to courses."""
        fks = {fk.target_fullname for fk in SourceMaterial.__table__.foreign_keys}
        assert "courses.id" in fks

    def test_cascade_chain(self) -> None:
        """Verify cascade chain: Course -> Module -> Lesson -> Concept/Exercise."""
        # Module -> Course
        assert any(
            fk.target_fullname == "courses.id" for fk in Module.__table__.foreign_keys
        )
        # Lesson -> Module
        assert any(
            fk.target_fullname == "modules.id" for fk in Lesson.__table__.foreign_keys
        )
        # Concept -> Lesson
        assert any(
            fk.target_fullname == "lessons.id" for fk in Concept.__table__.foreign_keys
        )
        # Exercise -> Lesson
        assert any(
            fk.target_fullname == "lessons.id" for fk in Exercise.__table__.foreign_keys
        )

    def test_concept_has_vector_column(self) -> None:
        """Concept has embedding column for future RAG."""
        columns = {c.name for c in Concept.__table__.columns}
        assert "embedding" in columns

    def test_llm_call_linked_to_tenant(self) -> None:
        """LLMCall has FK to tenants for billing."""
        fks = {fk.target_fullname for fk in LLMCall.__table__.foreign_keys}
        assert fks == {"tenants.id"}

    def test_slide_video_mapping_fk(self) -> None:
        """SlideVideoMapping has FK to courses."""
        fks = {fk.target_fullname for fk in SlideVideoMapping.__table__.foreign_keys}
        assert "courses.id" in fks

    def test_ondelete_cascade_on_foreign_keys(self) -> None:
        """All FK constraints use CASCADE ondelete."""
        models_with_fks = [
            SourceMaterial,
            SlideVideoMapping,
            Module,
            Lesson,
            Concept,
            Exercise,
        ]
        for model in models_with_fks:
            for fk in model.__table__.foreign_keys:
                assert fk.ondelete == "CASCADE", (
                    f"{model.__tablename__}.{fk.parent.name} missing CASCADE ondelete"
                )
