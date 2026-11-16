"""What a reply reads about its review, on a real database (task 12a, K3).

The core's decisions are pinned on doubles (``tests/unit/test_homework/
test_reply_core.py``); what they are decided FROM is a set of queries — which
later submissions there are, which remarks a review has, which attempt a copy
repeats — and those are facts about rows, so they are pinned here. The store
of comments is the in-memory double until K1's table is merged (phase 2).
Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework.attempt_dedup import DEDUP_RECEIPT_KEY
from course_supporter.homework.reply_core import (
    ReplyRefusal,
    post_reply,
    review_facts,
    review_items,
)
from course_supporter.models.source import AssignmentType
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    Student,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
    VerdictRecord,
)
from tests._helpers.path_config import shipped_with_replies_for
from tests._helpers.remark_comments import FakeRemarkComments

pytestmark = pytest.mark.requires_db

_VERSION = "a" * 64
_FINGERPRINT = "f" * 64
_T0 = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


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


class TestAReplyOverTheRealFacts:
    async def test_taken_on_the_latest_review_refused_on_the_one_before(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
    ) -> None:
        doc = await _task(db_session, seed_root_node)
        me = await _student(db_session, seed_tenant, "e2e")
        before = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=0
        )
        latest = await _submission(
            db_session, student=me, doc=doc, status="delivered", minute=1
        )
        await _judged(db_session, before)
        await _judged(db_session, latest)
        store = FakeRemarkComments()
        config = shipped_with_replies_for(AssignmentType.TASK)

        reply = await post_reply(
            db_session,
            submission=latest,
            student_id=me.id,
            item_id="c1.p2",
            kind="objection",
            body="Пункт 2 виконано: перевірка порожнього списку в рядку 3.",
            parent_id=None,
            comments=store,
            config=config,
        )
        with pytest.raises(HTTPException) as refused:
            await post_reply(
                db_session,
                submission=before,
                student_id=me.id,
                item_id="c1",
                kind="question",
                body="А цей?",
                parent_id=None,
                comments=store,
                config=config,
            )

        assert reply.submission_id == latest.id
        assert refused.value.status_code == 422
        # fastapi takes ``detail: Any``; starlette types the attribute ``str``.
        detail: dict[str, Any] = refused.value.detail  # type: ignore[assignment]
        assert detail == {"code": ReplyRefusal.NOT_LATEST_REVIEW.value, "details": {}}
