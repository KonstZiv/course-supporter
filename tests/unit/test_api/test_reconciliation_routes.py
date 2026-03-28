"""Tests for reconciliation API endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant
from course_supporter.storage.database import get_session

STUB_TENANT = MagicMock(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep"],
    rate_limit_prep=100,
    rate_limit_check=1000,
    key_prefix="cs_test",
)

NODE_ID = uuid.uuid4()
NOW = datetime.now(UTC)

_REQUIRE_NODE = "course_supporter.api.routes.reconciliation._require_node_for_tenant"
_EDITABLE_REPO = "course_supporter.api.routes.reconciliation.EditableRepository"
_ENQUEUE = "course_supporter.api.routes.reconciliation.enqueue_reconcile_preview"
_JOB_REPO = "course_supporter.api.routes.reconciliation.JobRepository"
_COMPUTE_FP = "course_supporter.api.routes.reconciliation._compute_current_fingerprints"
_PREVIEW_REPO = (
    "course_supporter.api.routes.reconciliation.ReconciliationPreviewRepository"
)
_MAKE_FP = "course_supporter.api.routes.reconciliation._make_combined_fingerprint"


def _make_job(
    *,
    job_id: uuid.UUID | None = None,
    job_type: str = "reconcile_preview",
    status: str = "queued",
) -> MagicMock:
    job = MagicMock()
    job.id = job_id or uuid.uuid4()
    job.job_type = job_type
    job.priority = "normal"
    job.status = status
    job.tenant_id = STUB_TENANT.tenant_id
    job.materialnode_id = NODE_ID
    job.arq_job_id = f"arq:{job.id}"
    job.result_data = None
    job.error_message = None
    job.queued_at = NOW
    job.started_at = None
    job.completed_at = None
    job.estimated_at = None
    return job


def _make_editable_node(
    *,
    node_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
) -> MagicMock:
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.materialnode_id = NODE_ID
    node.source_snapshot_id = uuid.uuid4()
    node.source_structurenode_id = uuid.uuid4()
    node.parent_editable_id = parent_id
    node.node_type = "module"
    node.order = 0
    node.title = "Test Module"
    node.description = "Test description"
    node.learning_goal = None
    node.expected_knowledge = None
    node.expected_skills = None
    node.prerequisites = None
    node.difficulty = None
    node.estimated_duration = None
    node.success_criteria = None
    node.assessment_method = None
    node.competencies = None
    node.key_concepts = None
    node.common_mistakes = None
    node.teaching_strategy = None
    node.activities = None
    node.teaching_style = None
    node.deep_dive_references = None
    node.timecodes = None
    node.slide_references = None
    node.web_references = None
    node.edited_fields = []
    node.created_at = NOW
    node.updated_at = NOW
    return node


# -- Fixtures --


@pytest.fixture()
def mock_session() -> MagicMock:
    session = AsyncMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()
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


# -- POST /nodes/{nid}/reconcile/preview --


class TestReconcilePreview:
    """POST /nodes/{nid}/reconcile/preview — enqueue async preview."""

    async def test_202_enqueue_success(self, client: AsyncClient) -> None:
        """Returns 202 with job response when editable tree exists."""
        job = _make_job()
        editable = _make_editable_node()

        with (
            patch(_REQUIRE_NODE, new_callable=AsyncMock),
            patch(_EDITABLE_REPO) as mock_repo_cls,
            patch(_ENQUEUE, new_callable=AsyncMock, return_value=job),
            patch(_COMPUTE_FP, new_callable=AsyncMock, return_value=(None, None)),
        ):
            mock_repo_cls.return_value.get_tree = AsyncMock(return_value=[editable])
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/reconcile/preview",
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["id"] == str(job.id)
        assert data["job_type"] == "reconcile_preview"
        assert data["status"] == "queued"

    async def test_200_idempotent_cached(self, client: AsyncClient) -> None:
        """Returns 202 but skips enqueue when matching preview exists."""
        existing_job = _make_job(status="complete")
        editable = _make_editable_node()
        cached_preview = MagicMock()
        cached_preview.job_id = existing_job.id

        with (
            patch(_REQUIRE_NODE, new_callable=AsyncMock),
            patch(_EDITABLE_REPO) as mock_repo_cls,
            patch(
                _COMPUTE_FP,
                new_callable=AsyncMock,
                return_value=("abc123", "def456"),
            ),
            patch(_MAKE_FP, return_value="combined_hash"),
            patch(_PREVIEW_REPO) as mock_preview_cls,
            patch(_JOB_REPO) as mock_job_cls,
        ):
            mock_repo_cls.return_value.get_tree = AsyncMock(return_value=[editable])
            mock_preview_cls.return_value.find_by_fingerprint = AsyncMock(
                return_value=cached_preview,
            )
            mock_job_cls.return_value.get_by_id = AsyncMock(
                return_value=existing_job,
            )
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/reconcile/preview",
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["id"] == str(existing_job.id)
        assert data["status"] == "complete"

    async def test_404_no_editable_tree(self, client: AsyncClient) -> None:
        """Returns 404 when no editable tree exists."""
        with (
            patch(_REQUIRE_NODE, new_callable=AsyncMock),
            patch(_EDITABLE_REPO) as mock_repo_cls,
        ):
            mock_repo_cls.return_value.get_tree = AsyncMock(return_value=[])
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/reconcile/preview",
            )

        assert resp.status_code == 404
        assert "editable tree" in resp.json()["detail"].lower()

    async def test_404_node_not_found(self, client: AsyncClient) -> None:
        """Returns 404 when node doesn't belong to tenant."""
        from fastapi import HTTPException

        with patch(
            _REQUIRE_NODE,
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=404, detail="Node not found"),
        ):
            resp = await client.post(
                f"/api/v1/nodes/{uuid.uuid4()}/reconcile/preview",
            )

        assert resp.status_code == 404


# -- GET /nodes/{nid}/reconcile/preview/result --


class TestReconcilePreviewResult:
    """GET /nodes/{nid}/reconcile/preview/result — fetch completed result."""

    async def test_200_completed_job(self, client: AsyncClient) -> None:
        """Returns preview result when job is complete."""
        job_id = uuid.uuid4()
        issue_id = uuid.uuid4()
        editable_id = uuid.uuid4()

        job = _make_job(job_id=job_id, status="complete")
        job.result_data = {
            "issues": [
                {
                    "id": str(issue_id),
                    "editable_node_id": str(editable_id),
                    "node_title": "Test Node",
                    "field": "difficulty",
                    "issue_type": "gap",
                    "description": "Missing difficulty",
                    "current_value": None,
                    "suggested_value": "intermediate",
                    "reasoning": "Should have difficulty set",
                }
            ],
            "context_summary": "Found 1 issue: 1 gap.",
        }

        with (
            patch(_REQUIRE_NODE, new_callable=AsyncMock),
            patch(_JOB_REPO) as mock_repo_cls,
        ):
            mock_repo_cls.return_value.get_by_id_for_tenant = AsyncMock(
                return_value=job
            )
            resp = await client.get(
                f"/api/v1/nodes/{NODE_ID}/reconcile/preview/result",
                params={"job_id": str(job_id)},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["issues"]) == 1
        assert data["issues"][0]["field"] == "difficulty"
        assert data["context_summary"] == "Found 1 issue: 1 gap."

    async def test_404_job_not_found(self, client: AsyncClient) -> None:
        """Returns 404 when job doesn't exist."""
        with (
            patch(_REQUIRE_NODE, new_callable=AsyncMock),
            patch(_JOB_REPO) as mock_repo_cls,
        ):
            mock_repo_cls.return_value.get_by_id_for_tenant = AsyncMock(
                return_value=None
            )
            resp = await client.get(
                f"/api/v1/nodes/{NODE_ID}/reconcile/preview/result",
                params={"job_id": str(uuid.uuid4())},
            )

        assert resp.status_code == 404

    async def test_409_job_not_complete(self, client: AsyncClient) -> None:
        """Returns 409 when job is still active."""
        job = _make_job(status="active")

        with (
            patch(_REQUIRE_NODE, new_callable=AsyncMock),
            patch(_JOB_REPO) as mock_repo_cls,
        ):
            mock_repo_cls.return_value.get_by_id_for_tenant = AsyncMock(
                return_value=job
            )
            resp = await client.get(
                f"/api/v1/nodes/{NODE_ID}/reconcile/preview/result",
                params={"job_id": str(job.id)},
            )

        assert resp.status_code == 409
        assert "not yet complete" in resp.json()["detail"]

    async def test_400_wrong_job_type(self, client: AsyncClient) -> None:
        """Returns 400 when job is not a reconcile_preview."""
        job = _make_job(job_type="generate_structure", status="complete")

        with (
            patch(_REQUIRE_NODE, new_callable=AsyncMock),
            patch(_JOB_REPO) as mock_repo_cls,
        ):
            mock_repo_cls.return_value.get_by_id_for_tenant = AsyncMock(
                return_value=job
            )
            resp = await client.get(
                f"/api/v1/nodes/{NODE_ID}/reconcile/preview/result",
                params={"job_id": str(job.id)},
            )

        assert resp.status_code == 400

    async def test_500_missing_result_data(self, client: AsyncClient) -> None:
        """Returns 500 when job is complete but result_data is None."""
        job = _make_job(status="complete")
        job.result_data = None

        with (
            patch(_REQUIRE_NODE, new_callable=AsyncMock),
            patch(_JOB_REPO) as mock_repo_cls,
        ):
            mock_repo_cls.return_value.get_by_id_for_tenant = AsyncMock(
                return_value=job
            )
            resp = await client.get(
                f"/api/v1/nodes/{NODE_ID}/reconcile/preview/result",
                params={"job_id": str(job.id)},
            )

        assert resp.status_code == 500


# -- POST /nodes/{nid}/reconcile/apply --


class TestReconcileApply:
    """POST /nodes/{nid}/reconcile/apply — apply accepted issues."""

    async def test_200_apply_success(self, client: AsyncClient) -> None:
        """Applies accepted issues and returns updated tree."""
        editable_id = uuid.uuid4()
        issue_id = uuid.uuid4()
        editable = _make_editable_node(node_id=editable_id)

        with (
            patch(_REQUIRE_NODE, new_callable=AsyncMock),
            patch(_EDITABLE_REPO) as mock_repo_cls,
        ):
            mock_repo_cls.return_value.get_tree = AsyncMock(return_value=[editable])
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/reconcile/apply",
                json={
                    "accepted_issue_ids": [str(issue_id)],
                    "issues": [
                        {
                            "id": str(issue_id),
                            "editable_node_id": str(editable_id),
                            "node_title": "Test Module",
                            "field": "difficulty",
                            "issue_type": "gap",
                            "description": "Missing difficulty",
                            "current_value": None,
                            "suggested_value": "intermediate",
                            "reasoning": "Should have difficulty set",
                        }
                    ],
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["materialnode_id"] == str(NODE_ID)

    async def test_422_no_accepted_issues(self, client: AsyncClient) -> None:
        """Returns 422 when no issues match accepted_issue_ids."""
        with patch(_REQUIRE_NODE, new_callable=AsyncMock):
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/reconcile/apply",
                json={
                    "accepted_issue_ids": [str(uuid.uuid4())],
                    "issues": [
                        {
                            "id": str(uuid.uuid4()),
                            "editable_node_id": str(uuid.uuid4()),
                            "node_title": "Test",
                            "field": "difficulty",
                            "issue_type": "gap",
                            "description": "Test",
                            "reasoning": "Test",
                        }
                    ],
                },
            )

        assert resp.status_code == 422

    async def test_apply_skips_disallowed_fields(self, client: AsyncClient) -> None:
        """Issues targeting non-content fields are silently skipped."""
        editable_id = uuid.uuid4()
        issue_id = uuid.uuid4()
        editable = _make_editable_node(node_id=editable_id)

        with (
            patch(_REQUIRE_NODE, new_callable=AsyncMock),
            patch(_EDITABLE_REPO) as mock_repo_cls,
        ):
            mock_repo_cls.return_value.get_tree = AsyncMock(return_value=[editable])
            resp = await client.post(
                f"/api/v1/nodes/{NODE_ID}/reconcile/apply",
                json={
                    "accepted_issue_ids": [str(issue_id)],
                    "issues": [
                        {
                            "id": str(issue_id),
                            "editable_node_id": str(editable_id),
                            "node_title": "Test",
                            "field": "nonexistent_field",
                            "issue_type": "gap",
                            "description": "Test",
                            "reasoning": "Test",
                        }
                    ],
                },
            )

        # Should still return 200 (field is silently skipped)
        assert resp.status_code == 200
