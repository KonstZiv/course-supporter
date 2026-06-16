"""Tests for the methodist node-summary HTTP surface (Phase 3.2.4 c2).

Covers all six endpoints with FastAPI dependency overrides + mocked
repository + ARQ Redis stub. Pins:

* Tenant-gate: foreign tenant + nonexistent node both 404
  ``Node not found`` (byte-identical) on every endpoint.
* P6 GET: missing Raw → 404 ``not_yet_generated``; present → 200.
* PATCH editable: editable family accepted; ``main_concepts`` etc.
  rejected with 422 at Pydantic boundary.
* POST /generate: enqueue_node_summary_regeneration invoked + 202 +
  JobResponse returned; ``orch.run()`` NEVER called synchronously.
* approve / accept-raw: repository methods invoked + 200.
* edit-view: combined payload shape.

Run with: ``uv run pytest tests/unit/test_api/test_node_summaries.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_arq_redis,
    get_current_tenant,
    get_stage_router,
)
from course_supporter.auth.context import TenantContext
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.database import get_session
from course_supporter.storage.node_summary_final_repository import (
    NodeSummaryFinalRepository,
)
from course_supporter.storage.node_summary_run_state import NodeSummaryRunScope

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep", "check"],
    plan_id="basic",
    key_prefix="cs_test",
)


def _mock_node(
    *,
    node_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> MagicMock:
    node = MagicMock()
    node.id = node_id or uuid.uuid4()
    node.tenant_id = tenant_id or STUB_TENANT.tenant_id
    return node


def _mock_final(course_node_id: uuid.UUID) -> MagicMock:
    """ORM-shaped NodeSummaryFinal MagicMock for response validation."""
    final = MagicMock()
    final.id = uuid.uuid4()
    final.course_node_id = course_node_id
    final.title = "test title"
    final.description = "test desc"
    final.learning_objectives = ["obj-1"]
    final.knowledge = []
    final.skills = []
    final.success_criteria = []
    final.assessment_approach = None
    final.teaching_approach = None
    final.key_activities = []
    final.common_mistakes = []
    final.main_concepts = ["c1"]
    final.secondary_concepts = []
    final.enclosing_context = None
    final.is_manual = False
    final.manual_description = None
    final.own_documents_count = 0
    final.own_chars_count = 0
    final.cumulative_documents_count = 0
    final.cumulative_chars_count = 0
    final.content_hash = "a" * 64
    final.approved_at = None
    final.enclosing_context_updated_at = None
    final.created_at = datetime.now(UTC)
    final.updated_at = datetime.now(UTC)
    return final


def _mock_job(
    *,
    tenant_id: uuid.UUID | None = None,
    course_node_id: uuid.UUID | None = None,
) -> MagicMock:
    job = MagicMock()
    job.id = uuid.uuid4()
    job.job_type = "node_summary_regeneration"
    job.priority = "normal"
    job.status = "queued"
    job.tenant_id = tenant_id or STUB_TENANT.tenant_id
    job.course_node_id = course_node_id or uuid.uuid4()
    job.arq_job_id = "arq-test-id"
    job.current_stage = None
    job.stage_progress = None
    job.result_data = None
    job.error_message = None
    job.queued_at = datetime.now(UTC)
    job.started_at = None
    job.completed_at = None
    return job


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
def mock_arq() -> AsyncMock:
    arq = AsyncMock()
    arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq-test-id"))
    return arq


@pytest.fixture()
def mock_stage_router() -> MagicMock:
    """Stub StageRouter for routes that need the dep resolved.

    The route's read-only ``validate_scope`` instantiates the
    orchestrator with this object but never invokes any LLM hook —
    ``MagicMock`` is sufficient. Tests that exercise the scope gate
    additionally ``patch`` ``build_node_summary_orchestrator`` to
    control the returned scope shape.
    """
    return MagicMock()


@pytest.fixture()
async def client(
    mock_session: AsyncMock,
    mock_arq: AsyncMock,
    mock_stage_router: MagicMock,
) -> AsyncClient:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    app.dependency_overrides[get_arq_redis] = lambda: mock_arq
    app.dependency_overrides[get_stage_router] = lambda: mock_stage_router
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac  # type: ignore[misc]
    app.dependency_overrides.clear()


def _orch_with_scope(
    *,
    uncovered: list[uuid.UUID] | None = None,
    in_scope: list[uuid.UUID] | None = None,
) -> MagicMock:
    """Patch target for ``build_node_summary_orchestrator``.

    Returns a MagicMock orch whose ``validate_scope`` is an AsyncMock
    returning the requested scope. ``run`` is also an AsyncMock so
    tests can assert it is NEVER awaited (inv #4 sharper form).
    """
    orch = MagicMock()
    orch.validate_scope = AsyncMock(
        return_value=NodeSummaryRunScope(
            in_scope_node_ids=in_scope or [],
            uncovered_stale_node_ids=uncovered or [],
        )
    )
    orch.run = AsyncMock()
    return orch


# ── Tenant-gate — byte-identical 404 for foreign + nonexistent ──


class TestTenantGateGeneric404:
    """All 6 endpoints render foreign-tenant + nonexistent into the
    SAME ``Node not found`` 404 (rule #12, inv #3)."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/api/v1/nodes/{nid}/summary/generate"),
            ("GET", "/api/v1/nodes/{nid}/summary"),
            ("PATCH", "/api/v1/node-summaries/{nid}/final"),
            ("POST", "/api/v1/node-summaries/{nid}/final/approve"),
            ("POST", "/api/v1/node-summaries/{nid}/final/accept-raw"),
            ("GET", "/api/v1/node-summaries/{nid}/edit-view"),
        ],
    )
    async def test_nonexistent_node_returns_generic_404(
        self, client: AsyncClient, method: str, path: str
    ) -> None:
        nid = uuid.uuid4()
        url = path.format(nid=nid)
        with patch.object(CourseNodeRepository, "get_by_id", return_value=None):
            if method == "GET":
                resp = await client.get(url)
            elif method == "POST":
                resp = await client.post(url, json={})
            else:
                resp = await client.patch(url, json={})
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Node not found"}

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/api/v1/nodes/{nid}/summary/generate"),
            ("GET", "/api/v1/nodes/{nid}/summary"),
            ("PATCH", "/api/v1/node-summaries/{nid}/final"),
            ("POST", "/api/v1/node-summaries/{nid}/final/approve"),
            ("POST", "/api/v1/node-summaries/{nid}/final/accept-raw"),
            ("GET", "/api/v1/node-summaries/{nid}/edit-view"),
        ],
    )
    async def test_foreign_tenant_returns_generic_404(
        self, client: AsyncClient, method: str, path: str
    ) -> None:
        """Foreign-tenant node returns the SAME body as nonexistent."""
        nid = uuid.uuid4()
        url = path.format(nid=nid)
        foreign_node = _mock_node(tenant_id=uuid.uuid4())  # not STUB_TENANT
        with patch.object(CourseNodeRepository, "get_by_id", return_value=foreign_node):
            if method == "GET":
                resp = await client.get(url)
            elif method == "POST":
                resp = await client.post(url, json={})
            else:
                resp = await client.patch(url, json={})
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Node not found"}


# ── POST /summary/generate — ARQ enqueue, NO synchronous orch.run ──


class TestGenerateEnqueuesJob:
    async def test_returns_202_with_job_response_when_scope_clean(
        self, client: AsyncClient, mock_session: AsyncMock
    ) -> None:
        """Clean scope (no uncovered_stale) + force=false → 202 + Job."""
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        job = _mock_job(course_node_id=nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch(
                "course_supporter.api.routes.node_summaries"
                ".build_node_summary_orchestrator",
                return_value=_orch_with_scope(uncovered=[], in_scope=[nid]),
            ),
            patch(
                "course_supporter.api.routes.node_summaries"
                ".enqueue_node_summary_regeneration",
                return_value=job,
            ) as mock_enqueue,
        ):
            resp = await client.post(
                f"/api/v1/nodes/{nid}/summary/generate",
                json={"force": False},
            )
        assert resp.status_code == 202
        body = resp.json()
        assert body["id"] == str(job.id)
        assert body["job_type"] == "node_summary_regeneration"
        mock_enqueue.assert_called_once()
        call_kwargs = mock_enqueue.call_args.kwargs
        assert call_kwargs["tenant_id"] == STUB_TENANT.tenant_id
        assert call_kwargs["vertex_node_id"] == nid
        assert call_kwargs["force"] is False
        # Task 3.2.6 Finding 1: commit ownership moved into
        # ``enqueue_node_summary_regeneration`` (mocked here), so the route no
        # longer commits. The helper's commit-before-dispatch ordering is
        # covered in tests/unit/test_enqueue.py.

    async def test_force_true_propagated_to_enqueue(self, client: AsyncClient) -> None:
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        job = _mock_job(course_node_id=nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch(
                "course_supporter.api.routes.node_summaries"
                ".build_node_summary_orchestrator",
                return_value=_orch_with_scope(uncovered=[], in_scope=[nid]),
            ),
            patch(
                "course_supporter.api.routes.node_summaries"
                ".enqueue_node_summary_regeneration",
                return_value=job,
            ) as mock_enqueue,
        ):
            resp = await client.post(
                f"/api/v1/nodes/{nid}/summary/generate",
                json={"force": True},
            )
        assert resp.status_code == 202
        assert mock_enqueue.call_args.kwargs["force"] is True

    async def test_uncovered_stale_without_force_returns_422(
        self, client: AsyncClient
    ) -> None:
        """K1 ratify: uncovered_stale_node_ids non-empty + force=false → 422.

        Body shape — machine-readable ``detail`` with ``reason`` discriminator,
        the list of uncovered ids, and a human hint. No new error envelope
        is introduced (operator-ratified pre-flight rule #1).
        """
        nid = uuid.uuid4()
        uncovered_a = uuid.uuid4()
        uncovered_b = uuid.uuid4()
        node = _mock_node(node_id=nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch(
                "course_supporter.api.routes.node_summaries"
                ".build_node_summary_orchestrator",
                return_value=_orch_with_scope(
                    uncovered=[uncovered_a, uncovered_b],
                    in_scope=[nid],
                ),
            ),
            patch(
                "course_supporter.api.routes.node_summaries"
                ".enqueue_node_summary_regeneration",
            ) as mock_enqueue,
        ):
            resp = await client.post(
                f"/api/v1/nodes/{nid}/summary/generate",
                json={"force": False},
            )
        assert resp.status_code == 422
        body = resp.json()
        detail = body["detail"]
        assert detail["reason"] == "uncovered_stale_nodes"
        assert set(detail["uncovered_stale_node_ids"]) == {
            str(uncovered_a),
            str(uncovered_b),
        }
        # No enqueue when the gate triggers — Job row not persisted.
        mock_enqueue.assert_not_called()

    async def test_uncovered_stale_with_force_true_returns_202(
        self, client: AsyncClient
    ) -> None:
        """K1 ratify: force=true bypasses the 422 even with uncovered ids."""
        nid = uuid.uuid4()
        uncovered_a = uuid.uuid4()
        node = _mock_node(node_id=nid)
        job = _mock_job(course_node_id=nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch(
                "course_supporter.api.routes.node_summaries"
                ".build_node_summary_orchestrator",
                return_value=_orch_with_scope(uncovered=[uncovered_a], in_scope=[nid]),
            ),
            patch(
                "course_supporter.api.routes.node_summaries"
                ".enqueue_node_summary_regeneration",
                return_value=job,
            ) as mock_enqueue,
        ):
            resp = await client.post(
                f"/api/v1/nodes/{nid}/summary/generate",
                json={"force": True},
            )
        assert resp.status_code == 202
        mock_enqueue.assert_called_once()
        assert mock_enqueue.call_args.kwargs["force"] is True

    async def test_orchestrator_run_never_invoked_synchronously(
        self, client: AsyncClient
    ) -> None:
        """Sharper inv #4: ``orch.run()`` NEVER awaited from the route.

        Read-only ``validate_scope`` IS permitted from the route (K1
        ratify — the explicit 422 API surface). The hard line is the
        generation method ``run``, which must only fire inside the
        ARQ worker. This test pins both sides at once: validate_scope
        IS called, run is NOT.
        """
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        job = _mock_job(course_node_id=nid)
        orch_mock = _orch_with_scope(uncovered=[], in_scope=[nid])
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch(
                "course_supporter.api.routes.node_summaries"
                ".build_node_summary_orchestrator",
                return_value=orch_mock,
            ),
            patch(
                "course_supporter.api.routes.node_summaries"
                ".enqueue_node_summary_regeneration",
                return_value=job,
            ),
        ):
            resp = await client.post(f"/api/v1/nodes/{nid}/summary/generate", json={})
        assert resp.status_code == 202
        # validate_scope IS called (the K1 422 surface).
        orch_mock.validate_scope.assert_awaited_once()
        # ``run`` is NEVER called from the route — inv #4 hard line.
        orch_mock.run.assert_not_awaited()


# ── GET /summary — P6 two-step contract ─────────────────────────


class TestGetSummaryP6Contract:
    async def test_missing_raw_returns_404_not_yet_generated(
        self, client: AsyncClient
    ) -> None:
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch(
                "course_supporter.api.routes.node_summaries._fetch_raw_for_node",
                return_value=None,
            ),
        ):
            resp = await client.get(f"/api/v1/nodes/{nid}/summary")
        assert resp.status_code == 404
        assert resp.json() == {"detail": {"reason": "not_yet_generated"}}

    async def test_present_raw_returns_200_with_final(
        self, client: AsyncClient
    ) -> None:
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        raw = MagicMock(course_node_id=nid)
        final = _mock_final(nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch(
                "course_supporter.api.routes.node_summaries._fetch_raw_for_node",
                return_value=raw,
            ),
            patch.object(
                NodeSummaryFinalRepository,
                "get_by_course_node_id",
                return_value=final,
            ),
        ):
            resp = await client.get(f"/api/v1/nodes/{nid}/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["course_node_id"] == str(nid)
        assert body["title"] == "test title"
        assert body["is_manual"] is False
        assert body["approved_at"] is None


# ── PATCH /final — editable family ───────────────────────────────


class TestPatchEditable:
    async def test_editable_fields_accepted(self, client: AsyncClient) -> None:
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        final = _mock_final(nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch.object(
                NodeSummaryFinalRepository, "patch_editable", return_value=final
            ) as mock_patch,
        ):
            resp = await client.patch(
                f"/api/v1/node-summaries/{nid}/final",
                json={"title": "new", "learning_objectives": ["x"]},
            )
        assert resp.status_code == 200
        passed_fields = mock_patch.call_args.args[1]
        assert passed_fields == {"title": "new", "learning_objectives": ["x"]}

    async def test_main_concepts_rejected_at_pydantic(
        self, client: AsyncClient
    ) -> None:
        """First line of defence: schema-level extra='forbid'."""
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        with patch.object(CourseNodeRepository, "get_by_id", return_value=node):
            resp = await client.patch(
                f"/api/v1/node-summaries/{nid}/final",
                json={"main_concepts": ["hacked"]},
            )
        assert resp.status_code == 422

    async def test_enclosing_context_rejected_at_pydantic(
        self, client: AsyncClient
    ) -> None:
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        with patch.object(CourseNodeRepository, "get_by_id", return_value=node):
            resp = await client.patch(
                f"/api/v1/node-summaries/{nid}/final",
                json={"enclosing_context": "hacked"},
            )
        assert resp.status_code == 422

    async def test_is_manual_rejected_at_pydantic(self, client: AsyncClient) -> None:
        """``is_manual`` is forward-compat only — PATCH must NOT flip it."""
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        with patch.object(CourseNodeRepository, "get_by_id", return_value=node):
            resp = await client.patch(
                f"/api/v1/node-summaries/{nid}/final",
                json={"is_manual": True},
            )
        assert resp.status_code == 422

    async def test_empty_body_returns_422(self, client: AsyncClient) -> None:
        """Empty PATCH body → 422 (no-op edits are an error, not 200)."""
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        with patch.object(CourseNodeRepository, "get_by_id", return_value=node):
            resp = await client.patch(f"/api/v1/node-summaries/{nid}/final", json={})
        assert resp.status_code == 422

    async def test_repository_value_error_translates_to_422(
        self, client: AsyncClient
    ) -> None:
        """Deep gate: if Pydantic ever relaxes, the repo VE still 422s."""
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch.object(
                NodeSummaryFinalRepository,
                "patch_editable",
                side_effect=ValueError("non-editable fields"),
            ),
        ):
            resp = await client.patch(
                f"/api/v1/node-summaries/{nid}/final",
                json={"title": "new"},
            )
        assert resp.status_code == 422

    async def test_repository_returns_none_when_final_absent(
        self, client: AsyncClient
    ) -> None:
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch.object(
                NodeSummaryFinalRepository, "patch_editable", return_value=None
            ),
        ):
            resp = await client.patch(
                f"/api/v1/node-summaries/{nid}/final",
                json={"title": "new"},
            )
        assert resp.status_code == 404


# ── POST /approve + POST /accept-raw ─────────────────────────────


class TestApprove:
    async def test_approve_returns_200(self, client: AsyncClient) -> None:
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        final = _mock_final(nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch.object(
                NodeSummaryFinalRepository, "approve", return_value=final
            ) as mock_approve,
        ):
            resp = await client.post(f"/api/v1/node-summaries/{nid}/final/approve")
        assert resp.status_code == 200
        mock_approve.assert_awaited_once_with(nid)

    async def test_approve_404_when_final_absent(self, client: AsyncClient) -> None:
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch.object(NodeSummaryFinalRepository, "approve", return_value=None),
        ):
            resp = await client.post(f"/api/v1/node-summaries/{nid}/final/approve")
        assert resp.status_code == 404


class TestAcceptRaw:
    async def test_accept_raw_returns_200(self, client: AsyncClient) -> None:
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        final = _mock_final(nid)
        raw = MagicMock(course_node_id=nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch(
                "course_supporter.api.routes.node_summaries._fetch_raw_for_node",
                return_value=raw,
            ),
            patch.object(
                NodeSummaryFinalRepository,
                "accept_raw",
                return_value=final,
            ) as mock_accept,
        ):
            resp = await client.post(f"/api/v1/node-summaries/{nid}/final/accept-raw")
        assert resp.status_code == 200
        mock_accept.assert_awaited_once_with(raw)

    async def test_accept_raw_404_when_raw_absent(self, client: AsyncClient) -> None:
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch(
                "course_supporter.api.routes.node_summaries._fetch_raw_for_node",
                return_value=None,
            ),
        ):
            resp = await client.post(f"/api/v1/node-summaries/{nid}/final/accept-raw")
        assert resp.status_code == 404


# ── GET /edit-view ───────────────────────────────────────────────


class TestEditView:
    async def test_edit_view_combined_payload(self, client: AsyncClient) -> None:
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        final = _mock_final(nid)
        snapshot = MagicMock()
        snapshot.snapshot = {"title": "v1", "approved_at": None}
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch.object(
                NodeSummaryFinalRepository,
                "get_edit_view",
                return_value=(final, ["obs-1", "obs-2"], snapshot),
            ),
        ):
            resp = await client.get(f"/api/v1/node-summaries/{nid}/edit-view")
        assert resp.status_code == 200
        body = resp.json()
        assert body["final"]["course_node_id"] == str(nid)
        assert body["raw_observations"] == ["obs-1", "obs-2"]
        assert body["previous_snapshot"] == {"title": "v1", "approved_at": None}

    async def test_edit_view_missing_returns_404(self, client: AsyncClient) -> None:
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch.object(
                NodeSummaryFinalRepository, "get_edit_view", return_value=None
            ),
        ):
            resp = await client.get(f"/api/v1/node-summaries/{nid}/edit-view")
        assert resp.status_code == 404

    async def test_edit_view_with_null_snapshot(self, client: AsyncClient) -> None:
        """First-time-generated node — snapshot is ``None`` in tuple."""
        nid = uuid.uuid4()
        node = _mock_node(node_id=nid)
        final = _mock_final(nid)
        with (
            patch.object(CourseNodeRepository, "get_by_id", return_value=node),
            patch.object(
                NodeSummaryFinalRepository,
                "get_edit_view",
                return_value=(final, [], None),
            ),
        ):
            resp = await client.get(f"/api/v1/node-summaries/{nid}/edit-view")
        assert resp.status_code == 200
        body = resp.json()
        assert body["previous_snapshot"] is None
        assert body["raw_observations"] == []
