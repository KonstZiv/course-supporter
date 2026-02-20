"""Tests for MaterialNode ORM model."""

from __future__ import annotations

import uuid

from course_supporter.storage.orm import Course, MaterialNode, _uuid7


class TestMaterialNodeModel:
    """MaterialNode ORM column/default tests."""

    def test_create_root_node(self) -> None:
        """Root node has parent_id=None."""
        node = MaterialNode(
            course_id=_uuid7(),
            title="Module 1",
        )

        assert node.title == "Module 1"
        assert node.parent_id is None
        assert node.description is None
        assert node.node_fingerprint is None

    def test_create_child_node(self) -> None:
        """Child node has explicit parent_id."""
        parent_id = _uuid7()
        node = MaterialNode(
            course_id=_uuid7(),
            parent_id=parent_id,
            title="Subtopic A",
            description="Details about subtopic A",
        )

        assert node.parent_id == parent_id
        assert node.description == "Details about subtopic A"

    def test_order_default(self) -> None:
        """Order column defaults to 0."""
        col = MaterialNode.__table__.c.order
        assert col.default.arg == 0

    def test_pk_uses_uuid7(self) -> None:
        """PK default is UUIDv7 factory."""
        pk = _uuid7()
        assert isinstance(pk, uuid.UUID)
        assert pk.version == 7

    def test_node_fingerprint_nullable(self) -> None:
        """node_fingerprint is nullable (lazy cached)."""
        col = MaterialNode.__table__.c.node_fingerprint
        assert col.nullable is True

    def test_title_max_length(self) -> None:
        """Title column accepts up to 500 chars."""
        col = MaterialNode.__table__.c.title
        assert col.type.length == 500  # type: ignore[union-attr]


class TestMaterialNodeRelationships:
    """MaterialNode relationship configuration tests."""

    def test_self_referential_fk(self) -> None:
        """parent_id FK points to material_nodes.id."""
        col = MaterialNode.__table__.c.parent_id
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "material_nodes.id"

    def test_course_fk(self) -> None:
        """course_id FK points to courses.id."""
        col = MaterialNode.__table__.c.course_id
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "courses.id"

    def test_cascade_delete_on_parent(self) -> None:
        """Parent FK uses CASCADE ondelete."""
        col = MaterialNode.__table__.c.parent_id
        fk = next(iter(col.foreign_keys))
        assert fk.ondelete == "CASCADE"

    def test_cascade_delete_on_course(self) -> None:
        """Course FK uses CASCADE ondelete."""
        col = MaterialNode.__table__.c.course_id
        fk = next(iter(col.foreign_keys))
        assert fk.ondelete == "CASCADE"

    def test_children_relationship_cascade(self) -> None:
        """children relationship has cascade delete-orphan."""
        rel = MaterialNode.__mapper__.relationships["children"]
        assert "delete-orphan" in rel.cascade

    def test_parent_relationship_exists(self) -> None:
        """parent relationship is configured."""
        rel = MaterialNode.__mapper__.relationships["parent"]
        assert rel.back_populates == "children"

    def test_course_relationship_back_populates(self) -> None:
        """course relationship back_populates material_nodes on Course."""
        rel = MaterialNode.__mapper__.relationships["course"]
        assert rel.back_populates == "material_nodes"


class TestCourseToMaterialNodeRelationship:
    """Course -> MaterialNode relationship tests."""

    def test_course_has_material_nodes_relationship(self) -> None:
        """Course model has material_nodes relationship."""
        rel = Course.__mapper__.relationships["material_nodes"]
        assert rel.back_populates == "course"

    def test_course_material_nodes_cascade(self) -> None:
        """Course -> material_nodes cascade includes delete-orphan."""
        rel = Course.__mapper__.relationships["material_nodes"]
        assert "delete-orphan" in rel.cascade


class TestMaterialNodeIndexes:
    """MaterialNode index/constraint tests."""

    def test_course_id_indexed(self) -> None:
        """course_id column is indexed."""
        col = MaterialNode.__table__.c.course_id
        assert col.index is True

    def test_parent_id_indexed(self) -> None:
        """parent_id column is indexed."""
        col = MaterialNode.__table__.c.parent_id
        assert col.index is True
