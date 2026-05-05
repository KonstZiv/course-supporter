"""Integration: Phase 1 hotfix-6 Pydantic ↔ legacy-ORM bridge.

Pins the validation_alias bridge across all materialization paths
exercised by Phase 2.x response endpoints:

  - SnapshotSummaryResponse / SnapshotDetailResponse:
    ORM-instance ``model_validate`` path used in routes
    ``GET /structure/history`` and ``GET /structure/snapshots/{id}``.
  - EditableNodeResponse:
    dict-input ``model_validate`` path via the helper
    ``_orm_to_response`` (editable.py:60-75) used in
    ``GET /editable``, ``PATCH /editable/{id}``,
    ``POST /editable/init``.

DB column names on these Phase 2.x tables (``materialnode_id``,
``node_fingerprint``) are intentionally retained at HEAD 06951c4 —
Phase 2.x DB rename is deferred.  Hotfix-6 bridges the schema layer
(``validation_alias`` + ``populate_by_name``) and the handler dict
construction (alias-aware ``getattr``) without touching DB or ORM.

Run with: ``uv run pytest -m requires_db --run-db``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.routes.editable import _orm_to_response
from course_supporter.api.schemas import (
    EditableNodeResponse,
    SnapshotDetailResponse,
    SnapshotSummaryResponse,
)
from course_supporter.storage.orm import (
    CourseNode,
    GenerationMode,
    StructureNodeEditable,
    StructureSnapshot,
    Tenant,
)

pytestmark = pytest.mark.requires_db


# ── Fixture helpers ──────────────────────────────────────────────


async def _make_tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(name=f"h6-test-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _make_root_node(session: AsyncSession, tenant_id: uuid.UUID) -> CourseNode:
    node = CourseNode(
        tenant_id=tenant_id,
        title="hotfix-6 probe course",
        order=0,
    )
    session.add(node)
    await session.flush()
    return node


async def _make_snapshot(
    session: AsyncSession,
    course_node_id: uuid.UUID,
    *,
    fingerprint: str = "a" * 64,
) -> StructureSnapshot:
    snap = StructureSnapshot(
        materialnode_id=course_node_id,
        externalservicecall_id=None,
        node_fingerprint=fingerprint,
        mode=GenerationMode.FREE,
        structure={"node_type": "course", "title": "hotfix-6 probe"},
    )
    session.add(snap)
    await session.flush()
    await session.refresh(snap)
    return snap


async def _make_editable(
    session: AsyncSession,
    course_node_id: uuid.UUID,
) -> StructureNodeEditable:
    editable = StructureNodeEditable(
        materialnode_id=course_node_id,
        source_snapshot_id=None,
        source_structurenode_id=None,
        parent_editable_id=None,
        node_type="module",
        order=0,
        title="hotfix-6 editable probe",
        edited_fields=[],
    )
    session.add(editable)
    await session.flush()
    await session.refresh(editable)
    return editable


# ── Snapshot* — ORM-instance materialization ─────────────────────


class TestSnapshotSchemaAliasBridge:
    """SnapshotSummary/Detail materialize from legacy-column ORM rows."""

    async def test_summary_materializes_from_orm(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_tenant(db_session)
        node = await _make_root_node(db_session, tenant.id)
        snap = await _make_snapshot(db_session, node.id)

        resp = SnapshotSummaryResponse.model_validate(snap)

        assert resp.course_node_id == snap.materialnode_id
        assert resp.content_hash == snap.node_fingerprint

        dumped = resp.model_dump(mode="json")
        assert "course_node_id" in dumped
        assert "content_hash" in dumped
        assert "materialnode_id" not in dumped
        assert "node_fingerprint" not in dumped

    async def test_detail_inherits_aliases_from_summary(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_tenant(db_session)
        node = await _make_root_node(db_session, tenant.id)
        snap = await _make_snapshot(db_session, node.id, fingerprint="c" * 64)

        resp = SnapshotDetailResponse.model_validate(snap)

        assert resp.course_node_id == snap.materialnode_id
        assert resp.content_hash == snap.node_fingerprint
        assert resp.structure == snap.structure


# ── EditableNodeResponse — dict-input via _orm_to_response ───────


class TestEditableSchemaAliasBridge:
    """_orm_to_response (alias-aware getattr) bridges legacy ORM."""

    async def test_orm_to_response_yields_course_node_id(
        self, db_session: AsyncSession
    ) -> None:
        tenant = await _make_tenant(db_session)
        node = await _make_root_node(db_session, tenant.id)
        editable = await _make_editable(db_session, node.id)

        resp = _orm_to_response(editable)

        assert resp.course_node_id == editable.materialnode_id

        dumped = resp.model_dump(mode="json")
        assert "course_node_id" in dumped
        assert "materialnode_id" not in dumped

    async def test_dict_input_with_field_name_key_via_populate_by_name(
        self,
        db_session: AsyncSession,
    ) -> None:
        """populate_by_name=True accepts field-name dict keys."""
        tenant = await _make_tenant(db_session)
        node = await _make_root_node(db_session, tenant.id)
        editable = await _make_editable(db_session, node.id)

        # Mirror _orm_to_response output: dict keyed by Pydantic field name
        # (course_node_id), value sourced via alias-aware getattr.
        data = {}
        for field_name, field_info in EditableNodeResponse.model_fields.items():
            if field_name == "children":
                continue
            alias = field_info.validation_alias
            src_attr = alias if isinstance(alias, str) else field_name
            data[field_name] = getattr(editable, src_attr)

        resp = EditableNodeResponse.model_validate({**data, "children": []})

        assert resp.course_node_id == editable.materialnode_id
