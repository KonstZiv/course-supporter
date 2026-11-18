"""Shadow estimate of a node-summary regeneration, against a real database.

Run with ``uv run pytest --run-db`` (PostgreSQL at the configured URL).

* the reader (``cost_estimate.node_summary``) sizes a bottom-up call as the
  methodist agent reads it, and finds the stale nodes as the orchestrator does;
* the hook writes one row per job with the right key and kind, never raises,
  and writes once;
* the calibration report sees the new rows beside the call register.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from scripts.cost_estimate_report import _load
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.agents.methodist import (
    FULL_TEXT_BUDGET_CHARS,
    MethodistAgent,
    build_document_blocks,
)
from course_supporter.call_outcome import CallOutcome, RequestKind
from course_supporter.cost_estimate import hooks
from course_supporter.cost_estimate import node_summary as reader
from course_supporter.cost_estimate.settings import get_estimate_settings
from course_supporter.service_logging import tenant_scope
from course_supporter.storage.enclosing_context_hash import (
    EnclosingContextHashService,
)
from course_supporter.storage.node_summary_orchestrator import (
    NodeSummaryGenerationOrchestrator,
)
from course_supporter.storage.orm import (
    ActionCostEstimate,
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
    ExternalServiceCall,
    Job,
    NodeSummaryRaw,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_CHILD_CHARS = get_estimate_settings().processing.node_summary.child_summary_chars


# ── Seeding ──


async def _node(
    session: AsyncSession, tenant: Tenant, parent: CourseNode | None, title: str
) -> CourseNode:
    node = (
        make_root_course_node(tenant_id=tenant.id, title=title, order=0)
        if parent is None
        else CourseNode(tenant_id=tenant.id, parent_id=parent.id, title=title, order=0)
    )
    session.add(node)
    await session.flush()
    return node


async def _document(
    session: AsyncSession,
    node: CourseNode,
    root: CourseNode,
    *,
    title: str,
    role: str = "educational",
    task_type: str | None = None,
    order: int = 0,
    segments: tuple[str, ...] = (),
) -> None:
    doc = AuthoredDocument(
        course_node_id=node.id,
        course_root_id=root.id,
        source_type="text",
        source_url=f"https://example.com/{uuid.uuid4().hex}",
        material_role=role,
        task_type=task_type,
        order=order,
    )
    session.add(doc)
    await session.flush()
    summary = DocumentSummary(
        authored_document_id=doc.id,
        course_root_id=root.id,
        title=title,
        description=f"Опис: {title}.",
        main_concepts=[f"{title}-головне"],
        secondary_concepts=[f"{title}-інше"],
        content_char_count=sum(len(s) for s in segments),
    )
    session.add(summary)
    await session.flush()
    start = 0
    for i, text in enumerate(segments):
        session.add(
            DocumentSegment(
                document_summary_id=summary.id,
                course_root_id=root.id,
                order=i,
                start_pos=start,
                end_pos=start + len(text),
                content=text,
                main_concepts=[],
                secondary_concepts=[],
            )
        )
        start += len(text)
    await session.flush()


def _raw(
    node: CourseNode, *, fresh: bool = True, summary: str | None = "Підсумок."
) -> NodeSummaryRaw:
    return NodeSummaryRaw(
        course_node_id=node.id,
        source_content_hash=node.content_hash if fresh else "f" * 64,
        compressed_summary=summary,
    )


async def _chars_the_agent_reads(session: AsyncSession, node: CourseNode) -> int:
    """What the methodist agent puts in front of the model, as the reader counts."""
    agent = MethodistAgent(session=session, stage_router=MagicMock())
    docs = await agent._fetch_own_documents(node.id)
    blocks = build_document_blocks(docs, budget=FULL_TEXT_BUDGET_CHARS)
    entries = [*blocks.methodological, *blocks.tasks, *blocks.educational]
    children = await agent._fetch_children_raws(node.id)
    return (
        sum(len(e["full_text"] or "") for e in entries)
        + sum(
            len(e["title"])
            + len(e["description"])
            + sum(len(c) for c in e["main_concepts"])
            + sum(len(c) for c in e["secondary_concepts"])
            for e in entries
        )
        + sum(len(c["title"]) + len(c["compressed_summary"]) for c in children)
    )


# ── The reader ──


class TestBottomupSize:
    async def test_the_size_is_what_the_agent_reads(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        root = await _node(db_session, seed_tenant, None, "Курс")
        lesson = await _node(db_session, seed_tenant, root, "Урок")
        leaf = await _node(db_session, seed_tenant, lesson, "Вправа")
        # a lesson: a methodological note in two segments, a task, a lecture
        await _document(
            db_session, lesson, root, title="методичка", role="methodological",
            order=2, segments=("Перша частина, ", "друга частина."),
        )  # fmt: skip
        await _document(
            db_session, lesson, root, title="завдання", task_type="task",
            order=1, segments=("Умова завдання.",),
        )  # fmt: skip
        await _document(
            db_session, lesson, root, title="конспект", order=0,
            segments=("Текст лекції, якого немає в запиті.",),
        )  # fmt: skip
        db_session.add_all(
            [
                _raw(leaf, summary="Короткий підсумок вправи."),
                _raw(lesson, fresh=False),
            ]
        )
        await db_session.flush()

        factors = await reader.read_node_summary_factors(
            db_session, root.id, child_summary_chars=_CHILD_CHARS
        )

        # stale: the lesson (stale Raw) and the course root (no Raw); the leaf is fresh
        assert factors.nodes == 3
        assert len(factors.bottomup_chars) == 2
        assert (
            await _chars_the_agent_reads(db_session, lesson) in factors.bottomup_chars
        )

    async def test_a_child_without_a_summary_is_read_at_the_settings_size(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """A stale child with no ``compressed_summary`` yet will have one by the
        time its parent is visited (leaf to root): it is counted at the size the
        bottom-up prompt asks for."""
        root = await _node(db_session, seed_tenant, None, "Курс")
        child = await _node(db_session, seed_tenant, root, "Тема")

        factors = await reader.read_node_summary_factors(
            db_session, root.id, child_summary_chars=1234
        )

        # both stale; the child reads nothing but its (empty) self, the root reads
        # the child's title and the settings size
        assert sorted(factors.bottomup_chars) == [0, len(child.title) + 1234]

    async def test_fresh_nodes_cost_nothing(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        root = await _node(db_session, seed_tenant, None, "Курс")
        db_session.add(_raw(root))
        await db_session.flush()

        factors = await reader.read_node_summary_factors(
            db_session, root.id, child_summary_chars=_CHILD_CHARS
        )

        assert (factors.bottomup_chars, factors.topdown_nodes, factors.nodes) == (
            (),
            0,
            1,
        )

    async def test_a_document_that_is_not_ready_is_left_out(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        root = await _node(db_session, seed_tenant, None, "Курс")
        await _document(
            db_session, root, root, title="зламана", role="methodological",
            segments=("Не читається.",),
        )  # fmt: skip
        failed = (
            await db_session.execute(
                select(AuthoredDocument).where(
                    AuthoredDocument.course_node_id == root.id
                )
            )
        ).scalar_one()
        failed.error_message = "boom"
        await db_session.flush()

        factors = await reader.read_node_summary_factors(
            db_session, root.id, child_summary_chars=_CHILD_CHARS
        )

        assert factors.bottomup_chars == (0,)


class TestFreshnessIsTheOrchestrators:
    @pytest.mark.parametrize("case", ["no_raw", "stale", "fresh"])
    async def test_axis_one(
        self, db_session: AsyncSession, seed_tenant: Tenant, case: str
    ) -> None:
        node = await _node(db_session, seed_tenant, None, "Курс")
        raw = {
            "no_raw": None,
            "stale": _raw(node, fresh=False),
            "fresh": _raw(node),
        }[case]
        assert reader._is_axis1_fresh(
            node, raw
        ) == NodeSummaryGenerationOrchestrator._is_axis1_fresh(node, raw)


class TestTopdownNodes:
    """The second axis: who calls the top-down model (the module's rule)."""

    async def _course(
        self, session: AsyncSession, tenant: Tenant
    ) -> tuple[CourseNode, CourseNode, CourseNode, CourseNode]:
        root = await _node(session, tenant, None, "Курс")
        a = await _node(session, tenant, root, "A")
        a1 = await _node(session, tenant, a, "A1")
        b = await _node(session, tenant, root, "B")
        return root, a, a1, b

    async def _settle(
        self, session: AsyncSession, nodes: list[CourseNode], *, except_: set[uuid.UUID]
    ) -> None:
        """Raws whose second-axis key is current, but for the ``except_`` nodes."""
        for node in nodes:
            raw = _raw(node)
            session.add(raw)
            await session.flush()
            if node.parent_id is not None and node.id not in except_:
                await EnclosingContextHashService(session).update_source_hash(
                    raw,
                    await EnclosingContextHashService(session).compute_source_hash_for(
                        raw
                    ),
                )

    async def test_only_the_stale_node_when_nothing_below_it(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        root, a, a1, b = await self._course(db_session, seed_tenant)
        await self._settle(db_session, [root, a, a1, b], except_={a1.id})

        factors = await reader.read_node_summary_factors(
            db_session, root.id, child_summary_chars=_CHILD_CHARS
        )

        assert factors.topdown_nodes == 1  # a1

    async def test_the_orchestrator_visits_the_same_node(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        """Run the real Pass 2 with a recording hook over the same course."""
        root, a, a1, b = await self._course(db_session, seed_tenant)
        await self._settle(db_session, [root, a, a1, b], except_={a1.id})

        class Recording:
            def __init__(self) -> None:
                self.topdown: list[uuid.UUID] = []

            async def generate_bottomup(self, *_: Any) -> None:
                return None

            async def generate_topdown(self, node: CourseNode, *_: Any) -> None:
                self.topdown.append(node.id)

        recording = Recording()
        job = Job(
            tenant_id=seed_tenant.id,
            course_node_id=root.id,
            job_type="node_summary_regeneration",
            status="active",
            subject_type="course_node",
            subject_id=root.id,
        )
        db_session.add(job)
        await db_session.flush()
        await NodeSummaryGenerationOrchestrator(
            db_session,
            methodist=recording,
        ).run(job_id=job.id, vertex_node_id=root.id)

        factors = await reader.read_node_summary_factors(
            db_session, root.id, child_summary_chars=_CHILD_CHARS
        )
        # (the run settled a1 itself, so the reader sees nothing left — the count
        # taken before the run is the one to compare)
        assert recording.topdown == [a1.id]
        assert factors.topdown_nodes == 0

    async def test_a_rewritten_enclosing_context_reaches_every_node_below(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        root, a, a1, b = await self._course(db_session, seed_tenant)
        await self._settle(db_session, [root, a, a1, b], except_={a.id})

        factors = await reader.read_node_summary_factors(
            db_session, root.id, child_summary_chars=_CHILD_CHARS
        )

        assert factors.topdown_nodes == 2  # a, and a1 below it; not b

    async def test_a_node_without_a_raw_yet_calls_it_and_the_course_root_never(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        root, *_ = await self._course(db_session, seed_tenant)

        factors = await reader.read_node_summary_factors(
            db_session, root.id, child_summary_chars=_CHILD_CHARS
        )

        assert factors.topdown_nodes == 3  # a, a1, b — never the course root
        assert len(factors.bottomup_chars) == 4


# ── The hook ──


@pytest.fixture()
async def committed_course(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[Tenant, CourseNode, Job]]:
    """A tenant with a one-node course and its regeneration job; removed after."""
    async with session_factory() as session:
        tenant = Tenant(name=f"est-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        root = await _node(session, tenant, None, "Курс")
        await _document(
            session, root, root, title="методичка", role="methodological",
            segments=("Текст методички.",),
        )  # fmt: skip
        job = Job(
            tenant_id=tenant.id,
            course_node_id=root.id,
            job_type="node_summary_regeneration",
            status="active",
            subject_type="course_node",
            subject_id=root.id,
        )
        session.add(job)
        await session.commit()
    yield tenant, root, job
    async with session_factory() as session:
        await session.execute(
            delete(ActionCostEstimate).where(ActionCostEstimate.job_id == job.id)
        )
        await session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id == job.id)
        )
        await session.execute(delete(Job).where(Job.tenant_id == tenant.id))
        await session.execute(
            delete(DocumentSegment).where(DocumentSegment.course_root_id == root.id)
        )
        await session.execute(
            delete(DocumentSummary).where(DocumentSummary.course_root_id == root.id)
        )
        await session.execute(
            delete(AuthoredDocument).where(AuthoredDocument.course_root_id == root.id)
        )
        await session.execute(
            delete(NodeSummaryRaw).where(NodeSummaryRaw.course_node_id == root.id)
        )
        await session.execute(
            delete(CourseNode).where(CourseNode.tenant_id == tenant.id)
        )
        await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
        await session.commit()


async def _estimates(
    session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> list[ActionCostEstimate]:
    async with session_factory() as session:
        result = await session.execute(
            select(ActionCostEstimate).where(ActionCostEstimate.job_id == job_id)
        )
        return list(result.scalars())


class TestTheHook:
    async def test_one_processing_row_keyed_by_the_job(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_course: tuple[Tenant, CourseNode, Job],
    ) -> None:
        tenant, root, job = committed_course

        with tenant_scope(tenant.id):
            for _ in range(2):  # an ARQ retry of the job writes nothing more
                await hooks.shadow_node_summary_regeneration(
                    session_factory=session_factory,
                    vertex_node_id=root.id,
                    job_id=job.id,
                )

        [row] = await _estimates(session_factory, job.id)
        assert (row.action_kind, row.action_key) == ("processing", str(job.id))
        assert row.tenant_id == tenant.id
        assert row.submission_id is None
        assert row.uncalibrated is True
        # one stale node (the root, no Raw): one bottom-up call, no top-down
        assert [s["step"] for s in row.breakdown] == ["methodist_bottomup"]
        assert row.inputs["kind"] == "node_summary_regeneration"
        assert row.inputs["factors"]["bottomup_nodes"] == 1
        assert row.e_usd > Decimal(0)
        assert row.p_units is not None
        assert row.reserve_units is not None

    async def test_a_failed_read_does_not_fail_the_job(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_course: tuple[Tenant, CourseNode, Job],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, root, job = committed_course

        async def broken(*_: Any, **__: Any) -> Any:
            raise RuntimeError("broken on purpose")

        monkeypatch.setattr(hooks, "read_node_summary_factors", broken)

        await hooks.shadow_node_summary_regeneration(
            session_factory=session_factory, vertex_node_id=root.id, job_id=job.id
        )

        assert await _estimates(session_factory, job.id) == []

    async def test_a_failed_estimate_does_not_fail_the_job(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_course: tuple[Tenant, CourseNode, Job],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, root, job = committed_course

        def broken() -> Any:
            raise RuntimeError("no ladders on purpose")

        monkeypatch.setattr(hooks, "load_ladder_stages", broken)

        await hooks.shadow_node_summary_regeneration(
            session_factory=session_factory, vertex_node_id=root.id, job_id=job.id
        )

        assert await _estimates(session_factory, job.id) == []

    async def test_a_failed_write_does_not_fail_the_job(
        self,
        committed_course: tuple[Tenant, CourseNode, Job],
    ) -> None:
        _, root, job = committed_course

        def broken_factory() -> Any:
            raise RuntimeError("no database on purpose")

        await hooks.shadow_node_summary_regeneration(
            session_factory=broken_factory,  # type: ignore[arg-type]
            vertex_node_id=root.id,
            job_id=job.id,
        )


# ── The calibration report ──


class TestTheReportSeesIt:
    async def test_estimate_and_fact_side_by_side_by_job(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_course: tuple[Tenant, CourseNode, Job],
    ) -> None:
        tenant, root, job = committed_course
        with tenant_scope(tenant.id):
            await hooks.shadow_node_summary_regeneration(
                session_factory=session_factory,
                vertex_node_id=root.id,
                job_id=job.id,
            )
        async with session_factory() as session:
            session.add(
                ExternalServiceCall(
                    job_id=job.id,
                    action="methodist_bottomup",
                    provider="deepseek_thinking",
                    model_id="deepseek-v4-pro",
                    success=True,
                    cost_usd=Decimal("0.03"),
                    cost_kind="exact",
                    catalog_version="test-catalog",
                    tariff_window="flat",
                )
            )
            await session.commit()

        async with session_factory() as session:
            rows = [
                r
                for r in await _load(session, None, None)
                if r.action_key == str(job.id)
            ]

        [report] = rows
        assert report.action_kind == "processing"
        assert report.job_type == "node_summary_regeneration"
        assert report.as_csv()["job_type"] == "node_summary_regeneration"
        assert report.fact_usd == Decimal("0.03")
        assert report.calls == 1
        assert report.deviation_usd == report.fact_usd - report.e_usd
        assert set(report.step_deviation) == {"methodist_bottomup"}

    async def test_the_fact_is_the_payers_alone(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_course: tuple[Tenant, CourseNode, Job],
    ) -> None:
        """E is calibrated against the fact the journal charges (operator
        2026-11-17): retries and an infrastructure failure stay out."""
        tenant, root, job = committed_course
        with tenant_scope(tenant.id):
            await hooks.shadow_node_summary_regeneration(
                session_factory=session_factory,
                vertex_node_id=root.id,
                job_id=job.id,
            )
        async with session_factory() as session:
            for kind, outcome, usd in (
                (RequestKind.FIRST, CallOutcome.SUCCESS, "0.03"),
                (RequestKind.FIRST, CallOutcome.TRANSPORT_ERROR, "0.01"),
                (RequestKind.INFRA_RETRY, CallOutcome.SUCCESS, "0.02"),
                (RequestKind.STRUCTURAL_RETRY, CallOutcome.SUCCESS, "0.02"),
            ):
                session.add(
                    ExternalServiceCall(
                        job_id=job.id,
                        action="methodist_bottomup",
                        provider="deepseek_thinking",
                        model_id="deepseek-v4-pro",
                        success=outcome is CallOutcome.SUCCESS,
                        outcome=outcome.value,
                        request_kind=kind.value,
                        unit_type="tokens",
                        cost_usd=Decimal(usd),
                        cost_kind="estimated",
                    )
                )
            await session.commit()

        async with session_factory() as session:
            [report] = [
                r
                for r in await _load(session, None, None)
                if r.action_key == str(job.id)
            ]

        assert report.fact_usd == Decimal("0.03")
        assert report.calls == 1
