"""Tests for structure generation API endpoints (S2-053)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant
from course_supporter.conflict_detection import ConflictInfo
from course_supporter.errors import (
    GenerationConflictError,
    NodeNotFoundError,
    NoReadyMaterialsError,
)
from course_supporter.generation_orchestrator import GenerationPlan, MappingWarning
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import MappingValidationState

STUB_TENANT = MagicMock(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)

NODE_ID = uuid.uuid4()
SNAPSHOT_ID = uuid.uuid4()
NOW = datetime.now(UTC)

_REPO_PATH = "course_supporter.api.routes.generation.MaterialNodeRepository"
_SNAP_PATH = "course_supporter.api.routes.generation.SnapshotRepository"
_TRIGGER_PATH = "course_supporter.api.routes.generation.trigger_generation"
_FIND_ROOT_PATH = "course_supporter.api.routes.generation._find_root_id"


# -- Helpers --


def _make_job(
    *,
    job_id: uuid.UUID | None = None,
    job_type: str = "generate_structure",
    status: str = "queued",
    tenant_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
) -> MagicMock:
    job = MagicMock()
    job.id = job_id or uuid.uuid4()
    job.job_type = job_type
    job.priority = "normal"
    job.status = status
    job.tenant_id = tenant_id or STUB_TENANT.tenant_id
    job.node_id = node_id or NODE_ID
    job.arq_job_id = f"arq:{job.id}"
    job.error_message = None
    job.queued_at = NOW
    job.started_at = None
    job.completed_at = None
    job.estimated_at = None
    return job


def _make_snapshot(
    *,
    snapshot_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    structure: dict[str, object] | None = None,
) -> MagicMock:
    snap = MagicMock()
    snap.id = snapshot_id or SNAPSHOT_ID
    snap.node_id = node_id or NODE_ID
    snap.mode = "free"
    snap.node_fingerprint = "a" * 64
    snap.externalservicecall_id = uuid.uuid4()
    snap.service_call = MagicMock(
        id=snap.externalservicecall_id,
        provider="gemini",
        model_id="gemini-2.0-flash",
        prompt_ref="v1",
        unit_in=1000,
        unit_out=500,
        cost_usd=0.01,
    )
    snap.structure = structure or {"title": "Test Course", "modules": []}
    snap.created_at = NOW
    return snap


def _mock_node(
    *,
    node_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
) -> MagicMock:
    node = MagicMock()
    node.id = node_id or NODE_ID
    node.tenant_id = tenant_id or STUB_TENANT.tenant_id
    node.parent_id = parent_id
    return node


# -- Fixtures --


@pytest.fixture()
def mock_session() -> MagicMock:
    session = AsyncMock()
    session.commit = AsyncMock()
    return session


@pytest.fixture()
def mock_arq() -> MagicMock:
    return MagicMock()


@pytest.fixture()
async def client(mock_session: MagicMock, mock_arq: MagicMock) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    app.dependency_overrides[get_arq_redis] = lambda: mock_arq
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


# -- POST /nodes/{nid}/generate --


class TestGenerateStructure:
    """POST /nodes/{nid}/generate -- trigger generation."""

    async def test_202_new_generation(self, client: AsyncClient) -> None:
        """New generation returns 202 with generation job."""
        gen_job = _make_job()
        plan = GenerationPlan(generation_job=gen_job)

        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_FIND_ROOT_PATH, new_callable=AsyncMock, return_value=NODE_ID),
            patch(_TRIGGER_PATH, new_callable=AsyncMock, return_value=plan),
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/generate",
                json={"mode": "free"},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["is_idempotent"] is False
        assert data["generation_job"] is not None
        assert data["generation_job"]["id"] == str(gen_job.id)
        assert data["existing_snapshot_id"] is None

    async def test_200_idempotent(self, client: AsyncClient) -> None:
        """Idempotent hit returns 200 with existing snapshot ID."""
        plan = GenerationPlan(
            existing_snapshot_id=SNAPSHOT_ID,
            is_idempotent=True,
        )

        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_FIND_ROOT_PATH, new_callable=AsyncMock, return_value=NODE_ID),
            patch(_TRIGGER_PATH, new_callable=AsyncMock, return_value=plan),
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/generate",
                json={"mode": "free"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["is_idempotent"] is True
        assert data["existing_snapshot_id"] == str(SNAPSHOT_ID)
        assert data["generation_job"] is None

    async def test_202_cascade_with_ingestion(self, client: AsyncClient) -> None:
        """Cascade plan returns 202 with both ingestion and generation jobs."""
        ing_job = _make_job(job_type="ingest")
        gen_job = _make_job()
        plan = GenerationPlan(
            ingestion_jobs=[ing_job],
            generation_job=gen_job,
        )

        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_FIND_ROOT_PATH, new_callable=AsyncMock, return_value=NODE_ID),
            patch(_TRIGGER_PATH, new_callable=AsyncMock, return_value=plan),
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/generate",
                json={"mode": "guided"},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert len(data["ingestion_jobs"]) == 1
        assert data["generation_job"] is not None

    async def test_404_node_not_found(self, client: AsyncClient) -> None:
        """Non-existent node returns 404."""
        with patch(_REPO_PATH) as mock_repo_cls:
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=None)
            resp = await client.post(
                f"/api/v1/nodes/{uuid.uuid4()}/generate",
                json={"mode": "free"},
            )

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Node not found"

    async def test_404_node_not_found_from_trigger(self, client: AsyncClient) -> None:
        """NodeNotFoundError from trigger_generation returns 404."""
        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_FIND_ROOT_PATH, new_callable=AsyncMock, return_value=NODE_ID),
            patch(
                _TRIGGER_PATH,
                new_callable=AsyncMock,
                side_effect=NodeNotFoundError("Node not found"),
            ),
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/generate",
                json={"mode": "free"},
            )

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Node not found"

    async def test_409_conflict(self, client: AsyncClient) -> None:
        """Overlapping active generation returns 409."""
        conflict = ConflictInfo(
            job_id=uuid.uuid4(),
            job_node_id=None,
            reason="both target the entire course",
        )

        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_FIND_ROOT_PATH, new_callable=AsyncMock, return_value=NODE_ID),
            patch(
                _TRIGGER_PATH,
                new_callable=AsyncMock,
                side_effect=GenerationConflictError(conflict),
            ),
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/generate",
                json={"mode": "free"},
            )

        assert resp.status_code == 409
        assert "conflict" in resp.json()["detail"].lower()

    async def test_422_no_materials(self, client: AsyncClient) -> None:
        """Subtree with no materials returns 422."""
        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_FIND_ROOT_PATH, new_callable=AsyncMock, return_value=NODE_ID),
            patch(
                _TRIGGER_PATH,
                new_callable=AsyncMock,
                side_effect=NoReadyMaterialsError("No materials"),
            ),
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/generate",
                json={"mode": "free"},
            )

        assert resp.status_code == 422
        assert "materials" in resp.json()["detail"].lower()

    async def test_default_mode_is_free(self, client: AsyncClient) -> None:
        """Omitting mode defaults to 'free'."""
        gen_job = _make_job()
        plan = GenerationPlan(generation_job=gen_job)

        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_FIND_ROOT_PATH, new_callable=AsyncMock, return_value=NODE_ID),
            patch(
                _TRIGGER_PATH,
                new_callable=AsyncMock,
                return_value=plan,
            ) as mock_trigger,
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            await client.post(
                f"/api/v1/nodes/{NODE_ID}/generate",
                json={},
            )

        mock_trigger.assert_called_once()
        call_kwargs = mock_trigger.call_args.kwargs
        assert call_kwargs["mode"] == "free"

    async def test_202_with_mapping_warnings(self, client: AsyncClient) -> None:
        """Generation response includes mapping warnings."""
        gen_job = _make_job()
        warning_id = uuid.uuid4()
        warning_node = uuid.uuid4()
        plan = GenerationPlan(
            generation_job=gen_job,
            mapping_warnings=[
                MappingWarning(
                    mapping_id=warning_id,
                    node_id=warning_node,
                    slide_number=5,
                    validation_state=MappingValidationState.PENDING_VALIDATION,
                ),
            ],
        )

        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_FIND_ROOT_PATH, new_callable=AsyncMock, return_value=NODE_ID),
            patch(_TRIGGER_PATH, new_callable=AsyncMock, return_value=plan),
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/generate",
                json={"mode": "free"},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert len(data["mapping_warnings"]) == 1
        w = data["mapping_warnings"][0]
        assert w["mapping_id"] == str(warning_id)
        assert w["node_id"] == str(warning_node)
        assert w["slide_number"] == 5
        assert w["validation_state"] == "pending_validation"

    async def test_202_no_warnings_empty_list(self, client: AsyncClient) -> None:
        """No warnings -> empty list in response."""
        gen_job = _make_job()
        plan = GenerationPlan(generation_job=gen_job)

        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_FIND_ROOT_PATH, new_callable=AsyncMock, return_value=NODE_ID),
            patch(_TRIGGER_PATH, new_callable=AsyncMock, return_value=plan),
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/generate",
                json={"mode": "free"},
            )

        assert resp.status_code == 202
        assert resp.json()["mapping_warnings"] == []

    async def test_trigger_receives_root_and_target(self, client: AsyncClient) -> None:
        """trigger_generation is called with root_node_id and target_node_id."""
        root_id = uuid.uuid4()
        child_id = uuid.uuid4()
        gen_job = _make_job(node_id=child_id)
        plan = GenerationPlan(generation_job=gen_job)

        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_FIND_ROOT_PATH, new_callable=AsyncMock, return_value=root_id),
            patch(
                _TRIGGER_PATH,
                new_callable=AsyncMock,
                return_value=plan,
            ) as mock_trigger,
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(
                return_value=_mock_node(node_id=child_id)
            )
            await client.post(
                f"/api/v1/nodes/{child_id}/generate",
                json={"mode": "free"},
            )

        mock_trigger.assert_called_once()
        call_kwargs = mock_trigger.call_args.kwargs
        assert call_kwargs["root_node_id"] == root_id
        assert call_kwargs["target_node_id"] == child_id

    async def test_trigger_root_node_target_is_none(self, client: AsyncClient) -> None:
        """When node IS the root, target_node_id is None."""
        gen_job = _make_job()
        plan = GenerationPlan(generation_job=gen_job)

        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_FIND_ROOT_PATH, new_callable=AsyncMock, return_value=NODE_ID),
            patch(
                _TRIGGER_PATH,
                new_callable=AsyncMock,
                return_value=plan,
            ) as mock_trigger,
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            await client.post(
                f"/api/v1/nodes/{NODE_ID}/generate",
                json={"mode": "free"},
            )

        mock_trigger.assert_called_once()
        call_kwargs = mock_trigger.call_args.kwargs
        assert call_kwargs["root_node_id"] == NODE_ID
        assert call_kwargs["target_node_id"] is None


# -- GET /nodes/{nid}/structure --


class TestGetLatestStructure:
    """GET /nodes/{nid}/structure -- latest snapshot."""

    async def test_200(self, client: AsyncClient) -> None:
        """Returns latest snapshot for the node."""
        snap = _make_snapshot()

        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_SNAP_PATH) as mock_snap_cls,
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            mock_snap_cls.return_value.get_latest_for_node = AsyncMock(
                return_value=snap
            )
            resp = await client.get(
                f"/api/v1/nodes/{NODE_ID}/structure",
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(SNAPSHOT_ID)
        assert "structure" in data
        assert data["structure"]["title"] == "Test Course"

    async def test_404_no_snapshot(self, client: AsyncClient) -> None:
        """Returns 404 when no snapshot exists."""
        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_SNAP_PATH) as mock_snap_cls,
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            mock_snap_cls.return_value.get_latest_for_node = AsyncMock(
                return_value=None
            )
            resp = await client.get(
                f"/api/v1/nodes/{NODE_ID}/structure",
            )

        assert resp.status_code == 404

    async def test_404_node_not_found(self, client: AsyncClient) -> None:
        """Non-existent node returns 404."""
        with patch(_REPO_PATH) as mock_repo_cls:
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=None)
            resp = await client.get(
                f"/api/v1/nodes/{uuid.uuid4()}/structure",
            )

        assert resp.status_code == 404


# -- GET /nodes/{nid}/structure/history --


class TestListSnapshots:
    """GET /nodes/{nid}/structure/history -- snapshot list."""

    async def test_200_with_items(self, client: AsyncClient) -> None:
        """Returns paginated list of snapshot summaries."""
        snaps = [_make_snapshot(snapshot_id=uuid.uuid4()) for _ in range(3)]

        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_SNAP_PATH) as mock_snap_cls,
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            mock_snap_cls.return_value.count_for_node = AsyncMock(return_value=3)
            mock_snap_cls.return_value.list_for_node = AsyncMock(return_value=snaps)
            resp = await client.get(
                f"/api/v1/nodes/{NODE_ID}/structure/history",
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3
        assert data["limit"] == 20
        assert data["offset"] == 0
        # Summary should NOT contain 'structure'
        assert "structure" not in data["items"][0]

    async def test_200_empty(self, client: AsyncClient) -> None:
        """Returns empty list when no snapshots exist."""
        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_SNAP_PATH) as mock_snap_cls,
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            mock_snap_cls.return_value.count_for_node = AsyncMock(return_value=0)
            mock_snap_cls.return_value.list_for_node = AsyncMock(return_value=[])
            resp = await client.get(
                f"/api/v1/nodes/{NODE_ID}/structure/history",
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    async def test_pagination(self, client: AsyncClient) -> None:
        """Pagination with limit and offset works."""
        page = [_make_snapshot(snapshot_id=uuid.uuid4()) for _ in range(2)]

        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_SNAP_PATH) as mock_snap_cls,
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            mock_snap_cls.return_value.count_for_node = AsyncMock(return_value=5)
            mock_snap_cls.return_value.list_for_node = AsyncMock(return_value=page)
            resp = await client.get(
                f"/api/v1/nodes/{NODE_ID}/structure/history",
                params={"limit": 2, "offset": 1},
            )

        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2
        assert data["limit"] == 2
        assert data["offset"] == 1


# -- GET /nodes/{nid}/structure/snapshots/{snap_id} --


class TestGetSnapshot:
    """GET /nodes/{nid}/structure/snapshots/{snap_id} -- detail."""

    async def test_200_existing(self, client: AsyncClient) -> None:
        """Returns full snapshot with structure."""
        snap = _make_snapshot()

        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_SNAP_PATH) as mock_snap_cls,
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            mock_snap_cls.return_value.get_by_id = AsyncMock(return_value=snap)
            resp = await client.get(
                f"/api/v1/nodes/{NODE_ID}/structure/snapshots/{SNAPSHOT_ID}",
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(SNAPSHOT_ID)
        assert "structure" in data

    async def test_404_not_found(self, client: AsyncClient) -> None:
        """Non-existent snapshot returns 404."""
        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_SNAP_PATH) as mock_snap_cls,
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            mock_snap_cls.return_value.get_by_id = AsyncMock(return_value=None)
            resp = await client.get(
                f"/api/v1/nodes/{NODE_ID}/structure/snapshots/{uuid.uuid4()}",
            )

        assert resp.status_code == 404

    async def test_404_wrong_node(self, client: AsyncClient) -> None:
        """Snapshot belonging to another node returns 404."""
        other_node_id = uuid.uuid4()
        snap = _make_snapshot(node_id=other_node_id)

        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_SNAP_PATH) as mock_snap_cls,
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            mock_snap_cls.return_value.get_by_id = AsyncMock(return_value=snap)
            resp = await client.get(
                f"/api/v1/nodes/{NODE_ID}/structure/snapshots/{snap.id}",
            )

        assert resp.status_code == 404


# -- Tenant isolation --


class TestTenantIsolation:
    """Verify _require_node_for_tenant checks node ownership."""

    async def test_generate_checks_tenant(self, client: AsyncClient) -> None:
        """POST generate verifies node ownership via MaterialNodeRepository."""
        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_FIND_ROOT_PATH, new_callable=AsyncMock, return_value=NODE_ID),
            patch(
                _TRIGGER_PATH,
                new_callable=AsyncMock,
                return_value=GenerationPlan(generation_job=_make_job()),
            ),
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            await client.post(
                f"/api/v1/nodes/{NODE_ID}/generate",
                json={"mode": "free"},
            )

        # MaterialNodeRepository is instantiated with session (no tenant_id)
        mock_repo_cls.assert_called()
        # get_by_id is called with node_id
        mock_repo_cls.return_value.get_by_id.assert_called()

    async def test_get_structure_checks_tenant(self, client: AsyncClient) -> None:
        """GET structure verifies node ownership via MaterialNodeRepository."""
        with (
            patch(_REPO_PATH) as mock_repo_cls,
            patch(_SNAP_PATH) as mock_snap_cls,
        ):
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=_mock_node())
            mock_snap_cls.return_value.get_latest_for_node = AsyncMock(
                return_value=_make_snapshot()
            )
            await client.get(
                f"/api/v1/nodes/{NODE_ID}/structure",
            )

        mock_repo_cls.assert_called()
        mock_repo_cls.return_value.get_by_id.assert_called()

    async def test_wrong_tenant_returns_404(self, client: AsyncClient) -> None:
        """Node belonging to another tenant returns 404."""
        other_tenant_id = uuid.uuid4()
        wrong_node = _mock_node(tenant_id=other_tenant_id)

        with patch(_REPO_PATH) as mock_repo_cls:
            mock_repo_cls.return_value.get_by_id = AsyncMock(return_value=wrong_node)
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/generate",
                json={"mode": "free"},
            )

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Node not found"
