"""Unit tests for enqueue_base_normalize (KD18 P2, MagicMock — no DB/Redis).

Locks the Commit-3↔Commit-4 contract: the enqueue payload
``("base_normalize_task", job_id, project_base_id)``, the QQ5 commit→enqueue→
commit order (DD-3.2.6-A), and the no-swallow of a version collision (the
IntegrityError from create_version's flush propagates BEFORE any Job is created).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from course_supporter.enqueue import enqueue_base_normalize
from course_supporter.jobs import JobType


def _mock_session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    return session


def _mock_redis(arq_job_id: str = "arq:base:1") -> AsyncMock:
    redis = AsyncMock()
    arq_job = MagicMock()
    arq_job.job_id = arq_job_id
    redis.enqueue_job = AsyncMock(return_value=arq_job)
    return redis


def _mock_base(base_id: uuid.UUID | None = None, version: int = 1) -> MagicMock:
    base = MagicMock()
    base.id = base_id or uuid.uuid4()
    base.version = version
    return base


def _mock_job(job_id: uuid.UUID | None = None) -> MagicMock:
    job = MagicMock()
    job.id = job_id or uuid.uuid4()
    return job


class TestEnqueueBaseNormalize:
    async def test_payload_is_job_id_and_project_base_id(self) -> None:
        """Payload = ("base_normalize_task", job_id, project_base_id)."""
        session = _mock_session()
        redis = _mock_redis()
        base = _mock_base()
        job = _mock_job()

        with (
            patch("course_supporter.enqueue.ProjectBaseRepository") as pb_cls,
            patch("course_supporter.enqueue.JobRepository") as job_cls,
        ):
            pb_cls.return_value.create_version = AsyncMock(return_value=base)
            job_cls.return_value.create = AsyncMock(return_value=job)
            job_cls.return_value.set_arq_job_id = AsyncMock()

            result = await enqueue_base_normalize(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                authored_document_id=uuid.uuid4(),
                archive_key="tenants/t/nodes/n/bases/d/v1/original.zip",
            )

        assert result is base
        redis.enqueue_job.assert_awaited_once_with(
            "base_normalize_task",
            str(job.id),
            str(base.id),
        )
        # Job created as BASE_NORMALIZE carrying project_base_id (worker
        # re-reads archive_key from the ProjectBase by id).
        create_kwargs = job_cls.return_value.create.call_args.kwargs
        assert create_kwargs["job_type"] == JobType.BASE_NORMALIZE
        assert create_kwargs["input_params"] == {"project_base_id": str(base.id)}

    async def test_durable_commit_precedes_arq_dispatch(self) -> None:
        """QQ5 (DD-3.2.6-A): commit → enqueue → second commit (arq_job_id)."""
        session = _mock_session()
        redis = _mock_redis()
        base = _mock_base()
        job = _mock_job()
        events: list[str] = []
        session.commit = AsyncMock(side_effect=lambda: events.append("commit"))

        arq_job = MagicMock()
        arq_job.job_id = "arq:base:1"

        async def _enqueue(*_args: object, **_kwargs: object) -> MagicMock:
            events.append("enqueue")
            return arq_job

        redis.enqueue_job = AsyncMock(side_effect=_enqueue)

        with (
            patch("course_supporter.enqueue.ProjectBaseRepository") as pb_cls,
            patch("course_supporter.enqueue.JobRepository") as job_cls,
        ):
            pb_cls.return_value.create_version = AsyncMock(return_value=base)
            job_cls.return_value.create = AsyncMock(return_value=job)
            job_cls.return_value.set_arq_job_id = AsyncMock()

            await enqueue_base_normalize(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                authored_document_id=uuid.uuid4(),
                archive_key="k/v1/original.zip",
            )

        assert events == ["commit", "enqueue", "commit"]

    async def test_version_collision_propagates_before_job_created(self) -> None:
        """A racing re-upload's IntegrityError (from create_version's flush)
        propagates — NOT swallowed — and no Job is created / committed."""
        session = _mock_session()
        redis = _mock_redis()

        with (
            patch("course_supporter.enqueue.ProjectBaseRepository") as pb_cls,
            patch("course_supporter.enqueue.JobRepository") as job_cls,
        ):
            pb_cls.return_value.create_version = AsyncMock(
                side_effect=IntegrityError("INSERT ...", {}, Exception("dup version"))
            )
            job_cls.return_value.create = AsyncMock()
            job_cls.return_value.set_arq_job_id = AsyncMock()

            with pytest.raises(IntegrityError):
                await enqueue_base_normalize(
                    redis=redis,
                    session=session,
                    tenant_id=uuid.uuid4(),
                    authored_document_id=uuid.uuid4(),
                    archive_key="k/v1/original.zip",
                )

        job_cls.return_value.create.assert_not_awaited()
        session.commit.assert_not_awaited()
        redis.enqueue_job.assert_not_awaited()

    async def test_sets_arq_job_id_after_enqueue(self) -> None:
        session = _mock_session()
        redis = _mock_redis(arq_job_id="arq:base:xyz")
        base = _mock_base()
        job = _mock_job()

        with (
            patch("course_supporter.enqueue.ProjectBaseRepository") as pb_cls,
            patch("course_supporter.enqueue.JobRepository") as job_cls,
        ):
            pb_cls.return_value.create_version = AsyncMock(return_value=base)
            job_cls.return_value.create = AsyncMock(return_value=job)
            job_cls.return_value.set_arq_job_id = AsyncMock()

            await enqueue_base_normalize(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                authored_document_id=uuid.uuid4(),
                archive_key="k/v1/original.zip",
            )

        job_cls.return_value.set_arq_job_id.assert_awaited_once_with(
            job.id, "arq:base:xyz"
        )

    async def test_handles_none_arq_job(self) -> None:
        """enqueue_job None (Redis down / dup id): no second commit, no set_arq."""
        session = _mock_session()
        commit_count = 0

        async def _commit() -> None:
            nonlocal commit_count
            commit_count += 1

        session.commit = AsyncMock(side_effect=_commit)
        redis = AsyncMock()
        redis.enqueue_job = AsyncMock(return_value=None)
        base = _mock_base()
        job = _mock_job()

        with (
            patch("course_supporter.enqueue.ProjectBaseRepository") as pb_cls,
            patch("course_supporter.enqueue.JobRepository") as job_cls,
        ):
            pb_cls.return_value.create_version = AsyncMock(return_value=base)
            job_cls.return_value.create = AsyncMock(return_value=job)
            job_cls.return_value.set_arq_job_id = AsyncMock()

            result = await enqueue_base_normalize(
                redis=redis,
                session=session,
                tenant_id=uuid.uuid4(),
                authored_document_id=uuid.uuid4(),
                archive_key="k/v1/original.zip",
            )

        assert result is base
        job_cls.return_value.set_arq_job_id.assert_not_awaited()
        assert commit_count == 1
