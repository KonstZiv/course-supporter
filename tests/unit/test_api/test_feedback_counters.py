"""The author's read of the counters (mentor-rebuild task 05).

``get_current_tenant`` is overridden; the repository is mocked. What belongs to
the ROUTES and is tested here: the scope is PREP and only PREP, the tenant
handed to the repository is the KEY's, the label is composed the way the cost
breakdown composes it, and an empty answer is an empty answer rather than a 404.

That the counting itself excludes a foreign tenant, a soft-deleted submission
and a soft-deleted student is SQL, and it is proved against a live database in
``tests/integration/test_feedback_repository_db.py`` — a mocked repository
cannot fail those.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.deps import get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.feedback_repository import (
    Counters,
    FeedbackRepository,
    TaskCountersRow,
)

STUB_TENANT_ID = uuid.uuid4()
STUB_TENANT = TenantContext(
    tenant_id=STUB_TENANT_ID,
    tenant_name="test-tenant",
    scopes=["prep", "check"],
    plan_id="basic",
    key_prefix="cs_test",
)
CHECK_ONLY_TENANT = TenantContext(
    tenant_id=STUB_TENANT_ID,
    tenant_name="check-only",
    scopes=["check"],
    plan_id="basic",
    key_prefix="cs_check",
)


def _row(
    *,
    helped: int = 3,
    not_helped: int = 1,
    is_deleted: bool = False,
) -> TaskCountersRow:
    return TaskCountersRow(
        authored_document_id=uuid.uuid4(),
        filename="homework.md",
        source_type="text",
        order=2,
        is_deleted=is_deleted,
        helped=helped,
        not_helped=not_helped,
    )


@pytest.fixture()
def mock_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture()
async def client(mock_session: AsyncMock) -> AsyncGenerator[AsyncClient]:
    app.dependency_overrides[get_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant] = lambda: STUB_TENANT
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


def _course_url(course_node_id: uuid.UUID) -> str:
    return f"/api/v1/feedback/counters/course/{course_node_id}"


def _task_url(authored_document_id: uuid.UUID) -> str:
    return f"/api/v1/feedback/counters/task/{authored_document_id}"


class TestTheCourseLevel:
    async def test_the_breakdown_carries_counters_and_the_shared_label(
        self, client: AsyncClient
    ) -> None:
        """One task, its two numbers, and the label the cost pages already use."""
        row = _row()
        course_id = uuid.uuid4()
        with patch.object(
            FeedbackRepository, "counters_by_task", new=AsyncMock(return_value=[row])
        ):
            resp = await client.get(_course_url(course_id))
        assert resp.status_code == 200
        body = resp.json()
        assert body["course_node_id"] == str(course_id)
        assert body["by_task"] == [
            {
                "authored_document_id": str(row.authored_document_id),
                "task_label": "homework.md",
                "is_deleted": False,
                "helped": 3,
                "not_helped": 1,
            }
        ]

    async def test_a_course_with_nothing_to_show_is_empty_not_a_404(
        self, client: AsyncClient
    ) -> None:
        """Leak-safe: a foreign or unknown course id is answered, not refused."""
        with patch.object(
            FeedbackRepository, "counters_by_task", new=AsyncMock(return_value=[])
        ):
            resp = await client.get(_course_url(uuid.uuid4()))
        assert resp.status_code == 200
        assert resp.json()["by_task"] == []

    async def test_a_soft_deleted_task_keeps_its_row(self, client: AsyncClient) -> None:
        """Shown, not filtered — the answers about its reviews were still given."""
        with patch.object(
            FeedbackRepository,
            "counters_by_task",
            new=AsyncMock(return_value=[_row(is_deleted=True)]),
        ):
            resp = await client.get(_course_url(uuid.uuid4()))
        assert resp.json()["by_task"][0]["is_deleted"] is True


class TestTheTaskLevel:
    async def test_the_totals_come_back_whole(self, client: AsyncClient) -> None:
        task_id = uuid.uuid4()
        with patch.object(
            FeedbackRepository,
            "counters_for_task",
            new=AsyncMock(return_value=Counters(helped=7, not_helped=2)),
        ):
            resp = await client.get(_task_url(task_id))
        assert resp.status_code == 200
        assert resp.json() == {
            "authored_document_id": str(task_id),
            "helped": 7,
            "not_helped": 2,
        }

    async def test_a_task_with_nothing_to_show_is_zeros_not_a_404(
        self, client: AsyncClient
    ) -> None:
        with patch.object(
            FeedbackRepository,
            "counters_for_task",
            new=AsyncMock(return_value=Counters(helped=0, not_helped=0)),
        ):
            resp = await client.get(_task_url(uuid.uuid4()))
        assert resp.status_code == 200
        assert (resp.json()["helped"], resp.json()["not_helped"]) == (0, 0)


class TestTheTenantHandedToTheCountingIsTheKeys:
    async def test_the_course_level_counts_within_the_keys_tenant(
        self, client: AsyncClient
    ) -> None:
        """The route reads the tenant off the KEY, not off the path or a guess."""
        course_id = uuid.uuid4()
        with patch.object(
            FeedbackRepository, "counters_by_task", new=AsyncMock(return_value=[])
        ) as counted:
            await client.get(_course_url(course_id))
        assert counted.await_args.kwargs["tenant_id"] == STUB_TENANT_ID
        assert counted.await_args.kwargs["course_node_id"] == course_id

    async def test_the_task_level_counts_within_the_keys_tenant(
        self, client: AsyncClient
    ) -> None:
        task_id = uuid.uuid4()
        with patch.object(
            FeedbackRepository,
            "counters_for_task",
            new=AsyncMock(return_value=Counters(helped=0, not_helped=0)),
        ) as counted:
            await client.get(_task_url(task_id))
        assert counted.await_args.kwargs == {
            "tenant_id": STUB_TENANT_ID,
            "authored_document_id": task_id,
        }


class TestTheScopeIsPrepAndOnlyPrep:
    async def test_a_check_key_cannot_read_the_course_level(
        self, client: AsyncClient
    ) -> None:
        """The channel touches; it does not read the author's statistics."""
        app.dependency_overrides[get_current_tenant] = lambda: CHECK_ONLY_TENANT
        with patch.object(
            FeedbackRepository, "counters_by_task", new=AsyncMock(return_value=[])
        ) as counted:
            resp = await client.get(_course_url(uuid.uuid4()))
        assert resp.status_code == 403
        counted.assert_not_awaited()

    async def test_a_check_key_cannot_read_the_task_level(
        self, client: AsyncClient
    ) -> None:
        app.dependency_overrides[get_current_tenant] = lambda: CHECK_ONLY_TENANT
        with patch.object(
            FeedbackRepository,
            "counters_for_task",
            new=AsyncMock(return_value=Counters(helped=0, not_helped=0)),
        ) as counted:
            resp = await client.get(_task_url(uuid.uuid4()))
        assert resp.status_code == 403
        counted.assert_not_awaited()
