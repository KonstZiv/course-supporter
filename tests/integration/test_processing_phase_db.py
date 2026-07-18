"""Integration tests for L3 ``processing_phase`` (root cause #2) against a real
DB — the queued/processing/ready/error split reaching the author through the
tree loader, with a flat ``selectinload`` (Рат.6) so it costs no N+1 and never
trips a ``MissingGreenlet``.

Requires ``docker compose up -d`` (PostgreSQL).
Run: ``uv run pytest tests/integration/test_processing_phase_db.py --run-db -v``
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterable

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import AuthoredDocument, CourseNode, Job, Tenant
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = [pytest.mark.requires_db]


def _walk(roots: Iterable[CourseNode]) -> list[CourseNode]:
    out: list[CourseNode] = []
    stack = list(roots)
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.children)
    return out


async def _new_document(
    session: AsyncSession, *, node_id: uuid.UUID, url: str
) -> AuthoredDocument:
    doc = AuthoredDocument(
        course_node_id=node_id,
        course_root_id=node_id,
        source_type="web",
        source_url=url,
    )
    session.add(doc)
    await session.flush()
    return doc


async def _attach_job(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
    doc_id: uuid.UUID,
    status: str,
) -> uuid.UUID:
    """Create a Job in ``status`` and link it to the document (in-flight)."""
    job_repo = JobRepository(session)
    entry_repo = AuthoredDocumentRepository(session)
    job = await job_repo.create(
        tenant_id=tenant_id,
        course_node_id=node_id,
        job_type="document_processing",
    )
    if status != "queued":
        await job_repo.update_status(job.id, status)
    await entry_repo.set_pending(doc_id, job.id)
    return job.id


@pytest.fixture()
async def phase_scenario(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """A node with four documents spanning every phase.

    ``processing`` (job active) · ``queued`` (job queued) · ``ready`` (no job)
    · ``error`` (error_message, no job).
    """
    async with session_factory() as session:
        tenant = Tenant(name=f"l3-tenant-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = make_root_course_node(tenant_id=tenant.id, title="L3", order=0)
        session.add(node)
        await session.flush()

        d_proc = await _new_document(session, node_id=node.id, url="w://proc")
        d_queued = await _new_document(session, node_id=node.id, url="w://queued")
        d_ready = await _new_document(session, node_id=node.id, url="w://ready")
        d_error = await _new_document(session, node_id=node.id, url="w://error")

        await _attach_job(
            session,
            tenant_id=tenant.id,
            node_id=node.id,
            doc_id=d_proc.id,
            status="active",
        )
        await _attach_job(
            session,
            tenant_id=tenant.id,
            node_id=node.id,
            doc_id=d_queued.id,
            status="queued",
        )
        d_error.error_message = "boom"
        await session.commit()

        ids = {
            "tenant_id": tenant.id,
            "node_id": node.id,
            "proc": d_proc.id,
            "queued": d_queued.id,
            "ready": d_ready.id,
            "error": d_error.id,
        }

    yield ids

    async with session_factory() as session:
        await session.execute(
            Job.__table__.delete().where(Job.course_node_id == ids["node_id"])
        )
        await session.execute(
            AuthoredDocument.__table__.delete().where(
                AuthoredDocument.course_node_id == ids["node_id"]
            )
        )
        await session.execute(
            CourseNode.__table__.delete().where(CourseNode.id == ids["node_id"])
        )
        await session.execute(
            Tenant.__table__.delete().where(Tenant.id == ids["tenant_id"])
        )
        await session.commit()


class TestProcessingPhaseGesture:
    """Acceptance #1: the split is real and consistent with ``state``."""

    async def test_tree_load_distinguishes_queued_from_processing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        phase_scenario: dict[str, uuid.UUID],
    ) -> None:
        async with session_factory() as session:
            roots = await CourseNodeRepository(
                session
            ).get_subtree_with_active_documents(phase_scenario["node_id"])
            docs = {d.id: d for n in _walk(roots) for d in n.documents}

            # queued vs processing are distinguished while both are in-flight.
            assert docs[phase_scenario["proc"]].processing_phase == "processing"
            assert docs[phase_scenario["queued"]].processing_phase == "queued"
            assert docs[phase_scenario["ready"]].processing_phase == "ready"
            assert docs[phase_scenario["error"]].processing_phase == "error"

            # Terminal phases mirror ``state`` verbatim; both in-flight docs
            # still collapse to the old ``pending`` on ``state`` (untouched).
            assert docs[phase_scenario["proc"]].state == "pending"
            assert docs[phase_scenario["queued"]].state == "pending"
            assert docs[phase_scenario["ready"]].state == "ready"
            assert docs[phase_scenario["error"]].state == "error"

    async def test_phase_becomes_ready_after_completion(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        phase_scenario: dict[str, uuid.UUID],
    ) -> None:
        # Complete the in-flight job the way the pipeline does: terminal the
        # Job and clear the document's job_id.
        async with session_factory() as session:
            entry_repo = AuthoredDocumentRepository(session)
            job_repo = JobRepository(session)
            proc = await entry_repo.get_by_id(phase_scenario["proc"])
            assert proc is not None and proc.job_id is not None
            await job_repo.update_status(proc.job_id, "complete")
            await entry_repo.complete_processing(phase_scenario["proc"])
            await session.commit()

        async with session_factory() as session:
            roots = await CourseNodeRepository(
                session
            ).get_subtree_with_active_documents(phase_scenario["node_id"])
            docs = {d.id: d for n in _walk(roots) for d in n.documents}
            assert docs[phase_scenario["proc"]].processing_phase == "ready"
            assert docs[phase_scenario["proc"]].state == "ready"


class TestProcessingPhaseNoNPlusOne:
    """Acceptance #3: flat ``selectinload`` — no MissingGreenlet, query count
    constant in the number of in-flight documents."""

    async def _seed_inflight_node(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        n_docs: int,
    ) -> dict[str, uuid.UUID]:
        async with session_factory() as session:
            tenant = Tenant(name=f"l3-n1-{uuid.uuid4().hex[:8]}")
            session.add(tenant)
            await session.flush()
            node = make_root_course_node(tenant_id=tenant.id, title="N1", order=0)
            session.add(node)
            await session.flush()
            for i in range(n_docs):
                doc = await _new_document(session, node_id=node.id, url=f"w://n1/{i}")
                await _attach_job(
                    session,
                    tenant_id=tenant.id,
                    node_id=node.id,
                    doc_id=doc.id,
                    status="active",
                )
            await session.commit()
        return {"tenant_id": tenant.id, "node_id": node.id}

    async def _cleanup(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ids: dict[str, uuid.UUID],
    ) -> None:
        async with session_factory() as session:
            await session.execute(
                Job.__table__.delete().where(Job.course_node_id == ids["node_id"])
            )
            await session.execute(
                AuthoredDocument.__table__.delete().where(
                    AuthoredDocument.course_node_id == ids["node_id"]
                )
            )
            await session.execute(
                CourseNode.__table__.delete().where(CourseNode.id == ids["node_id"])
            )
            await session.execute(
                Tenant.__table__.delete().where(Tenant.id == ids["tenant_id"])
            )
            await session.commit()

    async def _count_load_queries(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        async_engine: AsyncEngine,
        node_id: uuid.UUID,
    ) -> int:
        count = 0

        def _on_exec(conn, cursor, statement, params, context, executemany):  # type: ignore[no-untyped-def]
            nonlocal count
            count += 1

        sync_engine = async_engine.sync_engine
        event.listen(sync_engine, "before_cursor_execute", _on_exec)
        try:
            async with session_factory() as session:
                roots = await CourseNodeRepository(
                    session
                ).get_subtree_with_active_documents(node_id)
                # Force the property (reads pending_job) on EVERY document — a
                # per-row lazy load here would raise MissingGreenlet.
                phases = [d.processing_phase for n in _walk(roots) for d in n.documents]
            assert phases and all(p == "processing" for p in phases)
        finally:
            event.remove(sync_engine, "before_cursor_execute", _on_exec)
        return count

    async def test_query_count_constant_in_document_count(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        async_engine: AsyncEngine,
    ) -> None:
        one = await self._seed_inflight_node(session_factory, n_docs=1)
        many = await self._seed_inflight_node(session_factory, n_docs=6)
        try:
            q_one = await self._count_load_queries(
                session_factory, async_engine, one["node_id"]
            )
            q_many = await self._count_load_queries(
                session_factory, async_engine, many["node_id"]
            )
            # Equal counts prove the pending_job load is batched (selectinload),
            # not per-row (which would grow with N → N+1).
            assert q_one == q_many
            # And it stays a small constant (CTE + nodes + docs + jobs).
            assert q_many <= 5
        finally:
            await self._cleanup(session_factory, one)
            await self._cleanup(session_factory, many)
