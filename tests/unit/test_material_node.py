"""Tests for CourseNode ORM model."""

from __future__ import annotations

import uuid

from course_supporter.storage.orm import CourseNode, _uuid7


class TestCourseNodeModel:
    """CourseNode ORM column/default tests."""

    def test_create_root_node(self) -> None:
        """Root node has parent_id=None."""
        node = CourseNode(
            tenant_id=_uuid7(),
            title="Module 1",
        )

        assert node.title == "Module 1"
        assert node.parent_id is None
        assert node.description is None
        assert node.content_hash is None

    def test_create_child_node(self) -> None:
        """Child node has explicit parent_id."""
        parent_id = _uuid7()
        node = CourseNode(
            tenant_id=_uuid7(),
            parent_id=parent_id,
            title="Subtopic A",
            description="Details about subtopic A",
        )

        assert node.parent_id == parent_id
        assert node.description == "Details about subtopic A"

    def test_order_nullable(self) -> None:
        """Order column is nullable; NULL = no preferred sort position."""
        col = CourseNode.__table__.c.order
        assert col.nullable is True
        assert col.default is None
        assert col.server_default is None

    def test_pk_uses_uuid7(self) -> None:
        """PK default is UUIDv7 factory."""
        pk = _uuid7()
        assert isinstance(pk, uuid.UUID)
        assert pk.version == 7

    def test_content_hash_not_null_with_empty_default(self) -> None:
        """``content_hash`` is NOT NULL + ``server_default`` = empty-hash.

        Phase 3.1 commit 4 closed the KD9 NULL-at-INSERT regression
        (vision §3 KD9 "Спостереження Phase 1"). The column carries a
        defined empty-hash at INSERT — an empty CourseNode never has
        ``content_hash IS NULL`` at any point in its lifecycle.
        """
        col = CourseNode.__table__.c.content_hash
        assert col.nullable is False
        assert col.server_default is not None

    def test_title_max_length(self) -> None:
        """Title column accepts up to 500 chars."""
        col = CourseNode.__table__.c.title
        assert col.type.length == 500  # type: ignore[union-attr]


class TestCourseNodeRelationships:
    """CourseNode relationship configuration tests."""

    def test_self_referential_fk(self) -> None:
        """parent_id FK points to course_nodes.id."""
        col = CourseNode.__table__.c.parent_id
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "course_nodes.id"

    def test_tenant_fk(self) -> None:
        """tenant_id FK points to tenants.id."""
        col = CourseNode.__table__.c.tenant_id
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].target_fullname == "tenants.id"

    def test_cascade_delete_on_parent(self) -> None:
        """Parent FK uses CASCADE ondelete."""
        col = CourseNode.__table__.c.parent_id
        fk = next(iter(col.foreign_keys))
        assert fk.ondelete == "CASCADE"

    def test_cascade_delete_on_tenant(self) -> None:
        """Tenant FK uses CASCADE ondelete."""
        col = CourseNode.__table__.c.tenant_id
        fk = next(iter(col.foreign_keys))
        assert fk.ondelete == "CASCADE"

    def test_parent_relationship_exists(self) -> None:
        """parent relationship is configured."""
        rel = CourseNode.__mapper__.relationships["parent"]
        assert rel.back_populates == "children"


class TestCourseNodeIndexes:
    """CourseNode index/constraint tests."""

    def test_tenant_id_indexed(self) -> None:
        """tenant_id column is indexed."""
        col = CourseNode.__table__.c.tenant_id
        assert col.index is True

    def test_parent_id_indexed(self) -> None:
        """parent_id column is indexed."""
        col = CourseNode.__table__.c.parent_id
        assert col.index is True
