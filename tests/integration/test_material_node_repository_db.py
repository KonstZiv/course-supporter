"""Integration tests for CourseNodeRepository against real PostgreSQL.

Focus: sort behaviors that depend on actual SQL semantics
(``NULLS LAST``, ``ORDER BY`` chaining), which mock-based unit tests
cannot verify reliably.

Requires ``docker compose up -d`` (PostgreSQL).
Run with::

    uv run pytest tests/integration/test_course_node_repository_db.py \\
        --run-db -v
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.orm import CourseNode, Tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


class TestSortOrder:
    """Verify ORDER BY order ASC NULLS LAST, created_at ASC across queries."""

    @staticmethod
    async def _seed_siblings(
        session: AsyncSession, parent_id: uuid.UUID | None, tenant_id: uuid.UUID
    ) -> dict[str, CourseNode]:
        """Create 4 siblings with mixed (order, created_at) configurations.

        Layout (expected sort result):
            1. integer order=0, created earliest
            2. integer order=2, created later
            3. NULL order, created earliest of NULL-group
            4. NULL order, created latest
        """
        base = datetime.now(UTC) - timedelta(hours=1)

        def _make(**kwargs: object) -> CourseNode:
            # Task 2.4.13B — route root siblings through the factory
            # (CHECK ``default_language`` lands here); children stay raw
            # (task 2.4.13 рішення 1 — child language is dead data).
            if parent_id is None:
                return make_root_course_node(tenant_id=tenant_id, **kwargs)  # type: ignore[arg-type]
            return CourseNode(tenant_id=tenant_id, parent_id=parent_id, **kwargs)  # type: ignore[arg-type]

        siblings = {
            "ordered_first": _make(
                title="ordered_first",
                order=0,
                created_at=base,
            ),
            "ordered_second": _make(
                title="ordered_second",
                order=2,
                created_at=base + timedelta(minutes=20),
            ),
            "null_old": _make(
                title="null_old",
                order=None,
                created_at=base + timedelta(minutes=5),
            ),
            "null_new": _make(
                title="null_new",
                order=None,
                created_at=base + timedelta(minutes=40),
            ),
        }
        for node in siblings.values():
            session.add(node)
        await session.flush()
        return siblings

    async def test_get_children_orders_nulls_last_then_created_at(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """get_children: integers first by order, then NULLs by created_at."""
        await self._seed_siblings(
            db_session,
            parent_id=seed_root_node.id,
            tenant_id=seed_tenant.id,
        )

        repo = CourseNodeRepository(db_session)
        result = await repo.get_children(seed_root_node.id)

        titles = [n.title for n in result]
        assert titles == [
            "ordered_first",  # order=0
            "ordered_second",  # order=2
            "null_old",  # NULL, earlier created_at
            "null_new",  # NULL, later created_at
        ]

    async def test_get_roots_applies_same_sort(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
    ) -> None:
        """get_roots: NULLs last + created_at tie-break works at root level too."""
        await self._seed_siblings(
            db_session,
            parent_id=None,
            tenant_id=seed_tenant.id,
        )

        repo = CourseNodeRepository(db_session)
        result = await repo.get_roots(seed_tenant.id)

        titles = [n.title for n in result]
        assert titles == [
            "ordered_first",
            "ordered_second",
            "null_old",
            "null_new",
        ]

    async def test_get_subtree_applies_same_sort(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """get_subtree: ordering applied across descendants under root."""
        await self._seed_siblings(
            db_session,
            parent_id=seed_root_node.id,
            tenant_id=seed_tenant.id,
        )

        repo = CourseNodeRepository(db_session)
        subtree = await repo.get_subtree(seed_root_node.id)

        # Root first, then ordered children
        assert subtree[0].id == seed_root_node.id
        children_titles = [n.title for n in seed_root_node.children]
        assert children_titles == [
            "ordered_first",
            "ordered_second",
            "null_old",
            "null_new",
        ]


class TestNextSiblingOrderWithNulls:
    """Verify _next_sibling_order behavior when siblings include NULLs."""

    async def test_max_excludes_nulls(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """_next_sibling_order returns MAX(order)+1, ignoring NULL siblings."""
        # Two ordered siblings (max=5) + one NULL
        for title, order in (("a", 3), ("b", 5), ("c", None)):
            db_session.add(
                CourseNode(
                    tenant_id=seed_tenant.id,
                    parent_id=seed_root_node.id,
                    title=title,
                    order=order,
                )
            )
        await db_session.flush()

        repo = CourseNodeRepository(db_session)
        next_order = await repo._next_sibling_order(seed_root_node.id)
        assert next_order == 6  # MAX(3, 5) + 1, NULL excluded

    async def test_returns_zero_when_all_siblings_null(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """When all siblings are NULL, _next_sibling_order falls back to 0."""
        for title in ("a", "b"):
            db_session.add(
                CourseNode(
                    tenant_id=seed_tenant.id,
                    parent_id=seed_root_node.id,
                    title=title,
                    order=None,
                )
            )
        await db_session.flush()

        repo = CourseNodeRepository(db_session)
        next_order = await repo._next_sibling_order(seed_root_node.id)
        assert next_order == 0  # coalesce(NULL+1, 0) → 0
