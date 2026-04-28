"""Tests for tenant_id on existing tables.

ExternalServiceCall coverage moved to integration suite — task 0.4
drops ``ExternalServiceCall.tenant_id`` per KD5 (tenant attribution
flows through ``Job`` JOIN). Schema-shape tests for the KD5 ESC live
in ``tests/integration/test_external_service_call_db.py`` (added in
0.4 commit (g)) since they assert against ``pg_constraint`` /
``pg_indexes`` rather than the ORM mapping alone.
"""

from __future__ import annotations

from course_supporter.storage.orm import MaterialNode, _uuid7


class TestMaterialNodeTenant:
    """Tests for tenant_id on MaterialNode model."""

    def test_node_has_tenant_id_column(self) -> None:
        """MaterialNode table has tenant_id FK column."""
        table = MaterialNode.__table__
        col = table.c.tenant_id
        assert col is not None
        assert col.nullable is False

    def test_node_with_tenant(self) -> None:
        """MaterialNode accepts tenant_id at construction."""
        tid = _uuid7()
        node = MaterialNode(tenant_id=tid, title="Python 101")
        assert node.tenant_id == tid
        assert node.title == "Python 101"

    def test_node_tenant_fk_cascade(self) -> None:
        """MaterialNode.tenant_id FK has CASCADE ondelete."""
        table = MaterialNode.__table__
        fks = [fk for fk in table.foreign_keys if fk.column.table.name == "tenants"]
        assert len(fks) == 1
        assert fks[0].ondelete == "CASCADE"

    def test_node_tenant_id_indexed(self) -> None:
        """MaterialNode.tenant_id has an index."""
        table = MaterialNode.__table__
        col = table.c.tenant_id
        assert col.index is True
