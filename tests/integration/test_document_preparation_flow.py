"""№21 BE8 — DOCUMENT_PREPARATION visibility on a REAL document (not the derive
matrix). A code doc's prep must be a visible in-flight phase (decision 11): the
enqueue binds the pending receipt (queued/processing), a failed prep surfaces as
ERROR, and a completed prep with no author decision rests at ``awaiting_author``.

Requires ``docker compose up -d`` (PostgreSQL + Redis).
Run with: ``uv run pytest tests/integration/test_document_preparation_flow.py \
--run-db --run-redis -v``
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.tasks import _prepare_on_terminal
from course_supporter.enqueue import enqueue_document_preparation
from course_supporter.jobs.execution_seam import SeamTerminal
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import Job

pytestmark = [pytest.mark.requires_db, pytest.mark.requires_redis]

_PROPOSAL = {
    "files": {"src/app.py": {"role": "full", "reason": "custom_source"}},
    "tree_digest": "digest-1",
    "computed_at": "2026-07-23T00:00:00+00:00",
}


async def _phase_of(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    node_id: uuid.UUID,
    material_id: uuid.UUID,
) -> str:
    """Read the derived ``processing_phase`` on the REAL document, via the
    pending-job-eager-loading list loader (Рат.6 — no lazy load)."""
    async with session_factory() as session:
        docs = await AuthoredDocumentRepository(session).get_for_node(node_id)
    doc = next(d for d in docs if d.id == material_id)
    return doc.processing_phase


async def _seed_prep_job(
    session_factory: async_sessionmaker[AsyncSession],
    seeds: dict[str, uuid.UUID],
    *,
    status: str,
) -> uuid.UUID:
    """Create a DOCUMENT_PREPARATION Job at ``status`` bound to the material
    (``set_pending``), mirroring enqueue_document_preparation's receipt. Sets
    ``course_node_id`` so committed_seeds' teardown reaps it."""
    async with session_factory() as session:
        job_repo = JobRepository(session)
        entry_repo = AuthoredDocumentRepository(session)
        job = await job_repo.create(
            tenant_id=seeds["tenant_id"],
            course_node_id=seeds["course_node_id"],
            job_type="document_preparation",
            subject_type="authored_document",
            subject_id=seeds["material_id"],
        )
        await entry_repo.set_pending(seeds["material_id"], job.id)
        if status != "queued":
            await job_repo.update_status(job.id, "active")
        if status not in {"queued", "active"}:
            await job_repo.update_status(job.id, status)
        await session.commit()
        return job.id


class TestPreparationVisibility:
    """The prep receipt is visible on the real document (decision 11 / BE4)."""

    async def test_enqueue_binds_pending_receipt(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
        arq_redis: ArqRedis,
    ) -> None:
        """After enqueue_document_preparation the document carries the prep job as
        its pending receipt — a live prep reads as ``queued`` (not ``ready``)."""
        async with session_factory() as session:
            job = await enqueue_document_preparation(
                redis=arq_redis,
                session=session,
                tenant_id=committed_seeds["tenant_id"],
                authored_document_id=committed_seeds["material_id"],
            )
            prep_job_id = job.id

        async with session_factory() as session:
            doc = await AuthoredDocumentRepository(session).get_by_id(
                committed_seeds["material_id"]
            )
            assert doc is not None
            assert doc.job_id == prep_job_id

        phase = await _phase_of(
            session_factory,
            node_id=committed_seeds["course_node_id"],
            material_id=committed_seeds["material_id"],
        )
        assert phase == "queued"

        # enqueue_document_preparation does not stamp course_node_id on the Job
        # (orthogonal to this task), so committed_seeds' teardown misses it —
        # reap it here to keep the tenant delete FK-clean.
        async with session_factory() as session:
            await session.execute(Job.__table__.delete().where(Job.id == prep_job_id))
            await session.commit()

    async def test_failed_prep_surfaces_error(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
    ) -> None:
        """A failed prep is a VISIBLE error on the document (decision 11), not a
        silent forever-``ready``: _prepare_on_terminal clears the receipt + sets
        the error, so the derived phase is ``error``."""
        job_id = await _seed_prep_job(session_factory, committed_seeds, status="failed")
        ctx = {"session_factory": session_factory, "redis": MagicMock()}

        await _prepare_on_terminal(
            ctx,
            str(job_id),
            str(committed_seeds["material_id"]),
            outcome=SeamTerminal(status="failed", error_message="prep boom"),
        )

        async with session_factory() as session:
            doc = await AuthoredDocumentRepository(session).get_by_id(
                committed_seeds["material_id"]
            )
        assert doc is not None
        assert doc.job_id is None  # fail_processing cleared the receipt
        assert doc.error_message == "prep boom"
        assert doc.processing_phase == "error"

    async def test_complete_no_decision_releases_receipt_awaiting_author(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
    ) -> None:
        """A completed prep with a proposal but no covering decision RELEASES the
        prep receipt and rests at the designed awaiting_author pair (decision 10):
        job_id=None → state=ready, processing_phase=awaiting_author."""
        job_id = await _seed_prep_job(
            session_factory, committed_seeds, status="complete"
        )
        async with session_factory() as session:
            await AuthoredDocumentRepository(session).store_file_roles_proposal(
                committed_seeds["material_id"], proposal=_PROPOSAL
            )
            await session.commit()

        ctx = {"session_factory": session_factory, "redis": MagicMock()}
        await _prepare_on_terminal(
            ctx,
            str(job_id),
            str(committed_seeds["material_id"]),
            outcome=SeamTerminal(status="complete"),
        )

        async with session_factory() as session:
            doc = await AuthoredDocumentRepository(session).get_by_id(
                committed_seeds["material_id"]
            )
        assert doc is not None
        # The receipt is released — the designed pair UI2 was built for.
        assert doc.job_id is None
        assert doc.state == "ready"
        assert doc.processing_phase == "awaiting_author"

    async def test_complete_with_decision_chains_processing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
        arq_redis: ArqRedis,
    ) -> None:
        """A completed prep whose decision covers the tree chains
        DOCUMENT_PROCESSING (criterion 4): the prep receipt is OVERWRITTEN by the
        new processing Job (job_id moves off the prep job)."""
        prep_job_id = await _seed_prep_job(
            session_factory, committed_seeds, status="complete"
        )
        decision = {
            "files": {"src/app.py": "full"},
            "tree_digest": _PROPOSAL["tree_digest"],
            "decided_at": "2026-07-23T00:00:00+00:00",
        }
        async with session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            await repo.store_file_roles_proposal(
                committed_seeds["material_id"], proposal=_PROPOSAL
            )
            await repo.store_file_roles_decision(
                committed_seeds["material_id"], decision=decision
            )
            await session.commit()

        ctx = {"session_factory": session_factory, "redis": arq_redis}
        await _prepare_on_terminal(
            ctx,
            str(prep_job_id),
            str(committed_seeds["material_id"]),
            outcome=SeamTerminal(status="complete"),
        )

        async with session_factory() as session:
            doc = await AuthoredDocumentRepository(session).get_by_id(
                committed_seeds["material_id"]
            )
            assert doc is not None
            assert doc.job_id is not None
            assert doc.job_id != prep_job_id  # overwritten by the processing Job
            new_job = await JobRepository(session).get_by_id(doc.job_id)
        assert new_job is not None
        assert new_job.job_type == "document_processing"

    async def test_release_pending_noop_when_receipt_moved_on(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
    ) -> None:
        """Compare-and-clear: release_pending never clobbers a NEWER receipt — a
        stale prep-job id releases nothing while the document points elsewhere."""
        prep_job_id = await _seed_prep_job(
            session_factory, committed_seeds, status="active"
        )
        stale_job_id = uuid.uuid4()  # a different job that lost the race
        async with session_factory() as session:
            repo = AuthoredDocumentRepository(session)
            released = await repo.release_pending(
                committed_seeds["material_id"], expected_job_id=stale_job_id
            )
            await session.commit()
            doc = await repo.get_by_id(committed_seeds["material_id"])
        assert released is None  # no write
        assert doc is not None
        assert doc.job_id == prep_job_id  # the live receipt is untouched
