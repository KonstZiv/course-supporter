"""A written test: its draft, its publication, what a publication costs (task 07b, V1).

What is pinned here, against a live database — the claims are about versions
and about money, and both are decided by indexes and by rows of jobs:

* A publication identical to the latest version makes no version and asks for
  nothing; a return to an earlier state is a new version that asks for nothing,
  because its explanations are written already; a new pass mark is a new
  version the student cannot tell apart, and it asks for nothing (decisions 6
  and 9).
* Another job of the task in flight refuses the publication, and nothing of it
  survives the rollback (decision 12).
* Explanations stuck in ``pending`` with no job in flight are asked for again
  by a publication and by a submission; failed ones in the course language by
  a publication only (decision 13).
* The language is the course's as it stands at publication (operator's
  decision at the stop of V1, 2026-09-25).

The queue is the shipped one, writing real ``Job`` rows: whether a job of the
task is in flight is the question under test, and a counter would answer it
wrongly. Only the dispatch to a worker is absent, and what a worker leaves
behind is written by hand (:func:`_finish_the_work`, :func:`_the_job_dies`,
:func:`_the_generation_fails`).

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.homework.explanation_queue import ArqExplanationQueue
from course_supporter.homework.reference_service import (
    GenerationInProgressError,
    ReferenceService,
)
from course_supporter.homework.test_object import (
    DraftBody,
    DraftOption,
    DraftQuestion,
)
from course_supporter.homework.test_object_service import (
    NotATestObjectError,
    TestObjectService,
)
from course_supporter.jobs import JOB_SUBJECT_TYPE, JobType
from course_supporter.reference_kinds import ReferenceState
from course_supporter.storage.content_hash import compute_content_hash
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    Job,
    TaskReference,
    Tenant,
    TestVersion,
)
from course_supporter.storage.task_reference_repository import TaskReferenceRepository
from course_supporter.storage.test_object_repository import TestObjectRepository
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


def _draft(*, right: int = 1, pass_threshold: int | None = 80) -> DraftBody:
    """One question, two options; ``right`` is the position of the right one."""
    return DraftBody(
        pass_threshold=pass_threshold,
        questions=(
            DraftQuestion(
                text="Що виведе print(2 ** 3)?",
                options=(
                    DraftOption(text="6", correct=right == 0),
                    DraftOption(text="8", correct=right == 1),
                ),
            ),
        ),
    )


_A = _draft()
_B = _draft(right=0)
"""The same question with the other option right: another key, the same text."""


async def _written_test(
    session: AsyncSession, root: CourseNode, draft: DraftBody = _A
) -> AuthoredDocument:
    """A test as creating it leaves it (PRE-FLIGHT section 4.3).

    No file and never processed; the course's language copied as it was then.
    """
    document = AuthoredDocument(
        course_node_id=root.id,
        course_root_id=root.id,
        source_type="test_object",
        source_url="test-object:",
        task_type="test",
        language=root.default_language,
        content_hash=compute_content_hash(b"", []),
        title="Тест до лекції 3",
    )
    session.add(document)
    await session.flush()
    await TestObjectRepository(session).replace_draft(document.id, draft.to_jsonb())
    return document


def _queue(session: AsyncSession, tenant_id: uuid.UUID) -> ArqExplanationQueue:
    """The shipped queue with its real ``Job`` rows; the worker's Redis is absent."""
    return ArqExplanationQueue(
        redis=AsyncMock(enqueue_job=AsyncMock(return_value=None)),
        session=session,
        tenant_id=tenant_id,
    )


def _service(session: AsyncSession, tenant_id: uuid.UUID) -> TestObjectService:
    return TestObjectService(session, _queue(session, tenant_id))


def _references(session: AsyncSession, tenant_id: uuid.UUID) -> ReferenceService:
    return ReferenceService(session, _queue(session, tenant_id))


async def _jobs(session: AsyncSession, document_id: uuid.UUID) -> list[Job]:
    """The explanation jobs of the test, oldest first."""
    stmt = (
        select(Job)
        .where(
            Job.subject_id == document_id,
            Job.job_type == JobType.KEY_EXPLANATION.value,
        )
        .order_by(Job.queued_at, Job.id)
    )
    return list((await session.execute(stmt)).scalars())


async def _explanations(
    session: AsyncSession, document_id: uuid.UUID
) -> list[TaskReference]:
    """The versions of the explanations of the test, in the order they were made."""
    stmt = (
        select(TaskReference)
        .where(TaskReference.authored_document_id == document_id)
        .order_by(TaskReference.version)
    )
    return list((await session.execute(stmt)).scalars())


async def _versions(session: AsyncSession, document_id: uuid.UUID) -> list[int]:
    stmt = (
        select(TestVersion.version)
        .where(TestVersion.authored_document_id == document_id)
        .order_by(TestVersion.version)
    )
    return list((await session.execute(stmt)).scalars())


async def _end_the_jobs(
    session: AsyncSession, document_id: uuid.UUID, status: str
) -> None:
    for job in await _jobs(session, document_id):
        if job.status in ("queued", "active"):
            job.status = status
    await session.flush()


async def _finish_the_work(session: AsyncSession, document_id: uuid.UUID) -> None:
    """What a worker leaves behind: its job complete, its version written."""
    await _end_the_jobs(session, document_id, "complete")
    repo = TaskReferenceRepository(session)
    for explanation in await _explanations(session, document_id):
        if explanation.state == ReferenceState.PENDING.value:
            await repo.mark_ready(explanation.id, {"1": "Два в кубі — вісім."})


async def _the_job_dies(session: AsyncSession, document_id: uuid.UUID) -> None:
    """A job that ended without writing its version: the version stays pending."""
    await _end_the_jobs(session, document_id, "cancelled")


async def _the_generation_fails(session: AsyncSession, document_id: uuid.UUID) -> None:
    """A job that gave up, and said so on its version."""
    await _end_the_jobs(session, document_id, "failed")
    repo = TaskReferenceRepository(session)
    for explanation in await _explanations(session, document_id):
        if explanation.state == ReferenceState.PENDING.value:
            await repo.mark_failed(explanation.id, "generation failed: the ladder")


class TestPublication:
    async def test_the_same_draft_again_makes_no_version_and_asks_for_nothing(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _written_test(db_session, seed_root_node)
        service = _service(db_session, seed_root_node.tenant_id)

        first = await service.publish(document.id)
        again = await service.publish(document.id)

        assert (first.created, again.created) == (True, False)
        assert again.version.id == first.version.id
        assert await _versions(db_session, document.id) == [1]
        assert len(await _jobs(db_session, document.id)) == 1

    async def test_a_return_to_an_earlier_state_is_a_new_version_that_asks_for_nothing(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _written_test(db_session, seed_root_node)
        service = _service(db_session, seed_root_node.tenant_id)
        first = await service.publish(document.id)
        await _finish_the_work(db_session, document.id)
        await service.save_draft(document.id, _B)
        await service.publish(document.id)
        await _finish_the_work(db_session, document.id)
        await service.save_draft(document.id, _A)

        back = await service.publish(document.id)

        assert (back.created, back.version.version) == (True, 3)
        assert back.version.content_digest == first.version.content_digest
        assert len(await _jobs(db_session, document.id)) == 2, (
            "the explanations of the first state are written already"
        )

    async def test_a_new_pass_mark_is_a_version_the_student_cannot_tell_apart(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _written_test(db_session, seed_root_node)
        service = _service(db_session, seed_root_node.tenant_id)
        first = await service.publish(document.id)
        await _finish_the_work(db_session, document.id)
        await service.save_draft(document.id, _draft(pass_threshold=60))

        second = await service.publish(document.id)

        assert (second.created, second.version.version) == (True, 2)
        assert second.version.content_digest == first.version.content_digest
        assert len(await _jobs(db_session, document.id)) == 1, (
            "a pass mark buys no explanations"
        )

    async def test_the_explanations_are_asked_for_by_the_axes_of_the_version(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _written_test(db_session, seed_root_node)

        publication = await _service(db_session, seed_root_node.tenant_id).publish(
            document.id
        )

        [explanation] = await _explanations(db_session, document.id)
        [job] = await _jobs(db_session, document.id)
        assert (
            explanation.source_content_hash,
            explanation.answers_hash,
            explanation.source_task_type,
            explanation.language,
        ) == (
            publication.version.content_digest,
            publication.version.answers_digest,
            "test",
            "ukr",
        )
        assert job.input_params == {"reference_id": str(explanation.id)}


@pytest.fixture()
async def committed_test(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[uuid.UUID, uuid.UUID]]:
    """A written test, committed, with another job of the task in flight.

    Its course's language was changed after the test was written, so a
    publication also has a language update to lose.
    """
    async with session_factory() as session:
        tenant = Tenant(name=f"test-object-service-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        root = make_root_course_node(tenant_id=tenant.id, title="Collision", order=0)
        session.add(root)
        await session.flush()
        document = await _written_test(session, root)
        root.default_language = "eng"
        await JobRepository(session).create(
            tenant_id=tenant.id,
            job_type=JobType.KEY_EXPLANATION,
            input_params={"reference_id": str(uuid.uuid4())},
            subject_type=JOB_SUBJECT_TYPE[JobType.KEY_EXPLANATION],
            subject_id=document.id,
        )
        await session.commit()
        ids = (tenant.id, document.id)

    yield ids

    async with session_factory() as session:
        # A job names its task without a foreign key, so it goes by hand.
        await session.execute(delete(Job).where(Job.subject_id == ids[1]))
        await session.execute(delete(Tenant).where(Tenant.id == ids[0]))
        await session.commit()


class TestAJobInFlight:
    async def test_it_refuses_the_publication_and_nothing_of_it_survives(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_test: tuple[uuid.UUID, uuid.UUID],
    ) -> None:
        tenant_id, document_id = committed_test

        async with session_factory() as session:
            with pytest.raises(GenerationInProgressError) as refused:
                await _service(session, tenant_id).publish(document_id)
            await session.rollback()

        assert refused.value.code == "GENERATION_IN_PROGRESS"
        async with session_factory() as session:
            assert await _versions(session, document_id) == []
            assert await _explanations(session, document_id) == []
            assert len(await _jobs(session, document_id)) == 1, (
                "only the job that was in flight"
            )
            document = await session.get(AuthoredDocument, document_id)
            assert document is not None
            assert document.language == "ukr", "the language went with the rest"


class TestExplanationsThatWillNotBeWritten:
    async def test_a_stuck_version_is_asked_for_again_by_a_publication(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _written_test(db_session, seed_root_node)
        service = _service(db_session, seed_root_node.tenant_id)
        await service.publish(document.id)
        await _the_job_dies(db_session, document.id)

        again = await service.publish(document.id)

        assert again.created is False
        stuck, fresh = await _explanations(db_session, document.id)
        assert (stuck.state, fresh.state) == ("failed", "pending")
        assert stuck.failure_reason is not None
        assert stuck.failure_reason.startswith("stuck")
        assert len(await _jobs(db_session, document.id)) == 2

    async def test_a_stuck_version_is_asked_for_again_by_a_submission(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _written_test(db_session, seed_root_node)
        await _service(db_session, seed_root_node.tenant_id).publish(document.id)
        await _the_job_dies(db_session, document.id)

        await _references(db_session, seed_root_node.tenant_id).request_explanations(
            document.id, "ukr"
        )

        stuck, fresh = await _explanations(db_session, document.id)
        assert (stuck.state, fresh.state) == ("failed", "pending")
        assert len(await _jobs(db_session, document.id)) == 2

    async def test_a_version_being_written_is_left_to_its_job(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _written_test(db_session, seed_root_node)
        await _service(db_session, seed_root_node.tenant_id).publish(document.id)

        await _references(db_session, seed_root_node.tenant_id).request_explanations(
            document.id, "ukr"
        )

        [explanation] = await _explanations(db_session, document.id)
        assert explanation.state == "pending"
        assert len(await _jobs(db_session, document.id)) == 1

    async def test_a_failed_course_language_version_is_not_retried_by_a_submission(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _written_test(db_session, seed_root_node)
        await _service(db_session, seed_root_node.tenant_id).publish(document.id)
        await _the_generation_fails(db_session, document.id)

        await _references(db_session, seed_root_node.tenant_id).request_explanations(
            document.id, "ukr"
        )

        [failed] = await _explanations(db_session, document.id)
        assert failed.state == "failed"
        assert len(await _jobs(db_session, document.id)) == 1

    async def test_a_failed_course_language_version_is_retried_by_a_publication(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _written_test(db_session, seed_root_node)
        service = _service(db_session, seed_root_node.tenant_id)
        await service.publish(document.id)
        await _the_generation_fails(db_session, document.id)

        await service.publish(document.id)

        failed, fresh = await _explanations(db_session, document.id)
        assert (failed.state, fresh.state) == ("failed", "pending")
        assert len(await _jobs(db_session, document.id)) == 2


class TestAStudentsLanguage:
    async def test_a_submission_asks_for_the_explanations_in_its_language(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _written_test(db_session, seed_root_node)
        publication = await _service(db_session, seed_root_node.tenant_id).publish(
            document.id
        )
        await _finish_the_work(db_session, document.id)

        await _references(db_session, seed_root_node.tenant_id).request_explanations(
            document.id, "eng"
        )

        course, student = await _explanations(db_session, document.id)
        assert (course.language, student.language) == ("ukr", "eng")
        assert (student.source_content_hash, student.answers_hash) == (
            publication.version.content_digest,
            publication.version.answers_digest,
        )
        assert len(await _jobs(db_session, document.id)) == 2

    async def test_nothing_published_asks_for_nothing(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _written_test(db_session, seed_root_node)

        await _references(db_session, seed_root_node.tenant_id).request_explanations(
            document.id, "eng"
        )

        assert await _explanations(db_session, document.id) == []
        assert await _jobs(db_session, document.id) == []


class TestTheCourseLanguage:
    async def test_a_language_changed_after_the_test_was_written_is_the_one_published(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _written_test(db_session, seed_root_node)
        assert document.language == "ukr"
        seed_root_node.default_language = "eng"
        await db_session.flush()

        publication = await _service(db_session, seed_root_node.tenant_id).publish(
            document.id
        )

        assert publication.version.language == "eng"
        options = publication.version.body["questions"][0]["options"]
        assert [option["label"] for option in options] == ["a", "b"]
        await db_session.refresh(document)
        assert document.language == "eng", "the test follows its course"
        [explanation] = await _explanations(db_session, document.id)
        assert explanation.language == "eng"


class TestTheDraft:
    async def test_the_draft_is_replaced_whole_and_a_title_renames_the_test(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _written_test(db_session, seed_root_node)
        service = _service(db_session, seed_root_node.tenant_id)

        await service.save_draft(document.id, _B, title="Тест до лекції 4")

        draft = await TestObjectRepository(db_session).get_draft(document.id)
        assert draft is not None
        assert DraftBody.from_jsonb(draft.body) == _B
        await db_session.refresh(document)
        assert document.title == "Тест до лекції 4"

        await service.save_draft(document.id, _A)

        await db_session.refresh(document)
        assert document.title == "Тест до лекції 4", "no title given — the name stays"

    async def test_the_version_in_force_is_the_latest_published(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _written_test(db_session, seed_root_node)
        service = _service(db_session, seed_root_node.tenant_id)
        assert await service.current_version(document.id) is None

        await service.publish(document.id)
        await _finish_the_work(db_session, document.id)
        await service.save_draft(document.id, _B)
        await service.publish(document.id)

        current = await service.current_version(document.id)
        assert current is not None
        assert current.version == 2

    async def test_a_document_that_is_not_a_written_test_is_refused(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = AuthoredDocument(
            course_node_id=seed_root_node.id,
            course_root_id=seed_root_node.id,
            source_type="text",
            source_url="file:///tmp/test.md",
            task_type="test",
            language="ukr",
        )
        db_session.add(document)
        await db_session.flush()
        service = _service(db_session, seed_root_node.tenant_id)

        with pytest.raises(NotATestObjectError):
            await service.save_draft(document.id, _A)
        with pytest.raises(NotATestObjectError):
            await service.publish(document.id)
        with pytest.raises(NotATestObjectError):
            await service.current_version(document.id)
