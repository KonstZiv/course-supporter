"""Storage of student feedback against a live database (mentor-rebuild task 05).

What only a real PostgreSQL can answer is here: that the three vocabularies are
enforced by the database and not merely by Python, that a repeat touch REPLACES
instead of adding a row, and that both counter filters — live submission, live
student — hold independently of each other.

The replacement test runs its two touches in two separate TRANSACTIONS, with a
commit between them, because ``now()`` in PostgreSQL is the time the
transaction started: two touches inside one transaction would leave
``updated_at`` unmoved no matter what the code does, and the test would pass
over a bug rather than through it.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.feedback_kinds import (
    FeedbackKind,
    FeedbackTargetKind,
    FeedbackValue,
)
from course_supporter.storage.feedback_repository import FeedbackRepository
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    Student,
    StudentFeedback,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


async def _student(session: AsyncSession, tenant: Tenant, external_id: str) -> Student:
    return await StudentRepository(session).create(
        tenant_id=tenant.id, external_id=external_id
    )


async def _reviewed_submission(
    session: AsyncSession,
    *,
    tenant: Tenant,
    student: Student,
    root: CourseNode,
    doc: AuthoredDocument,
    filename: str = "solution.py",
) -> HomeworkSubmission:
    """A submission that carries a review — the only kind a touch can point at."""
    submission = await HomeworkRepository(session).create(
        tenant_id=tenant.id,
        student_id=student.id,
        course_node_id=root.id,
        node_id=root.id,
        authored_document_id=doc.id,
        file_url=f"s3://bucket/{filename}",
        file_type="text/plain",
        original_filename=filename,
        delivery_mode="in_app",
    )
    submission.status = "completed"
    submission.review_markdown = "# Review\n\nWell done."
    await session.flush()
    return submission


async def _touch(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID,
    submission_id: uuid.UUID,
    value: FeedbackValue,
) -> StudentFeedback:
    return await FeedbackRepository(session).record(
        tenant_id=tenant_id,
        student_id=student_id,
        target_kind=FeedbackTargetKind.REVIEW,
        target_id=submission_id,
        kind=FeedbackKind.TOUCH,
        value=value,
    )


class TestTheDatabaseHoldsTheVocabularies:
    async def test_every_member_of_every_vocabulary_can_be_written(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Each CHECK admits each member — the enum and the DB agree both ways."""
        written: list[str] = []
        for i, value in enumerate(FeedbackValue):
            student = await _student(db_session, seed_tenant, f"ext-vocab-{i}")
            submission = await _reviewed_submission(
                db_session,
                tenant=seed_tenant,
                student=student,
                root=seed_root_node,
                doc=seed_material_entry,
            )
            row = await _touch(
                db_session,
                tenant_id=seed_tenant.id,
                student_id=student.id,
                submission_id=submission.id,
                value=value,
            )
            written.append(row.value)

        assert written, "the vocabulary is empty — nothing was measured"
        assert set(written) == {member.value for member in FeedbackValue}

    async def test_a_value_outside_the_vocabulary_is_refused_by_the_database(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """The CHECK, not the code: a raw INSERT of an unknown value fails."""
        student = await _student(db_session, seed_tenant, "ext-bad-value")
        submission = await _reviewed_submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )
        stmt = text(
            "INSERT INTO student_feedback "
            "(id, tenant_id, student_id, target_kind, target_id, kind, value) "
            "VALUES (:id, :tenant, :student, 'review', :target, 'touch', 'maybe')"
        )
        with pytest.raises(IntegrityError) as exc_info:
            await db_session.execute(
                stmt,
                {
                    "id": uuid.uuid4(),
                    "tenant": seed_tenant.id,
                    "student": student.id,
                    "target": submission.id,
                },
            )
        assert "ck_student_feedback_value" in str(exc_info.value)


class TestOneAnswerPerStudentPerTarget:
    async def test_two_students_on_one_review_are_two_rows(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """The rule is one row per (target, STUDENT), not one row per target."""
        author = await _student(db_session, seed_tenant, "ext-owner")
        other = await _student(db_session, seed_tenant, "ext-other")
        submission = await _reviewed_submission(
            db_session,
            tenant=seed_tenant,
            student=author,
            root=seed_root_node,
            doc=seed_material_entry,
        )
        for student in (author, other):
            await _touch(
                db_session,
                tenant_id=seed_tenant.id,
                student_id=student.id,
                submission_id=submission.id,
                value=FeedbackValue.HELPED,
            )

        rows = (
            (
                await db_session.execute(
                    select(StudentFeedback).where(
                        StudentFeedback.target_id == submission.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert {row.student_id for row in rows} == {author.id, other.id}


@pytest.fixture()
async def committed_review(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, uuid.UUID]]:
    """Tenant → course → task → student → reviewed submission, really committed.

    The replacement test needs two transactions, so its fixture cannot live on
    the savepoint session: everything here is committed and deleted afterwards
    in reverse FK order.
    """
    async with session_factory() as session:
        tenant = Tenant(name=f"test-tenant-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()

        root = make_root_course_node(
            tenant_id=tenant.id, title="Feedback Test Course", order=0
        )
        session.add(root)
        await session.flush()

        doc = AuthoredDocument(
            course_node_id=root.id,
            course_root_id=root.id,
            source_type="web",
            source_url="https://example.com/task",
        )
        session.add(doc)
        await session.flush()

        student = await _student(session, tenant, f"ext-replace-{uuid.uuid4().hex[:6]}")
        submission = await _reviewed_submission(
            session, tenant=tenant, student=student, root=root, doc=doc
        )
        await session.commit()

        ids = {
            "tenant_id": tenant.id,
            "course_node_id": root.id,
            "authored_document_id": doc.id,
            "student_id": student.id,
            "submission_id": submission.id,
        }

    yield ids

    async with session_factory() as session:
        await session.execute(
            StudentFeedback.__table__.delete().where(
                StudentFeedback.tenant_id == ids["tenant_id"]
            )
        )
        await session.execute(
            HomeworkSubmission.__table__.delete().where(
                HomeworkSubmission.tenant_id == ids["tenant_id"]
            )
        )
        await session.execute(
            Student.__table__.delete().where(Student.tenant_id == ids["tenant_id"])
        )
        await session.execute(
            AuthoredDocument.__table__.delete().where(
                AuthoredDocument.course_node_id == ids["course_node_id"]
            )
        )
        await session.execute(
            CourseNode.__table__.delete().where(CourseNode.id == ids["course_node_id"])
        )
        await session.execute(
            Tenant.__table__.delete().where(Tenant.id == ids["tenant_id"])
        )
        await session.commit()


class TestARepeatTouchReplacesTheAnswer:
    async def test_the_second_touch_leaves_one_row_and_moves_the_time(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_review: dict[str, uuid.UUID],
    ) -> None:
        """Two touches, two transactions: one row, the new answer, a later time.

        The two sessions are the point of the test. ``now()`` is the time the
        transaction began, so a second touch inside the first transaction would
        write the first transaction's timestamp and the time assertion below
        would hold even with ``updated_at`` missing from the upsert.
        """
        async with session_factory() as session:
            await _touch(
                session,
                tenant_id=committed_review["tenant_id"],
                student_id=committed_review["student_id"],
                submission_id=committed_review["submission_id"],
                value=FeedbackValue.HELPED,
            )
            await session.commit()

        async with session_factory() as session:
            await _touch(
                session,
                tenant_id=committed_review["tenant_id"],
                student_id=committed_review["student_id"],
                submission_id=committed_review["submission_id"],
                value=FeedbackValue.NOT_HELPED,
            )
            await session.commit()

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(StudentFeedback).where(
                            StudentFeedback.target_id
                            == committed_review["submission_id"]
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert len(rows) == 1, "a repeat touch must replace, not add"
        row = rows[0]
        assert row.value == FeedbackValue.NOT_HELPED.value
        assert row.updated_at > row.created_at, (
            "updated_at did not move: the upsert must set it explicitly"
        )


class TestCountersCountLiveThingsOnly:
    async def test_counters_split_by_answer_per_task_and_in_total(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Two answers on one task: the two levels agree with each other."""
        repo = FeedbackRepository(db_session)
        for i, value in enumerate((FeedbackValue.HELPED, FeedbackValue.NOT_HELPED)):
            student = await _student(db_session, seed_tenant, f"ext-count-{i}")
            submission = await _reviewed_submission(
                db_session,
                tenant=seed_tenant,
                student=student,
                root=seed_root_node,
                doc=seed_material_entry,
            )
            await _touch(
                db_session,
                tenant_id=seed_tenant.id,
                student_id=student.id,
                submission_id=submission.id,
                value=value,
            )

        by_task = await repo.counters_by_task(
            tenant_id=seed_tenant.id, course_node_id=seed_root_node.id
        )
        assert by_task, "the probe found no task — nothing was measured"
        assert [
            (row.authored_document_id, row.helped, row.not_helped) for row in by_task
        ] == [(seed_material_entry.id, 1, 1)]

        total = await repo.counters_for_task(
            tenant_id=seed_tenant.id, authored_document_id=seed_material_entry.id
        )
        assert total.helped == 1
        assert total.not_helped == 1

    async def test_a_soft_deleted_submission_falls_out_of_the_counters(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """First filter, alone: the student is alive, the submission is not."""
        student = await _student(db_session, seed_tenant, "ext-dead-submission")
        submission = await _reviewed_submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )
        await _touch(
            db_session,
            tenant_id=seed_tenant.id,
            student_id=student.id,
            submission_id=submission.id,
            value=FeedbackValue.HELPED,
        )
        before = await FeedbackRepository(db_session).counters_for_task(
            tenant_id=seed_tenant.id, authored_document_id=seed_material_entry.id
        )
        assert before.helped == 1, "the touch was not counted before the deletion"

        submission.deleted_at = datetime.now(UTC)
        await db_session.flush()

        after = await FeedbackRepository(db_session).counters_for_task(
            tenant_id=seed_tenant.id, authored_document_id=seed_material_entry.id
        )
        assert after.helped == 0
        assert await db_session.scalar(
            select(func.count()).select_from(StudentFeedback)
        ), "the row itself must stay — only the count drops it"

    async def test_a_soft_deleted_student_falls_out_of_the_counters(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Second filter, alone: the submission is alive, the student is not.

        Written as its own test because the two filters are independent facts:
        a student can be soft-deleted by direct SQL while their submissions
        stay untouched, which is exactly what the production clean-up of
        2026-09-17 did.
        """
        student = await _student(db_session, seed_tenant, "ext-dead-student")
        submission = await _reviewed_submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )
        await _touch(
            db_session,
            tenant_id=seed_tenant.id,
            student_id=student.id,
            submission_id=submission.id,
            value=FeedbackValue.HELPED,
        )
        before = await FeedbackRepository(db_session).counters_for_task(
            tenant_id=seed_tenant.id, authored_document_id=seed_material_entry.id
        )
        assert before.helped == 1, "the touch was not counted before the deletion"

        student.deleted_at = datetime.now(UTC)
        await db_session.flush()

        after = await FeedbackRepository(db_session).counters_for_task(
            tenant_id=seed_tenant.id, authored_document_id=seed_material_entry.id
        )
        assert after.helped == 0
        assert submission.deleted_at is None, "the submission stayed alive"

    async def test_another_tenant_counts_nothing(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Leak-safe: a foreign tenant gets zeros and an empty list, not an error."""
        student = await _student(db_session, seed_tenant, "ext-isolation")
        submission = await _reviewed_submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )
        await _touch(
            db_session,
            tenant_id=seed_tenant.id,
            student_id=student.id,
            submission_id=submission.id,
            value=FeedbackValue.HELPED,
        )

        stranger = Tenant(name=f"test-stranger-{uuid.uuid4().hex[:8]}")
        db_session.add(stranger)
        await db_session.flush()

        repo = FeedbackRepository(db_session)
        assert (
            await repo.counters_by_task(
                tenant_id=stranger.id, course_node_id=seed_root_node.id
            )
            == []
        )
        total = await repo.counters_for_task(
            tenant_id=stranger.id, authored_document_id=seed_material_entry.id
        )
        assert (total.helped, total.not_helped) == (0, 0)


class TestTheReturnedRowIsTheStoredRow:
    async def test_the_answer_comes_back_new_even_when_the_session_holds_it(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """A row already in the identity map is refreshed by the upsert.

        RETURNING rows are matched against the session's identity map, and an
        object already there is NOT re-populated unless the statement asks for
        it (``orm/query.py``: ``populate_existing`` "force[s] all the data read
        from the SELECT to be populated into the ORM objects returned, even if
        these objects are already in the identity map"). The route reads the
        student's own touch before writing the next one, so this is the
        ordinary case, not a corner of it.
        """
        student = await _student(db_session, seed_tenant, "ext-identity-map")
        submission = await _reviewed_submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )
        repo = FeedbackRepository(db_session)
        await _touch(
            db_session,
            tenant_id=seed_tenant.id,
            student_id=student.id,
            submission_id=submission.id,
            value=FeedbackValue.HELPED,
        )
        held = await repo.get_for_target(
            tenant_id=seed_tenant.id,
            student_id=student.id,
            target_kind=FeedbackTargetKind.REVIEW,
            target_id=submission.id,
        )
        assert held is not None, "the probe found no row — nothing was measured"

        returned = await _touch(
            db_session,
            tenant_id=seed_tenant.id,
            student_id=student.id,
            submission_id=submission.id,
            value=FeedbackValue.NOT_HELPED,
        )
        assert returned.value == FeedbackValue.NOT_HELPED.value


class TestReadingIsScopedByTenant:
    async def test_a_row_of_another_tenant_is_not_returned(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Invariant 3 by area, not by form: the read carries the tenant."""
        student = await _student(db_session, seed_tenant, "ext-tenant-read")
        submission = await _reviewed_submission(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
        )
        await _touch(
            db_session,
            tenant_id=seed_tenant.id,
            student_id=student.id,
            submission_id=submission.id,
            value=FeedbackValue.HELPED,
        )
        stranger = Tenant(name=f"test-stranger-{uuid.uuid4().hex[:8]}")
        db_session.add(stranger)
        await db_session.flush()

        repo = FeedbackRepository(db_session)
        own = await repo.get_for_target(
            tenant_id=seed_tenant.id,
            student_id=student.id,
            target_kind=FeedbackTargetKind.REVIEW,
            target_id=submission.id,
        )
        assert own is not None, "the row is unreadable for its own tenant"

        foreign = await repo.get_for_target(
            tenant_id=stranger.id,
            student_id=student.id,
            target_kind=FeedbackTargetKind.REVIEW,
            target_id=submission.id,
        )
        assert foreign is None
