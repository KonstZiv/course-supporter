"""The one definition of "the replies to answer", on real rows (task 12a, K3).

``pending_replies`` (``homework/remark_comments.py``) names the student's
replies no review answered yet to the reviews of ANY of their attempts at
the task read against the version in force — not of the previous attempt
alone (product owner's decision, 2026-11-20: a reply the screen could not
judge during one submission is picked up by the next). The state of a
submission, the reply screen, the rule of the same work and the source of
the Mentor's answers all read this one set. Here every filter of it is
pinned on rows: the version, the student, an answer, the Mentor's comments,
the live submission, the order, each reply once — and how many statements
it costs. Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from course_supporter.homework.remark_comments import pending_replies
from course_supporter.models.source import AssignmentType
from course_supporter.remark_kinds import CommentAuthor, CommentKind
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    Student,
    Tenant,
)
from tests._helpers.path_config import shipped_with_replies_for
from tests._helpers.remark_comments import a_stored_comment
from tests.integration.test_remark_comment_repository_db import (
    _T0,
    _V1,
    _V2,
    _attempt,
    _student,
    _task,
)

pytestmark = pytest.mark.requires_db

_REPLIES_ON = shipped_with_replies_for(AssignmentType.TASK)


async def _asking(
    session: AsyncSession, student: Student, doc: AuthoredDocument
) -> HomeworkSubmission:
    """The submission that asks: received, no review yet, the latest of all."""
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
        status="received",
        created_at=_T0 + timedelta(hours=1),
    )
    session.add(submission)
    await session.flush()
    return submission


async def _pending(
    session: AsyncSession, asking: HomeworkSubmission
) -> list[uuid.UUID]:
    replies = await pending_replies(
        session,
        tenant_id=asking.tenant_id,
        submission_id=asking.id,
        student_id=asking.student_id,
        authored_document_id=asking.authored_document_id,
        task_type=AssignmentType.TASK,
        config=_REPLIES_ON,
    )
    return [reply.id for reply in replies]


@pytest.fixture()
async def doc(db_session: AsyncSession, seed_root_node: CourseNode) -> AuthoredDocument:
    """A ``task`` whose version in force is ``_V1``."""
    return await _task(db_session, seed_root_node)


@pytest.fixture()
async def student(db_session: AsyncSession, seed_tenant: Tenant) -> Student:
    return await _student(db_session, seed_tenant)


class TestEveryAttemptAtTheVersionInForce:
    async def test_a_reply_to_an_older_attempt_is_picked_up(
        self, db_session: AsyncSession, doc: AuthoredDocument, student: Student
    ) -> None:
        """The finding of №686: the reply to the attempt before the previous."""
        older = await _attempt(db_session, student=student, doc=doc, minute=0)
        await _attempt(db_session, student=student, doc=doc, minute=1)
        waiting = await a_stored_comment(db_session, older)
        asking = await _asking(db_session, student, doc)

        assert await _pending(db_session, asking) == [waiting.id]

    async def test_several_attempts_each_reply_once_oldest_first(
        self, db_session: AsyncSession, doc: AuthoredDocument, student: Student
    ) -> None:
        first = await _attempt(db_session, student=student, doc=doc, minute=0)
        second = await _attempt(db_session, student=student, doc=doc, minute=1)
        third = await _attempt(db_session, student=student, doc=doc, minute=2)
        on_third = await a_stored_comment(db_session, third, minute=0)
        on_first = await a_stored_comment(db_session, first, minute=1)
        on_second = await a_stored_comment(db_session, second, item_id="c2", minute=2)
        on_first_later = await a_stored_comment(
            db_session, first, item_id="c3", minute=3
        )
        asking = await _asking(db_session, student, doc)

        pending = await _pending(db_session, asking)

        assert pending == [on_third.id, on_first.id, on_second.id, on_first_later.id]
        assert len(set(pending)) == len(pending)


class TestWhatIsNoReplyToAnswer:
    async def test_a_reply_to_an_attempt_of_another_version(
        self, db_session: AsyncSession, doc: AuthoredDocument, student: Student
    ) -> None:
        """The author changed the task: its remarks may be other criteria now."""
        before = await _attempt(
            db_session, student=student, doc=doc, minute=0, version=_V2
        )
        await a_stored_comment(db_session, before)
        await _attempt(db_session, student=student, doc=doc, minute=1)
        asking = await _asking(db_session, student, doc)

        assert await _pending(db_session, asking) == []

    async def test_an_attempt_with_no_recorded_version_has_none(
        self, db_session: AsyncSession, doc: AuthoredDocument, student: Student
    ) -> None:
        unversioned = await _attempt(db_session, student=student, doc=doc, minute=0)
        await a_stored_comment(db_session, unversioned, task_version=_V1)
        unversioned.task_version = None
        asking = await _asking(db_session, student, doc)

        assert await _pending(db_session, asking) == []

    async def test_an_answered_reply(
        self, db_session: AsyncSession, doc: AuthoredDocument, student: Student
    ) -> None:
        older = await _attempt(db_session, student=student, doc=doc, minute=0)
        previous = await _attempt(db_session, student=student, doc=doc, minute=1)
        await a_stored_comment(db_session, older, answered_in=previous.id)
        asking = await _asking(db_session, student, doc)

        assert await _pending(db_session, asking) == []

    async def test_another_students_reply(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        doc: AuthoredDocument,
        student: Student,
    ) -> None:
        """Another student's attempt at the same task and version."""
        other = await _student(db_session, seed_tenant)
        theirs = await _attempt(db_session, student=other, doc=doc, minute=0)
        await a_stored_comment(db_session, theirs)
        await _attempt(db_session, student=student, doc=doc, minute=1)
        asking = await _asking(db_session, student, doc)

        assert await _pending(db_session, asking) == []

    async def test_a_reply_on_a_soft_deleted_submission(
        self, db_session: AsyncSession, doc: AuthoredDocument, student: Student
    ) -> None:
        gone = await _attempt(db_session, student=student, doc=doc, minute=0)
        kept = await _attempt(db_session, student=student, doc=doc, minute=1)
        await a_stored_comment(db_session, gone)
        stays = await a_stored_comment(db_session, kept, minute=1)
        gone.deleted_at = _T0
        asking = await _asking(db_session, student, doc)

        assert await _pending(db_session, asking) == [stays.id]

    async def test_the_mentors_comment(
        self, db_session: AsyncSession, doc: AuthoredDocument, student: Student
    ) -> None:
        older = await _attempt(db_session, student=student, doc=doc, minute=0)
        previous = await _attempt(db_session, student=student, doc=doc, minute=1)
        waiting = await a_stored_comment(db_session, older, minute=0)
        await a_stored_comment(
            db_session,
            previous,
            author=CommentAuthor.MENTOR,
            kind=CommentKind.ANSWER,
            parent_id=waiting.id,
            minute=1,
        )
        asking = await _asking(db_session, student, doc)

        assert await _pending(db_session, asking) == [waiting.id]


class TestTheCost:
    async def test_three_statements_whatever_the_number_of_attempts(
        self,
        db_session: AsyncSession,
        async_engine: AsyncEngine,
        doc: AuthoredDocument,
        student: Student,
    ) -> None:
        """The version in force, the attempts at it, their replies — one each."""
        for minute in range(4):
            attempt = await _attempt(
                db_session, student=student, doc=doc, minute=minute
            )
            await a_stored_comment(db_session, attempt, minute=minute)
        asking = await _asking(db_session, student, doc)
        statements: list[str] = []

        def _count(*args: Any) -> None:
            statements.append(args[2])

        engine = async_engine.sync_engine
        event.listen(engine, "before_cursor_execute", _count)
        try:
            pending = await _pending(db_session, asking)
        finally:
            event.remove(engine, "before_cursor_execute", _count)

        assert len(pending) == 4
        assert len(statements) == 3, statements
