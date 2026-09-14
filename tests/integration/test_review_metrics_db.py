"""Acceptance 4 (mentor-rebuild task 01): metrics on their own register row.

Synthetic claims → the default calculator → the writer → a real row. There is
no live review structure before task 04, so the claims are hand-built; the
path from them to the database is the real one.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.review_metrics import (
    REVIEW_METRICS_ACTION,
    Claim,
    ShareMetricsCalculator,
)
from course_supporter.service_logging import job_scope, record_review_metrics
from course_supporter.storage.orm import ExternalServiceCall, Job

pytestmark = pytest.mark.requires_db


@pytest.fixture()
async def committed_job_id(
    session_factory: async_sessionmaker[AsyncSession],
    committed_seeds: dict[str, uuid.UUID],
) -> AsyncGenerator[uuid.UUID]:
    async with session_factory() as session:
        job = Job(
            tenant_id=committed_seeds["tenant_id"],
            course_node_id=committed_seeds["course_node_id"],
            job_type="document_processing",
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    yield job_id

    async with session_factory() as session:
        await session.execute(
            ExternalServiceCall.__table__.delete().where(
                ExternalServiceCall.job_id == job_id
            )
        )
        await session.execute(Job.__table__.delete().where(Job.id == job_id))
        await session.commit()


async def _metrics_rows(
    session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> list[ExternalServiceCall]:
    async with session_factory() as session:
        result = await session.execute(
            select(ExternalServiceCall).where(ExternalServiceCall.job_id == job_id)
        )
        return list(result.scalars())


class TestMetricsRow:
    async def test_calculated_metrics_land_on_their_own_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job_id: uuid.UUID,
    ) -> None:
        claims = [
            Claim(supported_by_reference=True, covered_by_verdict=True),
            Claim(supported_by_reference=True, covered_by_verdict=False),
            Claim(supported_by_reference=False, covered_by_verdict=True),
            Claim(supported_by_reference=True, covered_by_verdict=True),
        ]
        metrics = ShareMetricsCalculator().calculate(claims)

        with job_scope(committed_job_id):
            await record_review_metrics(session_factory, metrics)

        (row,) = await _metrics_rows(session_factory, committed_job_id)
        assert row.action == REVIEW_METRICS_ACTION
        assert row.authenticity == pytest.approx(0.75)
        assert row.completeness == pytest.approx(0.75)
        # Same table as cost, tokens and latency — but no call behind it.
        assert (row.provider, row.model_id, row.success, row.outcome) == (
            None,
            None,
            None,
            None,
        )
        assert row.cost_usd is None

    async def test_empty_review_stores_empty_metrics_not_zeros(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job_id: uuid.UUID,
    ) -> None:
        with job_scope(committed_job_id):
            await record_review_metrics(
                session_factory, ShareMetricsCalculator().calculate([])
            )

        (row,) = await _metrics_rows(session_factory, committed_job_id)
        assert (row.authenticity, row.completeness) == (None, None)

    async def test_metrics_row_is_outside_the_failure_count(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_job_id: uuid.UUID,
    ) -> None:
        with job_scope(committed_job_id):
            await record_review_metrics(
                session_factory,
                ShareMetricsCalculator().calculate(
                    [Claim(supported_by_reference=False, covered_by_verdict=False)]
                ),
            )

        async with session_factory() as session:
            counts = (
                await session.execute(
                    text(
                        "SELECT count(*) FILTER (WHERE success IS NULL), "
                        "count(*) FILTER "
                        "(WHERE NOT success OR error_message IS NOT NULL) "
                        "FROM external_service_calls WHERE job_id = :job"
                    ),
                    {"job": committed_job_id},
                )
            ).one()
        # Premise first: the row really has a NULL success, then the count.
        assert tuple(counts) == (1, 0)
