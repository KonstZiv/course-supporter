"""One rule of the same work, end to end through the portal (task 13, lock "ye").

The product owner's decision of 2026-11-12: for a type the new path serves and
deduplicates (``task``, ``short_task``), the entry's old check — the same file
by its raw hash, answered "already submitted" with no new submission — is not
applied. A byte-identical file is a new submission; at the doors the dedup of
task 13 meets it. A type on today's Mentor (``project``) keeps the old check.

Through the portal's submission route over HTTP, with a live database, the S3
of the environment (SeaweedFS or moto at ``s3_endpoint``) and the real body of
the queue task; the queue is a double, the body is called with the job the
route created. No model is called: the first review is answered by the
recorded router of ``test_text_review_e2e_db``, the repeat by a router that
fails on any question. Run with ``--run-db --run-redis``.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.app import app
from course_supporter.api.deps import (
    get_arq_redis,
    get_current_student,
    get_s3_client,
    get_session,
)
from course_supporter.api.tasks import arq_process_homework
from course_supporter.auth.context import StudentContext
from course_supporter.config import get_settings
from course_supporter.homework.attempt_dedup import DEDUP_RECEIPT_KEY
from course_supporter.homework.evaluator_version import EVALUATOR_VERSION_RECEIPT_KEY
from course_supporter.models.review_structure import ReviewStructureV1
from course_supporter.storage.orm import (
    AuthoredDocument,
    DocumentSummary,
    HomeworkSubmission,
    Job,
    StudentEnrollment,
)
from course_supporter.storage.s3 import S3Client
from tests.integration.test_attempt_dedup_db import (
    _WORK,
    _NoModelRouter,
    _providers_watched,
    _register_rows,
)
from tests.integration.test_attempt_ledger_db import _drop_world, _make_world
from tests.integration.test_text_review_e2e_db import (
    _ListSource,
    _PortDouble,
    _RouterDouble,
    _shipped,
    _webhook,
)

pytestmark = pytest.mark.requires_db


def _archive() -> bytes:
    """The student's work as a zip archive; the same bytes every time it is read."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("solution.py", date_time=(2026, 11, 12, 9, 0, 0))
        archive.writestr(info, _WORK.encode("utf-8"))
    return buffer.getvalue()


_ARCHIVE = _archive()


# ── The world: a task and a project, ready; an enrolled student ─────────────


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, Any]]:
    """The ``task`` world of the ledger's tests, made submittable from the portal.

    Its task gets a ready summary (the route's readiness gate); a ``project``
    task beside it, ready and without a base; the student enrolled.
    """
    world = await _make_world(session_factory, task_type="task")
    async with session_factory() as session:
        project = AuthoredDocument(
            course_node_id=world["node_id"],
            course_root_id=world["node_id"],
            source_type="text",
            source_url=f"https://example.com/{uuid.uuid4().hex}",
            task_type="project",
            language="ukr",
            content_hash="c" * 64,
        )
        session.add(project)
        await session.flush()
        for doc_id in (world["doc_id"], project.id):
            session.add(
                DocumentSummary(
                    authored_document_id=doc_id,
                    course_root_id=world["node_id"],
                    title="Завдання",
                    status="ready",
                )
            )
        session.add(
            StudentEnrollment(
                student_id=world["student_id"], course_node_id=world["node_id"]
            )
        )
        await session.commit()
    world["project_id"] = project.id
    yield world
    async with session_factory() as session:
        await session.execute(
            delete(StudentEnrollment).where(
                StudentEnrollment.student_id == world["student_id"]
            )
        )
        await session.commit()
    await _drop_world(session_factory, world)


@pytest.fixture()
async def s3() -> AsyncGenerator[S3Client]:
    settings = get_settings()
    client = S3Client(
        endpoint_url=settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key.get_secret_value(),
        bucket=settings.s3_bucket,
    )
    await client.open()
    try:
        await client.ensure_bucket()
        yield client
    finally:
        await client.close()


@pytest.fixture()
def portal(
    world: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
    s3: S3Client,
) -> Generator[None]:
    """The portal's route on the live database and S3; the queue a double.

    The entry's rule reads the shipped submission-path file, as the body does
    — not whatever another test left in the process-wide cache.
    """

    async def _session() -> Any:
        async with session_factory() as session:
            yield session

    arq = AsyncMock()
    arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq:hw:one-rule"))
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_student] = lambda: StudentContext(
        student_id=world["student_id"],
        tenant_id=world["tenant_id"],
        login="one-rule",
        display_name=None,
    )
    app.dependency_overrides[get_s3_client] = lambda: s3
    app.dependency_overrides[get_arq_redis] = lambda: arq
    with patch(
        "course_supporter.homework.attempt_dedup.get_path_config",
        return_value=_shipped(),
    ):
        yield
    app.dependency_overrides.clear()


async def _submit(task_id: uuid.UUID, filename: str = "solution.zip") -> Response:
    """The student sends ``_ARCHIVE`` to ``task_id`` from the portal."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/portal/tasks/{task_id}/submissions",
            files={"file": (filename, _ARCHIVE, "application/zip")},
        )
    assert response.status_code == 202, response.text
    return response


async def _review(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    s3: S3Client,
    submission_id: uuid.UUID,
    *,
    router: Any,
) -> HomeworkSubmission:
    """The queue task the route dispatched, run by the real body."""
    async with session_factory() as session:
        job_id = await session.scalar(
            select(Job.id).where(Job.subject_id == submission_id)
        )
    assert job_id is not None
    ctx = {
        "session_factory": session_factory,
        "stage_router": router,
        "s3_client": s3,
        "job_try": 1,
    }
    with (
        patch(
            "course_supporter.homework.path_runner.get_path_config",
            return_value=_shipped(),
        ),
        patch(
            "course_supporter.homework.path_runner.AlwaysEnoughFundsPort",
            return_value=_PortDouble(),
        ),
        patch(
            "course_supporter.homework.criteria_evaluation.build_criteria_list_service",
            return_value=_ListSource(world["in_force"]),
        ),
        patch("course_supporter.homework.webhook.deliver_webhook", new=_webhook()),
    ):
        await arq_process_homework(ctx, str(job_id), str(submission_id))
    async with session_factory() as session:
        submission = await session.get(HomeworkSubmission, submission_id)
    assert submission is not None
    return submission


async def _submissions_of(
    session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(HomeworkSubmission)
                .where(HomeworkSubmission.authored_document_id == task_id)
            )
            or 0
        )


# ── Lock "ye": the same file again ──────────────────────────────────────────────────


@pytest.mark.usefixtures("portal")
class TestLockEOneRuleOfTheSameWork:
    """Lock "ye". A task: a new submission, then the copy.

    A project: "already submitted", as before.
    """

    async def test_the_same_archive_for_a_task_is_a_new_submission_and_a_copy(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        s3: S3Client,
    ) -> None:
        # ── The first submission, reviewed by the new path ──
        sent = await _submit(world["doc_id"])
        assert sent.json()["duplicate"] is False
        first = await _review(
            session_factory,
            world,
            s3,
            uuid.UUID(sent.json()["submission_id"]),
            router=_RouterDouble(session_factory),
        )
        assert first.status == "completed"  # in_app: the read-path delivers
        assert first.attempt_no == 1
        assert first.review_result is not None
        assert first.attempt_receipt is not None
        register_before = await _register_rows(session_factory)

        # ── The same archive, byte for byte: not "already submitted" ──
        again = await _submit(world["doc_id"])
        body = again.json()
        assert body["duplicate"] is False
        assert body["status"] == "received"
        second_id = uuid.UUID(body["submission_id"])
        assert second_id != first.id
        assert await _submissions_of(session_factory, world["doc_id"]) == 2

        # ── At the doors: the copy, with no model asked ──
        router = _NoModelRouter()
        with _providers_watched() as providers:
            second = await _review(session_factory, world, s3, second_id, router=router)
        assert router.calls == []
        assert not any(mock.await_count for mock in providers)
        assert await _register_rows(session_factory) == register_before

        assert second.status == "completed"
        assert second.file_hash == first.file_hash is not None
        assert (second.attempt_no, second.previous_submission_id) == (2, first.id)
        assert second.review_result == {
            **first.review_result,
            "unchanged_from_attempt": 1,
        }
        assert (
            ReviewStructureV1.model_validate(
                second.review_result
            ).unchanged_from_attempt
            == 1
        )
        assert second.attempt_receipt is not None
        assert second.attempt_receipt[DEDUP_RECEIPT_KEY] == 1
        assert (
            second.attempt_receipt[EVALUATOR_VERSION_RECEIPT_KEY]
            == first.attempt_receipt[EVALUATOR_VERSION_RECEIPT_KEY]
        )

    async def test_the_same_archive_for_a_project_is_already_submitted(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
    ) -> None:
        """Today's Mentor serves a project: the entry's raw-hash check stays."""
        sent = await _submit(world["project_id"], filename="proj.zip")
        assert sent.json()["duplicate"] is False
        first_id = uuid.UUID(sent.json()["submission_id"])
        # Stand in for today's Mentor's review: the check reads a terminal one.
        async with session_factory() as session:
            first = await session.get(HomeworkSubmission, first_id)
            assert first is not None
            first.status = "completed"
            await session.commit()

        again = await _submit(world["project_id"], filename="proj.zip")

        assert again.json() == {
            "submission_id": str(first_id),
            "status": "completed",
            "duplicate": True,
        }
        assert await _submissions_of(session_factory, world["project_id"]) == 1
