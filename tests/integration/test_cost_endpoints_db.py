"""End-to-end integration: /cost/summary + /cost/course over real DB+Redis.

Uses the FastAPI app with overridden ``get_session`` (test session),
``get_arq_redis`` (real Redis pool), and ``get_current_tenant`` (stub
tenant scoped to a freshly-seeded Tenant id). Each test class sets up
its own Tenant + CourseNode tree + Jobs + ESCs; cleanup runs in
reverse FK order at teardown.

Coverage:
* KD5 invariant: ``total_usd == sum(by_course) + unattributed_cost_usd``
  with non-trivial data (5 attributed Jobs + 2 unattributed Jobs +
  ESCs at varying cost).
* Drill-down via real recursive CTE: subtree by_node + by_action.
* Pagination correctness: 75 distinct (provider, model_id) combos with
  limit_providers=50 → exactly 50 returned, ordered cost DESC.
* NULL ``cost_usd`` excluded from sums (not coalesced to zero).
* Cache hit on second call: real Redis spy via repo-method counter.
* Tenant isolation: foreign tenant's course_node_id → 404.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from arq.connections import ArqRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import get_arq_redis, get_current_tenant
from course_supporter.auth.context import TenantContext
from course_supporter.storage.database import get_session
from course_supporter.storage.orm import (
    CourseNode,
    ExternalServiceCall,
    Job,
    Tenant,
)
from course_supporter.storage.repositories import ExternalServiceCallRepository
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = [pytest.mark.requires_db, pytest.mark.requires_redis]


PERIOD_FROM = "2026-01-01"
PERIOD_TO = "2026-12-31"
SAMPLE_TIMESTAMP = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
# Anchor the seeded Tenant.created_at to a date that precedes
# SAMPLE_TIMESTAMP so omitted-``from`` requests (which fall back to
# ``tenant.created_at``) cover the full ESC seed without depending on
# wall-clock time at test runtime.
TENANT_CREATED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


@pytest.fixture()
async def cost_seed(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, object]]:
    """Seed Tenant + 3-node tree + 5 attributed Jobs + 2 unattributed Jobs.

    Cost layout (lets every assertion in the suite read against one
    consistent data set):

    * Course root → Lesson 1 → Concept A.
    * Job_R, Job_L, Job_C target root, lesson, concept respectively.
    * Job_X1, Job_X2 also target root (extra attributed cost on root).
    * Job_U1, Job_U2 are unattributed (course_node_id IS NULL).
    * ESCs:
      - root: 50.0 + 20.0 = 70.0  (anthropic / claude-sonnet-4)
      - lesson: 30.0              (openai / gpt-4o)
      - concept: 25.0             (openai / gpt-4o)
      - X1: 10.0                  (anthropic / claude-sonnet-4)
      - X2: 5.0                   (anthropic / claude-haiku)
      - unattributed: 4.0 + 1.0   (deepseek / deepseek-v3)
      - 2 NULL-cost rows on root (must be excluded from every sum)
    * total_usd = 145.0; unattributed = 5.0; by_course rows = 3
      (root=85.0, lesson=30.0, concept=25.0).

    The tree shape lets ``/cost/course/{root_id}`` exercise the
    recursive-CTE descendant path covering all three nodes.
    """
    async with session_factory() as session:
        # Pin ``created_at`` so the 0.7 omitted-``from`` fallback
        # (resolved to ``tenant.created_at.date()``) is deterministic
        # and precedes ``SAMPLE_TIMESTAMP`` — otherwise the DB
        # server-default ``now()`` would produce a tenant younger than
        # the seeded ESCs and exclude them from the all-time aggregate.
        tenant = Tenant(
            name=f"cost-e2e-{uuid.uuid4().hex[:6]}",
            created_at=TENANT_CREATED_AT,
        )
        session.add(tenant)
        await session.flush()

        root = make_root_course_node(
            tenant_id=tenant.id, title="Python Basics", order=0
        )
        session.add(root)
        await session.flush()
        lesson = CourseNode(
            tenant_id=tenant.id,
            title="Lesson 1",
            order=0,
            parent_id=root.id,
        )
        session.add(lesson)
        await session.flush()
        concept = CourseNode(
            tenant_id=tenant.id,
            title="Concept A",
            order=0,
            parent_id=lesson.id,
        )
        session.add(concept)
        await session.flush()

        def _job(course_node_id: uuid.UUID | None) -> Job:
            return Job(
                tenant_id=tenant.id,
                course_node_id=course_node_id,
                job_type="ingest",
            )

        job_r = _job(root.id)
        job_l = _job(lesson.id)
        job_c = _job(concept.id)
        job_x1 = _job(root.id)
        job_x2 = _job(root.id)
        job_u1 = _job(None)
        job_u2 = _job(None)
        for j in (job_r, job_l, job_c, job_x1, job_x2, job_u1, job_u2):
            session.add(j)
        await session.flush()

        def _esc(
            job_id: uuid.UUID,
            provider: str,
            model_id: str,
            cost: float | None,
            action: str = "document_processing",
        ) -> ExternalServiceCall:
            return ExternalServiceCall(
                job_id=job_id,
                provider=provider,
                model_id=model_id,
                cost_usd=cost,
                action=action,
                created_at=SAMPLE_TIMESTAMP,
            )

        session.add_all(
            [
                _esc(job_r.id, "anthropic", "claude-sonnet-4", 50.0),
                _esc(job_r.id, "anthropic", "claude-sonnet-4", 20.0, "methodist"),
                _esc(job_l.id, "openai", "gpt-4o", 30.0),
                _esc(job_c.id, "openai", "gpt-4o", 25.0),
                _esc(job_x1.id, "anthropic", "claude-sonnet-4", 10.0),
                _esc(job_x2.id, "anthropic", "claude-haiku", 5.0),
                _esc(job_u1.id, "deepseek", "deepseek-v3", 4.0),
                _esc(job_u2.id, "deepseek", "deepseek-v3", 1.0),
                # NULL-cost rows that must be excluded from every sum.
                _esc(job_r.id, "anthropic", "claude-sonnet-4", None, "stt"),
                _esc(job_r.id, "anthropic", "claude-sonnet-4", None, "stt"),
            ]
        )
        await session.flush()
        await session.commit()

        seed = {
            "tenant_id": tenant.id,
            "root_id": root.id,
            "lesson_id": lesson.id,
            "concept_id": concept.id,
            "expected_total": 145.0,
            "expected_unattributed": 5.0,
            "expected_by_course": {
                root.id: 85.0,
                lesson.id: 30.0,
                concept.id: 25.0,
            },
        }

    yield seed

    async with session_factory() as session:
        job_ids_subq = (
            Job.__table__.select()
            .with_only_columns(Job.id)
            .where(Job.tenant_id == seed["tenant_id"])
            .scalar_subquery()
        )
        await session.execute(
            ExternalServiceCall.__table__.delete().where(
                ExternalServiceCall.job_id.in_(job_ids_subq)
            )
        )
        await session.execute(
            Job.__table__.delete().where(Job.tenant_id == seed["tenant_id"])
        )
        await session.execute(
            CourseNode.__table__.delete().where(
                CourseNode.tenant_id == seed["tenant_id"]
            )
        )
        await session.execute(
            Tenant.__table__.delete().where(Tenant.id == seed["tenant_id"])
        )
        await session.commit()


@pytest.fixture()
async def cost_client(
    cost_seed: dict[str, object],
    session_factory: async_sessionmaker[AsyncSession],
    arq_redis: ArqRedis,
) -> AsyncGenerator[AsyncClient]:
    """FastAPI client wired to the test session, real Redis, seeded tenant."""
    tenant_ctx = TenantContext(
        tenant_id=cost_seed["tenant_id"],  # type: ignore[arg-type]
        tenant_name="cost-test",
        scopes=["prep", "check"],
        plan_id="basic",
        key_prefix="cs_test",
    )

    async def _yield_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _yield_session
    app.dependency_overrides[get_arq_redis] = lambda: arq_redis
    app.dependency_overrides[get_current_tenant] = lambda: tenant_ctx

    # Flush any stale cost-* keys from previous test runs.
    keys = await arq_redis.keys("cost:*")
    if keys:
        await arq_redis.delete(*keys)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
    keys = await arq_redis.keys("cost:*")
    if keys:
        await arq_redis.delete(*keys)


# ── /cost/summary ─────────────────────────────────────────────────


class TestCostSummaryE2E:
    async def test_total_invariant(
        self, cost_client: AsyncClient, cost_seed: dict[str, object]
    ) -> None:
        """KD5: ``total_usd == sum(by_course) + unattributed_cost_usd``."""
        response = await cost_client.get(
            "/api/v1/cost/summary",
            params={"from": PERIOD_FROM, "to": PERIOD_TO, "no_cache": "true"},
        )
        assert response.status_code == 200
        data = response.json()

        assert data["total_usd"] == pytest.approx(cost_seed["expected_total"])
        assert data["unattributed_cost_usd"] == pytest.approx(
            cost_seed["expected_unattributed"]
        )
        sum_by_course = sum(c["cost_usd"] for c in data["by_course"])
        assert sum_by_course + data["unattributed_cost_usd"] == pytest.approx(
            data["total_usd"]
        )

    async def test_by_course_includes_all_attributed_nodes(
        self, cost_client: AsyncClient, cost_seed: dict[str, object]
    ) -> None:
        response = await cost_client.get(
            "/api/v1/cost/summary",
            params={"from": PERIOD_FROM, "to": PERIOD_TO, "no_cache": "true"},
        )
        data = response.json()
        actual = {c["course_node_id"]: c["cost_usd"] for c in data["by_course"]}
        expected = {
            str(node_id): cost
            for node_id, cost in cost_seed["expected_by_course"].items()  # type: ignore[union-attr]
        }
        assert actual == pytest.approx(expected)

    async def test_null_cost_excluded(
        self,
        cost_client: AsyncClient,
        cost_seed: dict[str, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """NULL ``cost_usd`` rows are excluded entirely (not coalesced to 0).

        Add a NULL-cost row on a unique provider+model — if NULLs were
        included, that provider would appear in ``by_provider`` (with
        cost 0). With exclusion it must be absent.
        """
        async with session_factory() as session:
            job = Job(
                tenant_id=cost_seed["tenant_id"],  # type: ignore[arg-type]
                course_node_id=cost_seed["root_id"],  # type: ignore[arg-type]
                job_type="ingest",
            )
            session.add(job)
            await session.flush()
            session.add(
                ExternalServiceCall(
                    job_id=job.id,
                    provider="null-only",
                    model_id="null-only-v0",
                    cost_usd=None,
                    created_at=SAMPLE_TIMESTAMP,
                )
            )
            await session.commit()

        response = await cost_client.get(
            "/api/v1/cost/summary",
            params={"from": PERIOD_FROM, "to": PERIOD_TO, "no_cache": "true"},
        )
        data = response.json()
        # Total unchanged — the new NULL contributes nothing.
        assert data["total_usd"] == pytest.approx(cost_seed["expected_total"])
        # Provider absent from breakdown — exclusion, not coalescion.
        providers = {row["provider"] for row in data["by_provider"]}
        assert "null-only" not in providers

    async def test_by_provider_pagination(
        self,
        cost_client: AsyncClient,
        cost_seed: dict[str, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """75 distinct (provider, model_id) → limit_providers=50 returns 50."""
        async with session_factory() as session:
            job = Job(
                tenant_id=cost_seed["tenant_id"],  # type: ignore[arg-type]
                course_node_id=cost_seed["root_id"],  # type: ignore[arg-type]
                job_type="ingest",
            )
            session.add(job)
            await session.flush()
            session.add_all(
                [
                    ExternalServiceCall(
                        job_id=job.id,
                        provider=f"prov-{i:03d}",
                        model_id=f"model-{i:03d}",
                        cost_usd=float(75 - i),
                        created_at=SAMPLE_TIMESTAMP,
                    )
                    for i in range(75)
                ]
            )
            await session.commit()

        response = await cost_client.get(
            "/api/v1/cost/summary",
            params={
                "from": PERIOD_FROM,
                "to": PERIOD_TO,
                "limit_providers": "50",
                "no_cache": "true",
            },
        )
        data = response.json()
        # Pagination cap respected — exactly 50 rows on the first page
        # despite 75 new + 5 seed providers in scope.
        assert len(data["by_provider"]) == 50
        # ORDER BY cost DESC — strictly non-increasing within the page.
        costs = [row["cost_usd"] for row in data["by_provider"]]
        assert costs == sorted(costs, reverse=True)

    async def test_cache_hit_skips_repo(
        self,
        cost_client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Second identical request inside TTL doesn't call the repo."""
        call_log: list[str] = []
        original = ExternalServiceCallRepository.get_total_for_period

        async def _spy(self: ExternalServiceCallRepository, **kw: object) -> float:
            call_log.append("get_total_for_period")
            return await original(self, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(ExternalServiceCallRepository, "get_total_for_period", _spy)

        params = {"from": PERIOD_FROM, "to": PERIOD_TO}
        first = await cost_client.get("/api/v1/cost/summary", params=params)
        second = await cost_client.get("/api/v1/cost/summary", params=params)
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json() == second.json()
        assert len(call_log) == 1, f"Expected one repo hit total; got {len(call_log)}"


# ── /cost/course/{id} ─────────────────────────────────────────────


class TestCostCourseE2E:
    async def test_drill_down_covers_subtree(
        self, cost_client: AsyncClient, cost_seed: dict[str, object]
    ) -> None:
        response = await cost_client.get(
            f"/api/v1/cost/course/{cost_seed['root_id']}",
            params={"from": PERIOD_FROM, "to": PERIOD_TO, "no_cache": "true"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["course_node_id"] == str(cost_seed["root_id"])
        assert data["course_title"] == "Python Basics"
        # Subtree total = root (85) + lesson (30) + concept (25) = 140.
        assert data["total_usd"] == pytest.approx(140.0)
        node_ids = {n["course_node_id"] for n in data["by_node"]}
        assert node_ids == {
            str(cost_seed["root_id"]),
            str(cost_seed["lesson_id"]),
            str(cost_seed["concept_id"]),
        }

    async def test_by_action_breakdown(
        self, cost_client: AsyncClient, cost_seed: dict[str, object]
    ) -> None:
        response = await cost_client.get(
            f"/api/v1/cost/course/{cost_seed['root_id']}",
            params={"from": PERIOD_FROM, "to": PERIOD_TO, "no_cache": "true"},
        )
        data = response.json()
        actions = {a["action"]: a["cost_usd"] for a in data["by_action"]}
        # document_processing dominates (50 + 30 + 25 + 10 + 5 = 120).
        # methodist owns 20.
        assert actions["document_processing"] == pytest.approx(120.0)
        assert actions["methodist"] == pytest.approx(20.0)

    async def test_foreign_tenant_returns_404(
        self,
        cost_client: AsyncClient,
        cost_seed: dict[str, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Course belongs to another tenant → 404 (existence not leaked)."""
        async with session_factory() as session:
            other = Tenant(name=f"other-{uuid.uuid4().hex[:6]}")
            session.add(other)
            await session.flush()
            other_node = make_root_course_node(
                tenant_id=other.id, title="Other Course", order=0
            )
            session.add(other_node)
            await session.commit()
            other_node_id = other_node.id
            other_tenant_id = other.id

        try:
            response = await cost_client.get(
                f"/api/v1/cost/course/{other_node_id}",
                params={"from": PERIOD_FROM, "to": PERIOD_TO},
            )
            assert response.status_code == 404
        finally:
            async with session_factory() as session:
                await session.execute(
                    CourseNode.__table__.delete().where(CourseNode.id == other_node_id)
                )
                await session.execute(
                    Tenant.__table__.delete().where(Tenant.id == other_tenant_id)
                )
                await session.commit()

    async def test_cross_tenant_subtree_leak_protected(
        self,
        cost_client: AsyncClient,
        cost_seed: dict[str, object],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Defense-in-depth: a stray foreign-tenant node parented inside
        the caller's subtree must not leak into ``by_node``.

        Simulates a data anomaly — manually inserts a ``CourseNode``
        belonging to a different tenant whose ``parent_id``
        points at the caller's lesson. The recursive CTE in
        ``get_descendant_ids`` is now passed ``tenant_id``; the
        anomalous node must be filtered out at the recursion boundary.
        Without the fix, any ESC attached to a Job pointing at this
        intruder node would surface in the caller's drill-down.
        """
        async with session_factory() as session:
            other_tenant = Tenant(name=f"intruder-{uuid.uuid4().hex[:6]}")
            session.add(other_tenant)
            await session.flush()
            intruder_node = CourseNode(
                tenant_id=other_tenant.id,
                title="INTRUDER (foreign tenant)",
                order=0,
                # Parent points at the caller's lesson — invariant
                # violation we are explicitly defending against.
                parent_id=cost_seed["lesson_id"],  # type: ignore[arg-type]
            )
            session.add(intruder_node)
            await session.flush()
            intruder_job = Job(
                tenant_id=other_tenant.id,
                course_node_id=intruder_node.id,
                job_type="ingest",
            )
            session.add(intruder_job)
            await session.flush()
            session.add(
                ExternalServiceCall(
                    job_id=intruder_job.id,
                    provider="anthropic",
                    model_id="claude-sonnet-4",
                    cost_usd=999.0,
                    created_at=SAMPLE_TIMESTAMP,
                )
            )
            await session.commit()
            intruder_node_id = intruder_node.id
            intruder_job_id = intruder_job.id
            intruder_tenant_id = other_tenant.id

        try:
            response = await cost_client.get(
                f"/api/v1/cost/course/{cost_seed['root_id']}",
                params={"from": PERIOD_FROM, "to": PERIOD_TO, "no_cache": "true"},
            )
            assert response.status_code == 200
            data = response.json()
            # Intruder node must NOT appear in the drill-down.
            node_ids = {n["course_node_id"] for n in data["by_node"]}
            assert str(intruder_node_id) not in node_ids
            # Total stays at the legitimate 140 — intruder's 999 USD
            # was not summed.
            assert data["total_usd"] == pytest.approx(140.0)
        finally:
            async with session_factory() as session:
                await session.execute(
                    ExternalServiceCall.__table__.delete().where(
                        ExternalServiceCall.job_id == intruder_job_id
                    )
                )
                await session.execute(
                    Job.__table__.delete().where(Job.id == intruder_job_id)
                )
                await session.execute(
                    CourseNode.__table__.delete().where(
                        CourseNode.id == intruder_node_id
                    )
                )
                await session.execute(
                    Tenant.__table__.delete().where(Tenant.id == intruder_tenant_id)
                )
                await session.commit()


# ── Optional date filter (0.7) ─────────────────────────────────────


class TestCostEndpointsFallbackE2E:
    """Omitted ``from``/``to`` resolves to ``[tenant.created_at, today]``.

    The seed pins ``Tenant.created_at`` to ``TENANT_CREATED_AT``
    (2026-01-01) and ``ExternalServiceCall.created_at`` to
    ``SAMPLE_TIMESTAMP`` (2026-04-01), so the resolved range covers
    every seeded ESC regardless of wall-clock time at test runtime.
    The endpoint's ``to`` fallback is real ``datetime.now(UTC).date()``
    (no monkeypatch in integration suite) — assertions only require
    that ``to`` is on or after the seeded ESC date.
    """

    async def test_summary_no_params_returns_full_aggregate(
        self, cost_client: AsyncClient, cost_seed: dict[str, object]
    ) -> None:
        response = await cost_client.get(
            "/api/v1/cost/summary",
            params={"no_cache": "true"},
        )
        assert response.status_code == 200
        data = response.json()
        # Resolved bounds reach the response body via Pydantic alias.
        assert data["from"] == str(TENANT_CREATED_AT.date())
        assert data["to"] >= str(SAMPLE_TIMESTAMP.date())
        # Aggregate matches the explicit-dates baseline (KD-F: same
        # logical period regardless of how it was specified).
        assert data["total_usd"] == pytest.approx(cost_seed["expected_total"])
        assert data["unattributed_cost_usd"] == pytest.approx(
            cost_seed["expected_unattributed"]
        )

    async def test_course_no_params_returns_full_subtree(
        self, cost_client: AsyncClient, cost_seed: dict[str, object]
    ) -> None:
        response = await cost_client.get(
            f"/api/v1/cost/course/{cost_seed['root_id']}",
            params={"no_cache": "true"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["from"] == str(TENANT_CREATED_AT.date())
        assert data["to"] >= str(SAMPLE_TIMESTAMP.date())
        # Subtree total = root (85) + lesson (30) + concept (25) = 140.
        assert data["total_usd"] == pytest.approx(140.0)
        node_ids = {n["course_node_id"] for n in data["by_node"]}
        assert node_ids == {
            str(cost_seed["root_id"]),
            str(cost_seed["lesson_id"]),
            str(cost_seed["concept_id"]),
        }
