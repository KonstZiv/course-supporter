"""What a reply reads about its review, on a real database (task 12a, K3).

The core's decisions are pinned on doubles (``tests/unit/test_homework/
test_reply_core.py``); what they are decided FROM is a set of queries — which
later submissions there are, which remarks a review has, which attempt a copy
repeats — and those are facts about rows, so they are pinned here. The store
of comments is the real one, over K1's table.
Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework.attempt_dedup import DEDUP_RECEIPT_KEY
from course_supporter.homework.remark_comments import (
    CommentRecord,
    build_remark_comment_repository,
)
from course_supporter.homework.reply_core import (
    ReplyRefusal,
    post_reply,
    read_threads,
    review_facts,
    review_items,
)
from course_supporter.models.source import AssignmentType
from course_supporter.remark_kinds import CommentAuthor, CommentKind
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    RemarkComment,
    Student,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
    VerdictRecord,
)
from tests._helpers.path_config import shipped_with_replies_for
from tests._helpers.remark_comments import a_stored_comment
from tests.integration.test_attempt_ledger_db import (
    _VERSION_A,
    _drop_world,
    _make_world,
)

pytestmark = pytest.mark.requires_db

_VERSION = "a" * 64
_FINGERPRINT = "f" * 64
_T0 = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
_REPLIES_ON = shipped_with_replies_for(AssignmentType.TASK)


async def _task(session: AsyncSession, root: CourseNode) -> AuthoredDocument:
    doc = AuthoredDocument(
        course_node_id=root.id,
        course_root_id=root.id,
        source_type="text",
        source_url="s3://bucket/task.md",
        task_type=AssignmentType.TASK.value,
        content_hash=_VERSION,
    )
    session.add(doc)
    await session.flush()
    return doc


async def _student(session: AsyncSession, tenant: Tenant, name: str) -> Student:
    return await StudentRepository(session).create(
        tenant_id=tenant.id, external_id=f"ext-reply-{name}-{uuid.uuid4().hex[:6]}"
    )


async def _submission(
    session: AsyncSession,
    *,
    student: Student,
    doc: AuthoredDocument,
    status: str,
    minute: int,
    task_version: str | None = _VERSION,
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
        status=status,
        review_markdown="## Рецензія" if status in {"completed", "delivered"} else None,
        task_version=task_version,
        created_at=_T0 + timedelta(minutes=minute),
        **columns,
    )
    session.add(submission)
    await session.flush()
    return submission


async def _judged(session: AsyncSession, submission: HomeworkSubmission) -> None:
    """Verdict rows on two criteria, the first with two points, written in order."""
    not_met = VerdictValue.NOT_MET
    await SubmissionVerdictRepository(session).replace_for_submission(
        tenant_id=submission.tenant_id,
        submission_id=submission.id,
        criteria_layer=CriteriaLayer.MODEL,
        criteria_source_id=uuid.uuid4(),
        records=[
            VerdictRecord(
                "c1", VerdictItemKind.CRITERION, WeightCategory.MUST, not_met, None
            ),
            VerdictRecord("c1.p1", VerdictItemKind.POINT, None, not_met, not_met),
            VerdictRecord("c1.p2", VerdictItemKind.POINT, None, not_met, not_met),
            VerdictRecord(
                "c2", VerdictItemKind.CRITERION, WeightCategory.MAY, not_met, not_met
            ),
        ],
    )


class TestTheFactsOfAReview:
    async def test_the_task_its_version_and_what_came_later(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        other_doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, "me")
        them = await _student(db_session, seed_tenant, "them")
        await _submission(db_session, student=me, doc=doc, status="delivered", minute=0)
        reviewed = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=1
        )
        await _submission(db_session, student=me, doc=doc, status="rejected", minute=2)
        await _submission(db_session, student=me, doc=doc, status="received", minute=3)
        gone = await _submission(
            db_session, student=me, doc=doc, status="completed", minute=4
        )
        gone.deleted_at = _T0
        await _submission(
            db_session, student=me, doc=other_doc, status="delivered", minute=5
        )
        await _submission(
            db_session, student=them, doc=doc, status="delivered", minute=6
        )
        await db_session.flush()

        facts = await review_facts(db_session, reviewed)

        assert facts.task_type is AssignmentType.TASK
        assert facts.task_version == _VERSION
        assert sorted(facts.later_statuses) == ["received", "rejected"], (
            "only this student's live submissions on this task, made later"
        )

    async def test_a_tie_in_time_is_broken_by_the_id(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """The attempt ledger's order: the same minute, the later id is later."""
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, "tie")
        first = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=0
        )
        second = await _submission(
            db_session, student=me, doc=doc, status="completed", minute=0
        )
        earlier, later = sorted((first, second), key=lambda s: s.id)

        assert (await review_facts(db_session, earlier)).later_statuses == (
            later.status,
        )
        assert (await review_facts(db_session, later)).later_statuses == ()


class TestTheRemarksOfAReview:
    async def test_the_verdict_rows_in_the_order_they_were_written(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, "rows")
        reviewed = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=0
        )
        await _judged(db_session, reviewed)

        assert await review_items(db_session, reviewed) == (
            "c1",
            "c1.p1",
            "c1.p2",
            "c2",
        )

    async def test_a_copy_has_the_remarks_of_the_attempt_it_repeats(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """The same work, given its earlier review: the student replies to it."""
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, "copy")
        source = await _submission(
            db_session,
            student=me,
            doc=doc,
            status="delivered",
            minute=0,
            attempt_no=1,
            fingerprint=_FINGERPRINT,
        )
        await _judged(db_session, source)
        other_work = await _submission(
            db_session,
            student=me,
            doc=doc,
            status="delivered",
            minute=1,
            attempt_no=2,
            fingerprint="0" * 64,
        )
        await _judged(db_session, other_work)
        copy = await _submission(
            db_session,
            student=me,
            doc=doc,
            status="delivered",
            minute=2,
            attempt_no=3,
            fingerprint=_FINGERPRINT,
            attempt_receipt={DEDUP_RECEIPT_KEY: 1},
        )

        assert await review_items(db_session, copy) == ("c1", "c1.p1", "c1.p2", "c2")

    async def test_a_copy_whose_attempt_is_gone_has_no_remarks(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, "gone")
        source = await _submission(
            db_session,
            student=me,
            doc=doc,
            status="delivered",
            minute=0,
            attempt_no=1,
            fingerprint=_FINGERPRINT,
        )
        await _judged(db_session, source)
        source.deleted_at = _T0
        copy = await _submission(
            db_session,
            student=me,
            doc=doc,
            status="delivered",
            minute=1,
            attempt_no=2,
            fingerprint=_FINGERPRINT,
            attempt_receipt={DEDUP_RECEIPT_KEY: 1},
        )
        await db_session.flush()

        assert await review_items(db_session, copy) == ()

    async def test_a_review_without_rows_and_without_a_copy_has_none(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, "none")
        reviewed = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=0
        )

        assert await review_items(db_session, reviewed) == ()


async def _post(
    session: AsyncSession,
    submission: HomeworkSubmission,
    *,
    item_id: str = "c1",
    body: str = "Чому критерій 1 не виконано? Перевірка є в рядку 3.",
    parent_id: uuid.UUID | None = None,
) -> CommentRecord:
    """A reply of the submission's student, through the core and the real store."""
    return await post_reply(
        session,
        submission=submission,
        student_id=submission.student_id,
        item_id=item_id,
        kind="question",
        body=body,
        parent_id=parent_id,
        comments=build_remark_comment_repository(session),
        config=_REPLIES_ON,
    )


async def _refusal(
    session: AsyncSession, submission: HomeworkSubmission, **kwargs: Any
) -> dict[str, Any]:
    with pytest.raises(HTTPException) as refused:
        await _post(session, submission, **kwargs)
    assert refused.value.status_code == 422
    # fastapi takes ``detail: Any``; starlette types the attribute ``str``.
    detail: dict[str, Any] = refused.value.detail  # type: ignore[assignment]
    return detail


async def _threads(
    session: AsyncSession, submission: HomeworkSubmission
) -> dict[str, list[uuid.UUID]]:
    threads = await read_threads(
        session,
        submission=submission,
        student_id=submission.student_id,
        comments=build_remark_comment_repository(session),
        config=_REPLIES_ON,
    )
    return {t.item_id: [c.id for c in t.comments] for t in threads}


async def _rows(session: AsyncSession, submission_id: uuid.UUID) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(RemarkComment)
        .where(RemarkComment.submission_id == submission_id)
    )
    return int(count or 0)


class TestTheLatestAttempt:
    async def test_taken_on_the_latest_review_refused_on_the_one_before(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, "latest")
        before = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=0
        )
        latest = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=1
        )
        await _judged(db_session, before)
        await _judged(db_session, latest)

        reply = await _post(db_session, latest, item_id="c1.p2")
        refused = await _refusal(db_session, before)

        assert reply.submission_id == latest.id
        assert refused == {"code": ReplyRefusal.NOT_LATEST_REVIEW.value, "details": {}}
        assert await _rows(db_session, before.id) == 0

    async def test_a_review_with_no_recorded_version_is_refused_not_broken(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """Before the attempt ledger: the core's refusal, never the database's.

        K1's table holds ``task_version`` NOT NULL; a reply to such a review
        would be an IntegrityError at the insert. It never gets there.
        """
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, "unversioned")
        old = await _submission(
            db_session,
            student=me,
            doc=doc,
            status="delivered",
            minute=0,
            task_version=None,
        )
        await _judged(db_session, old)

        refused = await _refusal(db_session, old)

        assert refused == {"code": ReplyRefusal.NOT_LATEST_REVIEW.value, "details": {}}
        assert await _rows(db_session, old.id) == 0
        assert await _threads(db_session, old) == {}

    @pytest.mark.parametrize(
        ("later", "taken"),
        [
            ("rejected", True),
            ("mismatch", True),
            ("failed", True),
            ("received", False),
            ("awaiting_funds", False),
            ("completed", False),
        ],
    )
    async def test_a_later_submission(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        later: str,
        taken: bool,
    ) -> None:
        """Ended without a review — no attempt, the reply goes here; else not."""
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, f"later-{later}")
        reviewed = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=0
        )
        await _judged(db_session, reviewed)
        await _submission(db_session, student=me, doc=doc, status=later, minute=1)

        if taken:
            assert (await _post(db_session, reviewed)).submission_id == reviewed.id
        else:
            refused = await _refusal(db_session, reviewed)
            assert refused["code"] == ReplyRefusal.NOT_LATEST_REVIEW.value


class TestTheLimitOnTheTable:
    async def test_five_on_a_remark_then_the_limit_and_another_remark_is_open(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, "five")
        reviewed = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=0
        )
        await _judged(db_session, reviewed)

        for n in range(5):
            await _post(db_session, reviewed, body=f"Репліка номер {n + 1}.")
        refused = await _refusal(db_session, reviewed, body="Шоста.")
        other = await _post(db_session, reviewed, item_id="c2", body="До c2.")

        assert refused == {
            "code": ReplyRefusal.COMMENT_LIMIT_REACHED.value,
            "details": {"max": 5},
        }
        assert other.item_id == "c2"
        assert await _rows(db_session, reviewed.id) == 6

    async def test_two_replies_at_once_take_the_last_place_once(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The race the lock is for: committed rows, two sessions, one place."""
        world = await _make_world(session_factory, task_type="task")
        try:
            async with session_factory() as session:
                submission = HomeworkSubmission(
                    tenant_id=world["tenant_id"],
                    student_id=world["student_id"],
                    course_node_id=world["node_id"],
                    node_id=world["node_id"],
                    authored_document_id=world["doc_id"],
                    file_url="s3://bucket/solution.py",
                    file_type="text/plain",
                    original_filename="solution.py",
                    delivery_mode="in_app",
                    status="delivered",
                    review_markdown="## Рецензія",
                    task_version=_VERSION_A,
                )
                session.add(submission)
                await session.flush()
                await _judged(session, submission)
                for minute in range(4):
                    await a_stored_comment(session, submission, minute=minute)
                await session.commit()
                submission_id = submission.id

            async with session_factory() as first, session_factory() as second:
                mine = await first.get(HomeworkSubmission, submission_id)
                theirs = await second.get(HomeworkSubmission, submission_id)
                assert mine is not None and theirs is not None
                # The first takes the fifth place and holds the lock, uncommitted.
                await _post(first, mine, body="П'ята з першої сесії.")
                racing = asyncio.create_task(
                    _post(second, theirs, body="П'ята з другої сесії.")
                )
                await asyncio.sleep(0.3)
                assert not racing.done(), "the second waits for the first's lock"
                await first.commit()
                with pytest.raises(HTTPException) as refused:
                    await racing
                await second.rollback()

            # fastapi takes ``detail: Any``; starlette types the attribute ``str``.
            detail: dict[str, Any] = refused.value.detail  # type: ignore[assignment]
            assert detail["code"] == ReplyRefusal.COMMENT_LIMIT_REACHED.value
            async with session_factory() as session:
                assert await _rows(session, submission_id) == 5
        finally:
            await _drop_world(session_factory, world)


class TestThreadsThroughTheAttempts:
    async def test_a_reply_its_answer_and_the_answer_to_it_across_two_attempts(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, "threads")
        first = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=0
        )
        await _judged(db_session, first)
        question = await _post(db_session, first, item_id="c1")
        # The next attempt is reviewed, and its review answers the reply (K4).
        second = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=1
        )
        await _judged(db_session, second)
        answer = await a_stored_comment(
            db_session,
            second,
            author=CommentAuthor.MENTOR,
            kind=CommentKind.ANSWER,
            parent_id=question.id,
            body="Перевірку порожнього списку не знайдено в рядку 3.",
            minute=None,
        )
        await db_session.execute(
            update(RemarkComment)
            .where(RemarkComment.id == question.id)
            .values(answered_in_submission_id=second.id)
        )

        follow_up = await _post(db_session, second, item_id="c1", parent_id=answer.id)

        expected = {"c1": [question.id, answer.id, follow_up.id]}
        assert await _threads(db_session, second) == expected
        assert await _threads(db_session, first) == expected, "the same thread"

    async def test_a_new_version_of_the_task_starts_the_threads_anew(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, "new-version")
        old = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=0
        )
        await _judged(db_session, old)
        on_old = await _post(db_session, old)
        doc.content_hash = "b" * 64
        new = await _submission(
            db_session,
            student=me,
            doc=doc,
            status="delivered",
            minute=1,
            task_version="b" * 64,
        )
        await _judged(db_session, new)

        refused = await _refusal(db_session, new, parent_id=on_old.id)

        assert await _threads(db_session, new) == {}
        assert refused["code"] == ReplyRefusal.PARENT_NOT_IN_THREAD.value


class TestDeletion:
    async def test_a_hard_deleted_attempt_takes_its_comments_and_its_answers(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        """K1's cascades, as the reads see them: rows gone, a reply unanswered."""
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, "hard")
        first = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=0
        )
        await _judged(db_session, first)
        question = await _post(db_session, first)
        second = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=1
        )
        await a_stored_comment(
            db_session,
            second,
            author=CommentAuthor.MENTOR,
            kind=CommentKind.ANSWER,
            parent_id=question.id,
            minute=None,
        )
        await db_session.execute(
            update(RemarkComment)
            .where(RemarkComment.id == question.id)
            .values(answered_in_submission_id=second.id)
        )
        me_id, first_id, second_id = me.id, first.id, second.id

        await db_session.execute(
            delete(HomeworkSubmission).where(HomeworkSubmission.id == second_id)
        )
        # The rows changed under the session's objects: read them anew.
        db_session.expire_all()

        (thread,) = await read_threads(
            db_session,
            submission=await db_session.get(HomeworkSubmission, first_id),
            student_id=me_id,
            comments=build_remark_comment_repository(db_session),
            config=_REPLIES_ON,
        )
        assert [c.id for c in thread.comments] == [question.id]
        assert thread.comments[0].answered_in_submission_id is None
        await db_session.execute(
            delete(HomeworkSubmission).where(HomeworkSubmission.id == first_id)
        )
        assert await _rows(db_session, first_id) == 0

    async def test_a_soft_deleted_attempt_hides_its_comments_and_keeps_them(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, "soft")
        first = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=0
        )
        await _judged(db_session, first)
        await _post(db_session, first)
        second = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=1
        )
        await _judged(db_session, second)
        on_second = await _post(db_session, second)
        first.deleted_at = _T0
        await db_session.flush()

        assert await _threads(db_session, second) == {"c1": [on_second.id]}
        assert await _rows(db_session, first.id) == 1
