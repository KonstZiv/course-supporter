"""Tests for enqueue_ingestion (post-C9.3; other enqueue helpers removed)."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from course_supporter.enqueue import (
    enqueue_ingestion,
    enqueue_node_summary_regeneration,
)
from course_supporter.job_priority import JobPriority


def _mock_session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    return session


def _mock_redis(arq_job_id: str = "arq:test:123") -> AsyncMock:
    redis = AsyncMock()
    arq_job = MagicMock()
    arq_job.job_id = arq_job_id
    redis.enqueue_job = AsyncMock(return_value=arq_job)
    return redis


def _mock_job(job_id: uuid.UUID | None = None) -> MagicMock:
    job = MagicMock()
    job.id = job_id or uuid.uuid4()
    return job


class TestEnqueueIngestion:
    async def test_creates_job_and_enqueues(self) -> None:
        """enqueue_ingestion creates a Job record and enqueues to ARQ."""
        session = _mock_session()
        redis = _mock_redis()
        mock_job = _mock_job()
        tenant_id = uuid.uuid4()
        node_id = uuid.uuid4()
        material_id = uuid.uuid4()

        with patch("course_supporter.enqueue.JobRepository") as repo_cls:
            repo_cls.return_value.create = AsyncMock(return_value=mock_job)
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            result = await enqueue_ingestion(
                redis=redis,
                session=session,
                tenant_id=tenant_id,
                node_id=node_id,
                material_id=material_id,
                source_type="web",
                source_url="https://example.com",
            )

        assert result is mock_job
        repo_cls.return_value.create.assert_awaited_once()
        create_kwargs = repo_cls.return_value.create.call_args.kwargs
        assert create_kwargs["job_type"] == "ingest"
        assert create_kwargs["tenant_id"] == tenant_id
        assert create_kwargs["course_node_id"] == node_id
        assert create_kwargs["priority"] == "normal"

    async def test_enqueues_with_correct_args(self) -> None:
        """ARQ enqueue_job receives correct positional args."""
        session = _mock_session()
        redis = _mock_redis()
        mock_job = _mock_job()
        material_id = uuid.uuid4()

        with patch("course_supporter.enqueue.JobRepository") as repo_cls:
            repo_cls.return_value.create = AsyncMock(return_value=mock_job)
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            await enqueue_ingestion(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                node_id=uuid.uuid4(),
                material_id=material_id,
                source_type="video",
                source_url="s3://bucket/key",
                priority=JobPriority.IMMEDIATE,
            )

        redis.enqueue_job.assert_awaited_once_with(
            "arq_ingest_material",
            str(mock_job.id),
            str(material_id),
            "video",
            "s3://bucket/key",
            "immediate",
        )

    async def test_sets_arq_job_id(self) -> None:
        """Job.arq_job_id is updated after enqueue."""
        session = _mock_session()
        redis = _mock_redis(arq_job_id="arq:abc:456")
        mock_job = _mock_job()

        with patch("course_supporter.enqueue.JobRepository") as repo_cls:
            repo_cls.return_value.create = AsyncMock(return_value=mock_job)
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            await enqueue_ingestion(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                node_id=uuid.uuid4(),
                material_id=uuid.uuid4(),
                source_type="text",
                source_url="https://example.com/doc",
            )

        repo_cls.return_value.set_arq_job_id.assert_awaited_once_with(
            mock_job.id, "arq:abc:456"
        )

    async def test_handles_none_arq_job(self) -> None:
        """Handles case where enqueue_job returns None (duplicate)."""
        session = _mock_session()
        redis = AsyncMock()
        redis.enqueue_job = AsyncMock(return_value=None)
        mock_job = _mock_job()

        with patch("course_supporter.enqueue.JobRepository") as repo_cls:
            repo_cls.return_value.create = AsyncMock(return_value=mock_job)
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            result = await enqueue_ingestion(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                node_id=uuid.uuid4(),
                material_id=uuid.uuid4(),
                source_type="web",
                source_url="https://example.com",
            )

        assert result is mock_job
        repo_cls.return_value.set_arq_job_id.assert_not_awaited()

    async def test_immediate_priority(self) -> None:
        """IMMEDIATE priority is passed correctly to Job and ARQ."""
        session = _mock_session()
        redis = _mock_redis()
        mock_job = _mock_job()

        with patch("course_supporter.enqueue.JobRepository") as repo_cls:
            repo_cls.return_value.create = AsyncMock(return_value=mock_job)
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            await enqueue_ingestion(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                node_id=uuid.uuid4(),
                material_id=uuid.uuid4(),
                source_type="web",
                source_url="https://example.com",
                priority=JobPriority.IMMEDIATE,
            )

        create_kwargs = repo_cls.return_value.create.call_args.kwargs
        assert create_kwargs["priority"] == "immediate"

    async def test_durable_commit_precedes_arq_dispatch(self) -> None:
        """QQ5 (Task 3.2.6 Finding 1): the durable Job commit happens BEFORE
        the ARQ dispatch — a hot worker can never read a not-yet-committed
        Job. The helper owns the commit (mirrors enqueue_s3_cleanup).
        """
        session = _mock_session()
        redis = _mock_redis()
        mock_job = _mock_job()
        events: list[str] = []
        session.commit = AsyncMock(side_effect=lambda: events.append("commit"))

        arq_job = MagicMock()
        arq_job.job_id = "arq:test:1"

        async def _enqueue(*_args: object, **_kwargs: object) -> MagicMock:
            events.append("enqueue")
            return arq_job

        redis.enqueue_job = AsyncMock(side_effect=_enqueue)

        with patch("course_supporter.enqueue.JobRepository") as repo_cls:
            repo_cls.return_value.create = AsyncMock(return_value=mock_job)
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            await enqueue_ingestion(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                node_id=uuid.uuid4(),
                material_id=uuid.uuid4(),
                source_type="text",
                source_url="https://example.com/doc",
            )

        # Lock the FULL QQ5 flow: durable commit → ARQ dispatch → second
        # commit for arq_job_id. Asserting the exact sequence (not just
        # commit-before-enqueue) also locks the post-dispatch commit.
        assert events == ["commit", "enqueue", "commit"]

    async def test_none_arq_leaves_durable_job_without_second_commit(self) -> None:
        """Inverse failure mode (operator awareness note): if the durable
        commit succeeds but ARQ dispatch returns None (Redis down /
        duplicate-id), the Job stays durably committed without ``arq_job_id``
        and there is NO second commit — an orphan-queued Job that is
        reactivate-eligible. Strictly better than the old orphan-ARQ-task
        outcome (hard "Job not found" crashing the worker).
        """
        session = _mock_session()
        commit_count = 0

        async def _commit() -> None:
            nonlocal commit_count
            commit_count += 1

        session.commit = AsyncMock(side_effect=_commit)
        redis = AsyncMock()
        redis.enqueue_job = AsyncMock(return_value=None)
        mock_job = _mock_job()

        with patch("course_supporter.enqueue.JobRepository") as repo_cls:
            repo_cls.return_value.create = AsyncMock(return_value=mock_job)
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            result = await enqueue_ingestion(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                node_id=uuid.uuid4(),
                material_id=uuid.uuid4(),
                source_type="web",
                source_url="https://example.com",
            )

        assert result is mock_job
        repo_cls.return_value.set_arq_job_id.assert_not_awaited()
        # Only the durable commit — no second (arq_job_id) commit.
        assert commit_count == 1


class TestEnqueueNodeSummaryRegeneration:
    """Finding 1 covers the methodist enqueue path too (Ratified #5)."""

    async def test_durable_commit_precedes_arq_dispatch(self) -> None:
        """Methodist regeneration: durable Job commit strictly before the
        ARQ dispatch (same QQ5 helper-owns-commit ordering).
        """
        session = _mock_session()
        mock_job = _mock_job()
        events: list[str] = []
        session.commit = AsyncMock(side_effect=lambda: events.append("commit"))

        arq_job = MagicMock()
        arq_job.job_id = "arq:test:regen"
        redis = AsyncMock()

        async def _enqueue(*_args: object, **_kwargs: object) -> MagicMock:
            events.append("enqueue")
            return arq_job

        redis.enqueue_job = AsyncMock(side_effect=_enqueue)

        with patch("course_supporter.enqueue.JobRepository") as repo_cls:
            repo_cls.return_value.create = AsyncMock(return_value=mock_job)
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            await enqueue_node_summary_regeneration(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                vertex_node_id=uuid.uuid4(),
                force=False,
            )

        # Full QQ5 sequence (same as the ingestion path).
        assert events == ["commit", "enqueue", "commit"]
        redis.enqueue_job.assert_awaited_once()
        regen_args = redis.enqueue_job.call_args.args
        assert regen_args[0] == "arq_regenerate_node_summary"
