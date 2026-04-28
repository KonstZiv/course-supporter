"""Tests for /api/v1/cost/summary and /api/v1/cost/course/{id}.

Mocked at the repo + cache boundary — SQL semantics live in the
integration suite. Here we verify response shape, validation 422s,
tenant-isolation 404, cache hit/miss flow, and ``?no_cache=true``
bypass.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant
from course_supporter.api.routes import cost as cost_module
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.material_node_repository import MaterialNodeRepository
from course_supporter.storage.repositories import (
    ByActionRow,
    ByCourseRow,
    ByNodeRow,
    ByProviderRow,
    ExternalServiceCallRepository,
)

STUB_TENANT = TenantContext(
    tenant_id=uuid.uuid4(),
    tenant_name="test-tenant",
    scopes=["prep", "check"],
    plan_id="basic",
    key_prefix="cs_test",
)
COURSE_ID = uuid.uuid4()
NODE_ID = uuid.uuid4()


@pytest.fixture()
async def client() -> AsyncGenerator[AsyncClient]:
    """FastAPI client with mocked session, tenant, and Redis."""
    app.dependency_overrides[get_session] = lambda: MagicMock()
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    app.dependency_overrides[get_arq_redis] = lambda: MagicMock()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture()
def patched_summary_repo() -> Any:
    """Patch all four repo methods used by /cost/summary with sane defaults."""
    return patch.multiple(
        ExternalServiceCallRepository,
        get_total_for_period=AsyncMock(return_value=150.0),
        get_unattributed_for_period=AsyncMock(return_value=5.0),
        get_by_course=AsyncMock(
            return_value=[
                ByCourseRow(
                    course_node_id=COURSE_ID,
                    course_title="Python Basics",
                    cost_usd=145.0,
                )
            ]
        ),
        get_by_provider=AsyncMock(
            return_value=[
                ByProviderRow(
                    provider="anthropic",
                    model_id="claude-sonnet-4",
                    cost_usd=150.0,
                )
            ]
        ),
    )


@pytest.fixture()
def patched_course_repo() -> Any:
    """Patch repo methods + node-repo helpers used by /cost/course."""
    course_node = MagicMock()
    course_node.title = "Python Basics"
    return (
        patch.object(
            MaterialNodeRepository,
            "get_by_id_for_tenant",
            AsyncMock(return_value=course_node),
        ),
        patch.object(
            MaterialNodeRepository,
            "get_descendant_ids",
            AsyncMock(return_value=[COURSE_ID, NODE_ID]),
        ),
        patch.multiple(
            ExternalServiceCallRepository,
            get_total_for_subtree=AsyncMock(return_value=100.0),
            get_by_node=AsyncMock(
                return_value=[
                    ByNodeRow(course_node_id=NODE_ID, title="Lesson 1", cost_usd=30.0)
                ]
            ),
            get_by_action=AsyncMock(
                return_value=[ByActionRow(action="document_processing", cost_usd=60.0)]
            ),
        ),
    )


# ── /cost/summary ─────────────────────────────────────────────────


class TestCostSummaryHappyPath:
    async def test_returns_200_and_documented_shape(
        self, client: AsyncClient, patched_summary_repo: Any
    ) -> None:
        with (
            patched_summary_repo,
            patch.object(cost_module, "get_cached", AsyncMock(return_value=None)),
            patch.object(cost_module, "set_cached", AsyncMock()),
        ):
            response = await client.get(
                "/api/v1/cost/summary",
                params={"from": "2026-01-01", "to": "2026-04-28"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["from"] == "2026-01-01"
        assert data["to"] == "2026-04-28"
        assert data["total_usd"] == 150.0
        assert data["unattributed_cost_usd"] == 5.0
        assert len(data["by_course"]) == 1
        assert data["by_course"][0]["course_title"] == "Python Basics"
        assert len(data["by_provider"]) == 1
        assert data["by_provider"][0]["provider"] == "anthropic"


class TestCostSummaryValidation:
    @pytest.mark.parametrize(
        ("params", "expected_msg"),
        [
            # Pydantic-shaped 422s — actual v2 message strings.
            ({"to": "2026-04-28"}, "Field required"),  # missing from
            ({"from": "2026-01-01"}, "Field required"),  # missing to
            (
                {"from": "2026-01-01", "to": "2026-04-28", "limit_courses": "501"},
                "Input should be less than or equal to 500",
            ),
            (
                {"from": "2026-01-01", "to": "2026-04-28", "offset_courses": "-1"},
                "Input should be greater than or equal to 0",
            ),
            (
                {"from": "2026-01-01", "to": "2026-04-28", "limit_providers": "501"},
                "Input should be less than or equal to 500",
            ),
            (
                {"from": "not-a-date", "to": "2026-04-28"},
                "Input should be a valid date",
            ),
            # Manual HTTPException — string detail, separate shape.
            ({"from": "2026-04-28", "to": "2026-01-01"}, "from must be <= to"),
        ],
    )
    async def test_returns_422(
        self,
        client: AsyncClient,
        params: dict[str, str],
        expected_msg: str,
    ) -> None:
        response = await client.get("/api/v1/cost/summary", params=params)
        assert response.status_code == 422

        detail = response.json()["detail"]
        # Pydantic 422 → list[ErrorDict]; manual HTTPException → str.
        if isinstance(detail, list):
            msgs = [e.get("msg", "") for e in detail]
            assert any(expected_msg in m for m in msgs), (
                f"Expected substring {expected_msg!r} in any of {msgs!r}"
            )
        else:
            assert expected_msg in detail


class TestCostSummaryCache:
    async def test_cache_miss_calls_repo_then_writes_cache(
        self, client: AsyncClient, patched_summary_repo: Any
    ) -> None:
        get_cached_mock = AsyncMock(return_value=None)
        set_cached_mock = AsyncMock()
        with (
            patched_summary_repo as m,
            patch.object(cost_module, "get_cached", get_cached_mock),
            patch.object(cost_module, "set_cached", set_cached_mock),
        ):
            response = await client.get(
                "/api/v1/cost/summary",
                params={"from": "2026-01-01", "to": "2026-04-28"},
            )
        assert response.status_code == 200
        get_cached_mock.assert_awaited_once()
        set_cached_mock.assert_awaited_once()
        # Each repo method invoked exactly once.
        for method in m.values():
            method.assert_awaited_once()

    async def test_cache_hit_skips_repo(
        self, client: AsyncClient, patched_summary_repo: Any
    ) -> None:
        cached_payload = (
            '{"from":"2026-01-01","to":"2026-04-28","total_usd":99.0,'
            '"unattributed_cost_usd":1.0,"by_course":[],"by_provider":[]}'
        )
        get_cached_mock = AsyncMock(return_value=cached_payload)
        set_cached_mock = AsyncMock()
        with (
            patched_summary_repo as m,
            patch.object(cost_module, "get_cached", get_cached_mock),
            patch.object(cost_module, "set_cached", set_cached_mock),
        ):
            response = await client.get(
                "/api/v1/cost/summary",
                params={"from": "2026-01-01", "to": "2026-04-28"},
            )
        assert response.status_code == 200
        assert response.json()["total_usd"] == 99.0
        get_cached_mock.assert_awaited_once()
        set_cached_mock.assert_not_awaited()
        for method in m.values():
            method.assert_not_called()

    async def test_no_cache_param_bypasses_both_paths(
        self, client: AsyncClient, patched_summary_repo: Any
    ) -> None:
        get_cached_mock = AsyncMock()
        set_cached_mock = AsyncMock()
        with (
            patched_summary_repo as m,
            patch.object(cost_module, "get_cached", get_cached_mock),
            patch.object(cost_module, "set_cached", set_cached_mock),
            patch.object(cost_module, "logger") as mock_logger,
        ):
            response = await client.get(
                "/api/v1/cost/summary",
                params={
                    "from": "2026-01-01",
                    "to": "2026-04-28",
                    "no_cache": "true",
                },
            )
        assert response.status_code == 200
        # Audit-log every cache bypass — even though rate limit bounds
        # absolute QPS, this gives observability into anomalous traffic
        # shape (e.g., one tenant always-bypassing).
        mock_logger.warning.assert_called_once()
        warning_call = mock_logger.warning.call_args
        assert warning_call.args[0] == "cost_no_cache_bypass"
        assert warning_call.kwargs["endpoint"] == "/cost/summary"
        get_cached_mock.assert_not_called()
        set_cached_mock.assert_not_called()
        for method in m.values():
            method.assert_awaited_once()

    async def test_no_warning_on_normal_request(
        self, client: AsyncClient, patched_summary_repo: Any
    ) -> None:
        """Symmetry: WARNING fires *only* on bypass, not on every request."""
        with (
            patched_summary_repo,
            patch.object(cost_module, "get_cached", AsyncMock(return_value=None)),
            patch.object(cost_module, "set_cached", AsyncMock()),
            patch.object(cost_module, "logger") as mock_logger,
        ):
            response = await client.get(
                "/api/v1/cost/summary",
                params={"from": "2026-01-01", "to": "2026-04-28"},
            )
        assert response.status_code == 200
        mock_logger.warning.assert_not_called()


# ── /cost/course/{id} ─────────────────────────────────────────────


class TestCostCourseHappyPath:
    async def test_returns_200_with_drill_down_shape(
        self, client: AsyncClient, patched_course_repo: Any
    ) -> None:
        node_get, descendants, repo_methods = patched_course_repo
        with (
            node_get,
            descendants,
            repo_methods,
            patch.object(cost_module, "get_cached", AsyncMock(return_value=None)),
            patch.object(cost_module, "set_cached", AsyncMock()),
        ):
            response = await client.get(
                f"/api/v1/cost/course/{COURSE_ID}",
                params={"from": "2026-01-01", "to": "2026-04-28"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["course_node_id"] == str(COURSE_ID)
        assert data["course_title"] == "Python Basics"
        assert data["total_usd"] == 100.0
        assert data["by_node"][0]["title"] == "Lesson 1"
        assert data["by_action"][0]["action"] == "document_processing"


class TestCostCourseTenantIsolation:
    async def test_foreign_tenant_returns_404(self, client: AsyncClient) -> None:
        with patch.object(
            MaterialNodeRepository,
            "get_by_id_for_tenant",
            AsyncMock(return_value=None),
        ):
            response = await client.get(
                f"/api/v1/cost/course/{uuid.uuid4()}",
                params={"from": "2026-01-01", "to": "2026-04-28"},
            )
        assert response.status_code == 404
        assert response.json()["detail"] == "Course not found"

    async def test_descendant_resolution_passes_tenant_id(
        self, client: AsyncClient, patched_course_repo: Any
    ) -> None:
        """Defense-in-depth: tenant_id must reach the recursive CTE."""
        node_get, descendants, repo_methods = patched_course_repo
        with (
            node_get,
            descendants as descendants_mock,
            repo_methods,
            patch.object(cost_module, "get_cached", AsyncMock(return_value=None)),
            patch.object(cost_module, "set_cached", AsyncMock()),
        ):
            response = await client.get(
                f"/api/v1/cost/course/{COURSE_ID}",
                params={"from": "2026-01-01", "to": "2026-04-28"},
            )
        assert response.status_code == 200
        descendants_mock.assert_awaited_once()
        kwargs = descendants_mock.await_args.kwargs
        assert kwargs.get("tenant_id") == STUB_TENANT.tenant_id


class TestCostCourseValidation:
    @pytest.mark.parametrize(
        ("params", "expected_msg"),
        [
            # Pydantic-shaped 422s — actual v2 message strings.
            ({"to": "2026-04-28"}, "Field required"),  # missing from
            ({"from": "2026-01-01"}, "Field required"),  # missing to
            (
                {"from": "2026-01-01", "to": "2026-04-28", "limit_nodes": "501"},
                "Input should be less than or equal to 500",
            ),
            (
                {"from": "2026-01-01", "to": "2026-04-28", "offset_nodes": "-1"},
                "Input should be greater than or equal to 0",
            ),
            (
                {"from": "2026-01-01", "to": "2026-04-28", "limit_actions": "501"},
                "Input should be less than or equal to 500",
            ),
            (
                {"from": "2026-01-01", "to": "2026-04-28", "offset_actions": "-1"},
                "Input should be greater than or equal to 0",
            ),
            (
                {"from": "not-a-date", "to": "2026-04-28"},
                "Input should be a valid date",
            ),
            # Manual HTTPException — string detail, separate shape.
            ({"from": "2026-04-28", "to": "2026-01-01"}, "from must be <= to"),
        ],
    )
    async def test_returns_422(
        self,
        client: AsyncClient,
        params: dict[str, str],
        expected_msg: str,
    ) -> None:
        response = await client.get(f"/api/v1/cost/course/{COURSE_ID}", params=params)
        assert response.status_code == 422

        detail = response.json()["detail"]
        # Pydantic 422 → list[ErrorDict]; manual HTTPException → str.
        if isinstance(detail, list):
            msgs = [e.get("msg", "") for e in detail]
            assert any(expected_msg in m for m in msgs), (
                f"Expected substring {expected_msg!r} in any of {msgs!r}"
            )
        else:
            assert expected_msg in detail


class TestCostCourseCache:
    async def test_cache_hit_skips_descendant_resolution(
        self, client: AsyncClient, patched_course_repo: Any
    ) -> None:
        node_get, descendants, repo_methods = patched_course_repo
        cached_payload = (
            f'{{"course_node_id":"{COURSE_ID}","course_title":"Python Basics",'
            f'"from":"2026-01-01","to":"2026-04-28","total_usd":42.0,'
            f'"by_node":[],"by_action":[]}}'
        )
        get_cached_mock = AsyncMock(return_value=cached_payload)
        set_cached_mock = AsyncMock()
        with (
            node_get as node_mock,
            descendants as descendants_mock,
            repo_methods,
            patch.object(cost_module, "get_cached", get_cached_mock),
            patch.object(cost_module, "set_cached", set_cached_mock),
        ):
            response = await client.get(
                f"/api/v1/cost/course/{COURSE_ID}",
                params={"from": "2026-01-01", "to": "2026-04-28"},
            )
        assert response.status_code == 200
        assert response.json()["total_usd"] == 42.0
        # Tenant lookup still runs (404 gating); descendant resolution
        # short-circuits — no need to walk the tree on a cache hit.
        node_mock.assert_awaited_once()
        descendants_mock.assert_not_awaited()
        set_cached_mock.assert_not_awaited()
