"""Tests for enqueue_ingestion and enqueue_generation helpers."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from course_supporter.enqueue import enqueue_generation, enqueue_ingestion
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
        course_id = uuid.uuid4()
        material_id = uuid.uuid4()

        with patch("course_supporter.enqueue.JobRepository") as repo_cls:
            repo_cls.return_value.create = AsyncMock(return_value=mock_job)
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            result = await enqueue_ingestion(
                redis=redis,
                session=session,
                course_id=course_id,
                material_id=material_id,
                source_type="web",
                source_url="https://example.com",
            )

        assert result is mock_job
        repo_cls.return_value.create.assert_awaited_once()
        create_kwargs = repo_cls.return_value.create.call_args.kwargs
        assert create_kwargs["job_type"] == "ingest"
        assert create_kwargs["course_id"] == course_id
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
                course_id=uuid.uuid4(),
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
                course_id=uuid.uuid4(),
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
                course_id=uuid.uuid4(),
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
                course_id=uuid.uuid4(),
                material_id=uuid.uuid4(),
                source_type="web",
                source_url="https://example.com",
                priority=JobPriority.IMMEDIATE,
            )

        create_kwargs = repo_cls.return_value.create.call_args.kwargs
        assert create_kwargs["priority"] == "immediate"


class TestEnqueueGeneration:
    async def test_creates_job_with_correct_type(self) -> None:
        """enqueue_generation creates Job with job_type='generate_structure'."""
        session = _mock_session()
        redis = _mock_redis()
        mock_job = _mock_job()
        course_id = uuid.uuid4()
        node_id = uuid.uuid4()
        deps = [str(uuid.uuid4()), str(uuid.uuid4())]

        with patch("course_supporter.enqueue.JobRepository") as repo_cls:
            repo_cls.return_value.create = AsyncMock(return_value=mock_job)
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            result = await enqueue_generation(
                redis=redis,
                session=session,
                course_id=course_id,
                node_id=node_id,
                mode="guided",
                depends_on=deps,
            )

        assert result is mock_job
        create_kwargs = repo_cls.return_value.create.call_args.kwargs
        assert create_kwargs["job_type"] == "generate_structure"
        assert create_kwargs["course_id"] == course_id
        assert create_kwargs["node_id"] == node_id
        assert create_kwargs["depends_on"] == deps
        assert create_kwargs["input_params"]["course_id"] == str(course_id)
        assert create_kwargs["input_params"]["node_id"] == str(node_id)
        assert create_kwargs["input_params"]["mode"] == "guided"

    async def test_enqueues_arq_with_correct_args(self) -> None:
        """ARQ enqueue_job receives correct args for generation."""
        session = _mock_session()
        redis = _mock_redis()
        mock_job = _mock_job()
        course_id = uuid.uuid4()
        node_id = uuid.uuid4()

        with patch("course_supporter.enqueue.JobRepository") as repo_cls:
            repo_cls.return_value.create = AsyncMock(return_value=mock_job)
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            await enqueue_generation(
                redis=redis,
                session=session,
                course_id=course_id,
                node_id=node_id,
                mode="free",
            )

        redis.enqueue_job.assert_awaited_once_with(
            "arq_generate_structure",
            str(mock_job.id),
            str(course_id),
            str(node_id),
            "free",
        )

    async def test_course_level_passes_none_node(self) -> None:
        """Course-level generation passes None for node_id."""
        session = _mock_session()
        redis = _mock_redis()
        mock_job = _mock_job()
        course_id = uuid.uuid4()

        with patch("course_supporter.enqueue.JobRepository") as repo_cls:
            repo_cls.return_value.create = AsyncMock(return_value=mock_job)
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            await enqueue_generation(
                redis=redis,
                session=session,
                course_id=course_id,
                node_id=None,
            )

        redis.enqueue_job.assert_awaited_once_with(
            "arq_generate_structure",
            str(mock_job.id),
            str(course_id),
            None,
            "free",
        )

        create_kwargs = repo_cls.return_value.create.call_args.kwargs
        assert create_kwargs["input_params"]["node_id"] is None

    async def test_sets_arq_job_id(self) -> None:
        """arq_job_id set on Job record after enqueue."""
        session = _mock_session()
        redis = _mock_redis(arq_job_id="arq:gen:789")
        mock_job = _mock_job()

        with patch("course_supporter.enqueue.JobRepository") as repo_cls:
            repo_cls.return_value.create = AsyncMock(return_value=mock_job)
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            await enqueue_generation(
                redis=redis,
                session=session,
                course_id=uuid.uuid4(),
            )

        repo_cls.return_value.set_arq_job_id.assert_awaited_once_with(
            mock_job.id, "arq:gen:789"
        )

    async def test_handles_none_arq_job(self) -> None:
        """When ARQ returns None (duplicate), set_arq_job_id is not called."""
        session = _mock_session()
        redis = AsyncMock()
        redis.enqueue_job = AsyncMock(return_value=None)
        mock_job = _mock_job()

        with patch("course_supporter.enqueue.JobRepository") as repo_cls:
            repo_cls.return_value.create = AsyncMock(return_value=mock_job)
            repo_cls.return_value.set_arq_job_id = AsyncMock()

            result = await enqueue_generation(
                redis=redis,
                session=session,
                course_id=uuid.uuid4(),
            )

        assert result is mock_job
        repo_cls.return_value.set_arq_job_id.assert_not_awaited()
