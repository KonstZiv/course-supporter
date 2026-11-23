"""The store of remark comments over K1's table, on a real database (task 12a, K3).

What the in-memory double of the unit tests cannot show is pinned here: the
filters every read keeps — the tenant, the live submission, the live student
(K1 asks them of every reader) — the order of a thread, the row a reply is
written as, and that a mark never lands on the Mentor's comment. The reply
core's own walks over this store are ``test_reply_core_db.py``. Requires
``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from course_supporter.homework.remark_comments import (
    NewReply,
    ScreenMark,
    ThreadScope,
    build_remark_comment_repository,
)
from course_supporter.models.source import AssignmentType
from course_supporter.remark_kinds import CommentAuthor, CommentKind, CommentTopic
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    RemarkComment,
    Student,
    Tenant,
)
from course_supporter.storage.remark_comment_repository import (
    SqlRemarkCommentRepository,
)
from course_supporter.storage.student_repository import StudentRepository
from tests._helpers.remark_comments import a_stored_comment

pytestmark = pytest.mark.requires_db

_V1 = "1" * 64
_V2 = "2" * 64
_T0 = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


async def _task(session: AsyncSession, root: CourseNode) -> AuthoredDocument:
    doc = AuthoredDocument(
        course_node_id=root.id,
        course_root_id=root.id,
        source_type="text",
        source_url="s3://bucket/task.md",
        task_type=AssignmentType.TASK.value,
        content_hash=_V1,
    )
    session.add(doc)
    await session.flush()
    return doc


async def _student(session: AsyncSession, tenant: Tenant) -> Student:
    return await StudentRepository(session).create(
        tenant_id=tenant.id, external_id=f"ext-store-{uuid.uuid4().hex[:8]}"
    )


async def _attempt(
    session: AsyncSession,
    *,
    student: Student,
    doc: AuthoredDocument,
    minute: int,
    version: str = _V1,
    **columns: Any,
) -> HomeworkSubmission:
    submission = HomeworkSubmission(
        tenant_id=student.tenant_id,
        student_id=student.id,
        course_node_id=doc.course_root_id,
        node_id=doc.course_node_id,
        authored_document_id=doc.id,
        file_url="s3://bucket/solution.py",
        file_type="text/plain",
        original_filename="solution.py",
        delivery_mode="in_app",
        status="delivered",
        review_markdown="## Рецензія",
        task_version=version,
        created_at=_T0 + timedelta(minutes=minute),
        **columns,
    )
    session.add(submission)
    await session.flush()
    return submission


def _scope(submission: HomeworkSubmission) -> ThreadScope:
    assert submission.task_version is not None
    return ThreadScope(
        tenant_id=submission.tenant_id,
        student_id=submission.student_id,
        authored_document_id=submission.authored_document_id,
        task_version=submission.task_version,
    )


class TestTheFactoryChoosesTheTable:
    def test_the_production_store_is_the_one_over_k1s_table(
        self, db_session: AsyncSession
    ) -> None:
        assert isinstance(
            build_remark_comment_repository(db_session), SqlRemarkCommentRepository
        )


class TestAReplyIsWrittenWhole:
    async def test_the_row_and_the_record_it_is_read_back_as(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        student = await _student(db_session, seed_tenant)
        attempt = await _attempt(db_session, student=student, doc=doc, minute=0)
        store = SqlRemarkCommentRepository(db_session)

        record = await store.add_student_reply(
            NewReply(
                tenant_id=attempt.tenant_id,
                student_id=student.id,
                authored_document_id=doc.id,
                task_version=_V1,
                submission_id=attempt.id,
                item_id="c2.p1",
                parent_id=None,
                kind=CommentKind.OBJECTION,
                body="Пункт виконано: перевірка є в рядку 4.",
            )
        )

        row = await db_session.get(RemarkComment, record.id)
        assert row is not None
        assert (row.tenant_id, row.student_id, row.authored_document_id) == (
            seed_tenant.id,
            student.id,
            doc.id,
        )
        assert (row.task_version, row.submission_id, row.item_id) == (
            _V1,
            attempt.id,
            "c2.p1",
        )
        assert (row.author, row.kind) == ("student", "objection")
        assert row.topic is None and row.answered_in_submission_id is None
        assert record.author is CommentAuthor.STUDENT
        assert record.kind is CommentKind.OBJECTION
        assert record.created_at == row.created_at is not None


class TestTheReads:
    async def test_the_count_is_the_students_on_this_remark_of_this_review(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        student = await _student(db_session, seed_tenant)
        attempt = await _attempt(db_session, student=student, doc=doc, minute=0)
        other = await _attempt(db_session, student=student, doc=doc, minute=1)
        first = await a_stored_comment(db_session, attempt, item_id="c1")
        await a_stored_comment(db_session, attempt, item_id="c1", minute=1)
        await a_stored_comment(
            db_session,
            attempt,
            item_id="c1",
            author=CommentAuthor.MENTOR,
            kind=CommentKind.ANSWER,
            parent_id=first.id,
            minute=2,
        )
        await a_stored_comment(db_session, attempt, item_id="c2", minute=3)
        await a_stored_comment(db_session, other, item_id="c1", minute=4)
        store = SqlRemarkCommentRepository(db_session)

        count = await store.count_student_replies(
            tenant_id=seed_tenant.id, submission_id=attempt.id, item_id="c1"
        )
        foreign = await store.count_student_replies(
            tenant_id=uuid.uuid4(), submission_id=attempt.id, item_id="c1"
        )

        assert (count, foreign) == (2, 0)

    async def test_a_thread_runs_through_the_attempts_of_one_version(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """Oldest first, both authors; another version, student or item — not."""
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant)
        them = await _student(db_session, seed_tenant)
        first = await _attempt(db_session, student=me, doc=doc, minute=0)
        second = await _attempt(db_session, student=me, doc=doc, minute=1)
        newer = await _attempt(db_session, student=me, doc=doc, minute=2, version=_V2)
        theirs = await _attempt(db_session, student=them, doc=doc, minute=3)
        question = await a_stored_comment(db_session, first, minute=0)
        answer = await a_stored_comment(
            db_session,
            second,
            author=CommentAuthor.MENTOR,
            kind=CommentKind.ANSWER,
            parent_id=question.id,
            minute=2,
        )
        # Written later, but earlier in time: the order is the time's.
        follow_up = await a_stored_comment(
            db_session, second, parent_id=answer.id, minute=3
        )
        early = await a_stored_comment(db_session, second, minute=1)
        await a_stored_comment(db_session, second, item_id="c9", minute=4)
        await a_stored_comment(db_session, newer, minute=5)
        await a_stored_comment(db_session, theirs, minute=6)
        store = SqlRemarkCommentRepository(db_session)

        thread = await store.thread(_scope(second), ["c1"])
        none = await store.thread(_scope(second), [])

        assert [c.id for c in thread] == [
            question.id,
            early.id,
            answer.id,
            follow_up.id,
        ]
        assert [c.author for c in thread].count(CommentAuthor.MENTOR) == 1
        assert none == []

    async def test_a_thread_keeps_to_the_tenant(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        student = await _student(db_session, seed_tenant)
        attempt = await _attempt(db_session, student=student, doc=doc, minute=0)
        await a_stored_comment(db_session, attempt)
        store = SqlRemarkCommentRepository(db_session)
        scope = _scope(attempt)
        stranger = ThreadScope(
            uuid.uuid4(), scope.student_id, scope.authored_document_id, _V1
        )

        assert len(await store.thread(scope, ["c1"])) == 1
        assert await store.thread(stranger, ["c1"]) == []
        assert (
            await store.unanswered_student_replies(
                tenant_id=uuid.uuid4(), submission_ids=[attempt.id]
            )
            == []
        )

    async def test_unanswered_are_the_students_with_no_answer_yet(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        student = await _student(db_session, seed_tenant)
        attempt = await _attempt(db_session, student=student, doc=doc, minute=0)
        later = await _attempt(db_session, student=student, doc=doc, minute=1)
        waiting = await a_stored_comment(db_session, attempt, minute=1)
        first = await a_stored_comment(db_session, attempt, item_id="c2", minute=0)
        answered = await a_stored_comment(
            db_session, attempt, answered_in=later.id, minute=2
        )
        await a_stored_comment(
            db_session,
            attempt,
            author=CommentAuthor.MENTOR,
            kind=CommentKind.ANSWER,
            parent_id=answered.id,
            minute=3,
        )
        store = SqlRemarkCommentRepository(db_session)

        unanswered = await store.unanswered_student_replies(
            tenant_id=seed_tenant.id, submission_ids=[attempt.id]
        )

        assert [c.id for c in unanswered] == [first.id, waiting.id]

    async def test_a_set_of_submissions_is_read_whole_oldest_first(
        self,
        db_session: AsyncSession,
        async_engine: AsyncEngine,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """Replies on several attempts: each once, by time across them, in one
        query; an attempt outside the set is not read."""
        doc = await _task(db_session, seed_root_node)
        student = await _student(db_session, seed_tenant)
        older = await _attempt(db_session, student=student, doc=doc, minute=0)
        newer = await _attempt(db_session, student=student, doc=doc, minute=1)
        outside = await _attempt(db_session, student=student, doc=doc, minute=2)
        on_newer = await a_stored_comment(db_session, newer, minute=0)
        on_older = await a_stored_comment(db_session, older, minute=1)
        await a_stored_comment(db_session, outside, minute=2)
        store = SqlRemarkCommentRepository(db_session)
        statements: list[str] = []

        def _count(*args: Any) -> None:
            statements.append(args[2])

        engine = async_engine.sync_engine
        event.listen(engine, "before_cursor_execute", _count)
        try:
            unanswered = await store.unanswered_student_replies(
                tenant_id=seed_tenant.id,
                submission_ids=[older.id, newer.id, older.id],
            )
            nothing = await store.unanswered_student_replies(
                tenant_id=seed_tenant.id, submission_ids=[]
            )
        finally:
            event.remove(engine, "before_cursor_execute", _count)

        assert [c.id for c in unanswered] == [on_newer.id, on_older.id]
        assert nothing == []
        assert len(statements) == 1, statements


class TestOnlyTheLivingAreRead:
    """K1: reads filter the live submission and the live student."""

    async def test_a_soft_deleted_submission_takes_its_comments_out_of_sight(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        student = await _student(db_session, seed_tenant)
        gone = await _attempt(db_session, student=student, doc=doc, minute=0)
        kept = await _attempt(db_session, student=student, doc=doc, minute=1)
        await a_stored_comment(db_session, gone, minute=0)
        stays = await a_stored_comment(db_session, kept, minute=1)
        gone.deleted_at = _T0
        await db_session.flush()
        store = SqlRemarkCommentRepository(db_session)

        thread = await store.thread(_scope(kept), ["c1"])
        unanswered = await store.unanswered_student_replies(
            tenant_id=seed_tenant.id, submission_ids=[gone.id]
        )

        assert [c.id for c in thread] == [stays.id]
        assert unanswered == []

    async def test_a_soft_deleted_student_takes_all_of_them(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        student = await _student(db_session, seed_tenant)
        attempt = await _attempt(db_session, student=student, doc=doc, minute=0)
        await a_stored_comment(db_session, attempt)
        student.deleted_at = _T0
        await db_session.flush()
        store = SqlRemarkCommentRepository(db_session)

        assert await store.thread(_scope(attempt), ["c1"]) == []
        assert (
            await store.unanswered_student_replies(
                tenant_id=seed_tenant.id, submission_ids=[attempt.id]
            )
            == []
        )


class TestTheMarks:
    async def test_a_mark_writes_its_three_columns_on_a_students_reply(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        student = await _student(db_session, seed_tenant)
        attempt = await _attempt(db_session, student=student, doc=doc, minute=0)
        reply = await a_stored_comment(db_session, attempt)
        store = SqlRemarkCommentRepository(db_session)

        await store.mark_screened(
            tenant_id=seed_tenant.id,
            comment_id=reply.id,
            mark=ScreenMark(CommentTopic.HOSTILE, 0.97, "prompt_injection"),
        )

        row = (
            await db_session.execute(
                select(
                    RemarkComment.topic,
                    RemarkComment.topic_confidence,
                    RemarkComment.hostile_category,
                ).where(RemarkComment.id == reply.id)
            )
        ).one()
        assert tuple(row) == ("hostile", 0.97, "prompt_injection")
        (read,) = await store.unanswered_student_replies(
            tenant_id=seed_tenant.id, submission_ids=[attempt.id]
        )
        assert (read.topic, read.hostile_category) == (
            CommentTopic.HOSTILE,
            "prompt_injection",
        )

    async def test_a_mark_never_lands_on_the_mentors_comment_or_another_tenant(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """K1's table refuses the Mentor a topic; the store does not try."""
        doc = await _task(db_session, seed_root_node)
        student = await _student(db_session, seed_tenant)
        attempt = await _attempt(db_session, student=student, doc=doc, minute=0)
        reply = await a_stored_comment(db_session, attempt)
        answer = await a_stored_comment(
            db_session,
            attempt,
            author=CommentAuthor.MENTOR,
            kind=CommentKind.ANSWER,
            parent_id=reply.id,
            minute=1,
        )
        store = SqlRemarkCommentRepository(db_session)
        mark = ScreenMark(CommentTopic.ON_TOPIC, None, None)

        await store.mark_screened(
            tenant_id=seed_tenant.id, comment_id=answer.id, mark=mark
        )
        await store.mark_screened(
            tenant_id=uuid.uuid4(), comment_id=reply.id, mark=mark
        )

        topics = (
            await db_session.scalars(
                select(RemarkComment.topic).where(
                    RemarkComment.id.in_([reply.id, answer.id])
                )
            )
        ).all()
        assert topics == [None, None]
