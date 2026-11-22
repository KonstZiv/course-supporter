"""Choosing a submission's path against a real database (mentor-rebuild task 03).

The state half of the choice is a query, so it is pinned here rather than on a
double: what counts as "already reviewed" is a fact about rows, and a stub would
only repeat whatever the test author believed. Requires ``docker compose up -d``;
run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from course_supporter.homework.path_config import (
    PathConfig,
    PathKey,
    ServedBy,
    SubmissionState,
)
from course_supporter.homework.path_selection import (
    choose_path,
    resolve_submission_state,
)
from course_supporter.models.source import AssignmentType
from course_supporter.remark_kinds import CommentAuthor, CommentKind
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    Student,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository
from tests._helpers.remark_comments import a_stored_comment

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.requires_db


def _config(
    *, served_by: ServedBy = ServedBy.NEW_PATH, replies_enabled: bool = False
) -> PathConfig:
    """A config whose ``task`` type is on the new path, with three described states.

    ``replies_enabled`` switches on the students' replies of ``task`` (task
    12a, K3); its replies path is told apart by a stage of its own.
    """
    stage = {
        "deterministic": True,
        "prompt_ref": "prompts/safety_check/v1.md",
        "requires": [],
        "input_budget_ratio": None,
        "record_output": False,
        "ceilings": {"tool_steps": 0, "money_usd": 0.05, "output_tokens": 8192},
        "ladder": [
            {
                "provider": "anthropic",
                "model": "m",
                "reasoning": None,
                "max_output_tokens": None,
            }
        ],
    }
    return PathConfig.model_validate(
        {
            "stages": {"safety": stage, "attempt_classifier": stage},
            "task_types": {
                "test": {
                    "served_by": ServedBy.TODAYS_MENTOR.value,
                    "paths": {s.value: [] for s in SubmissionState},
                },
                "short_task": {
                    "served_by": ServedBy.TODAYS_MENTOR.value,
                    "paths": {},
                },
                "task": {
                    "served_by": served_by.value,
                    "replies_enabled": replies_enabled,
                    "paths": {
                        "first": ["safety", "attempt_classifier"],
                        "repeat_without_replies": ["safety"],
                        "repeat_with_replies": ["safety", "attempt_classifier"],
                    },
                },
                "project": {"served_by": ServedBy.TODAYS_MENTOR.value, "paths": {}},
            },
        }
    )


async def _submit(
    repo: HomeworkRepository,
    *,
    tenant: Tenant,
    student: Student,
    root: CourseNode,
    doc: AuthoredDocument,
    status: str,
) -> HomeworkSubmission:
    submission = await repo.create(
        tenant_id=tenant.id,
        student_id=student.id,
        course_node_id=root.id,
        node_id=root.id,
        authored_document_id=doc.id,
        file_url=f"s3://bucket/{status}.py",
        file_type="text/plain",
        original_filename=f"{status}.py",
        delivery_mode="in_app",
    )
    submission.status = status
    await repo._session.flush()
    return submission


class TestSubmissionState:
    @pytest.mark.parametrize("status", ["completed", "delivered"])
    async def test_a_reviewed_revision_makes_the_next_one_a_repeat(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
        status: str,
    ) -> None:
        student = await StudentRepository(db_session).create(
            tenant_id=seed_tenant.id, external_id=f"ext-repeat-{status}"
        )
        await _submit(
            HomeworkRepository(db_session),
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
            status=status,
        )

        state = await resolve_submission_state(
            db_session,
            student_id=student.id,
            authored_document_id=seed_material_entry.id,
        )

        assert state is SubmissionState.REPEAT_WITHOUT_REPLIES

    @pytest.mark.parametrize(
        "status", ["received", "safety_ok", "rejected", "mismatch", "failed"]
    )
    async def test_anything_short_of_a_review_leaves_it_a_first(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
        status: str,
    ) -> None:
        """A refusal at the door does not spend the student's first attempt."""
        student = await StudentRepository(db_session).create(
            tenant_id=seed_tenant.id, external_id=f"ext-first-{status}"
        )
        await _submit(
            HomeworkRepository(db_session),
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
            status=status,
        )

        state = await resolve_submission_state(
            db_session,
            student_id=student.id,
            authored_document_id=seed_material_entry.id,
        )

        assert state is SubmissionState.FIRST

    async def test_a_soft_deleted_review_does_not_count(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """A review that is no longer there cannot make the next one a repeat.

        Skipping the classifier on the strength of a deleted row would be the
        path trusting something the rest of the system treats as gone (KD3).
        """
        student = await StudentRepository(db_session).create(
            tenant_id=seed_tenant.id, external_id="ext-soft-deleted"
        )
        reviewed = await _submit(
            HomeworkRepository(db_session),
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
            status="completed",
        )
        reviewed.deleted_at = datetime.now(UTC)
        await db_session.flush()

        state = await resolve_submission_state(
            db_session,
            student_id=student.id,
            authored_document_id=seed_material_entry.id,
        )

        assert state is SubmissionState.FIRST

    async def test_another_student_review_does_not_count(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        theirs = await StudentRepository(db_session).create(
            tenant_id=seed_tenant.id, external_id="ext-theirs"
        )
        mine = await StudentRepository(db_session).create(
            tenant_id=seed_tenant.id, external_id="ext-mine"
        )
        await _submit(
            HomeworkRepository(db_session),
            tenant=seed_tenant,
            student=theirs,
            root=seed_root_node,
            doc=seed_material_entry,
            status="delivered",
        )

        state = await resolve_submission_state(
            db_session,
            student_id=mine.id,
            authored_document_id=seed_material_entry.id,
        )

        assert state is SubmissionState.FIRST

    async def test_a_review_on_another_task_does_not_count(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        student = await StudentRepository(db_session).create(
            tenant_id=seed_tenant.id, external_id="ext-other-task"
        )
        await _submit(
            HomeworkRepository(db_session),
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
            status="delivered",
        )

        state = await resolve_submission_state(
            db_session,
            student_id=student.id,
            authored_document_id=uuid.uuid4(),
        )

        assert state is SubmissionState.FIRST


class TestChoosePath:
    async def test_todays_mentor_returns_nothing_and_asks_the_database_nothing(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """A type on today's Mentor costs exactly what it cost before task 03."""
        student = await StudentRepository(db_session).create(
            tenant_id=seed_tenant.id, external_id="ext-todays"
        )

        choice = await choose_path(
            db_session,
            task_type=AssignmentType.TASK,
            student_id=student.id,
            authored_document_id=seed_material_entry.id,
            config=_config(served_by=ServedBy.TODAYS_MENTOR),
        )

        assert choice is None

    async def test_the_shipped_switch_is_read_through_the_cached_config(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Without an explicit config the choice reads what the boot validated.

        Since task 07 the shipped file puts ``test`` on the new path, and a
        first submission of it walks ``test/first`` with no stage. Since task
        10 (K5) ``task`` and ``short_task`` are on the new path too, in test
        mode, and a first submission walks their five stages; since project
        chain K8 ``project`` is as well, and walks its own five. No type
        answers ``None`` any more. Before task 07 every type answered
        ``None`` — the production fact of task 03's acceptance criterion 13.
        """
        student = await StudentRepository(db_session).create(
            tenant_id=seed_tenant.id, external_id="ext-shipped-switch"
        )

        chosen = {
            task_type: await choose_path(
                db_session,
                task_type=task_type,
                student_id=student.id,
                authored_document_id=seed_material_entry.id,
            )
            for task_type in AssignmentType
        }

        test_choice = chosen.pop(AssignmentType.TEST)
        assert test_choice is not None, "test is on the new path"
        assert test_choice.key == PathKey(AssignmentType.TEST, SubmissionState.FIRST)
        assert test_choice.stages == ()
        text_path = (
            "safety",
            "attempt_classifier",
            "criteria_evaluation",
            "course_link_lookup",
            "review_explanation",
        )
        for text_type in (AssignmentType.TASK, AssignmentType.SHORT_TASK):
            text_choice = chosen.pop(text_type)
            assert text_choice is not None, f"{text_type} is on the new path"
            assert text_choice.key == PathKey(text_type, SubmissionState.FIRST)
            assert text_choice.stages == text_path
        project_choice = chosen.pop(AssignmentType.PROJECT)
        assert project_choice is not None, "project is on the new path (K8)"
        assert project_choice.key == PathKey(
            AssignmentType.PROJECT, SubmissionState.FIRST
        )
        assert project_choice.stages == (
            "safety",
            "attempt_classifier",
            "project_criteria_evaluation",
            "course_link_lookup",
            "project_review_explanation",
        )
        assert chosen == {}

    async def test_first_submission_walks_the_first_path(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        student = await StudentRepository(db_session).create(
            tenant_id=seed_tenant.id, external_id="ext-choose-first"
        )

        choice = await choose_path(
            db_session,
            task_type=AssignmentType.TASK,
            student_id=student.id,
            authored_document_id=seed_material_entry.id,
            config=_config(),
        )

        assert choice is not None
        assert choice.key == PathKey(AssignmentType.TASK, SubmissionState.FIRST)
        assert choice.stages == ("safety", "attempt_classifier")

    async def test_repeat_submission_walks_the_repeat_path(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        student = await StudentRepository(db_session).create(
            tenant_id=seed_tenant.id, external_id="ext-choose-repeat"
        )
        await _submit(
            HomeworkRepository(db_session),
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
            status="completed",
        )

        choice = await choose_path(
            db_session,
            task_type=AssignmentType.TASK,
            student_id=student.id,
            authored_document_id=seed_material_entry.id,
            config=_config(),
        )

        assert choice is not None
        assert choice.key == PathKey(
            AssignmentType.TASK, SubmissionState.REPEAT_WITHOUT_REPLIES
        )
        assert choice.stages == ("safety",)

    async def test_without_replies_the_replies_state_is_never_chosen(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Today's data, today's file: no type takes replies, none is chosen.

        Until task 12a this lock read "never from today's data"; K3 makes the
        third state reachable, and only so (``TestTheRepliesState`` below).
        Both reachable arrangements are exercised: a student with no review
        and a student with one.
        """
        student = await StudentRepository(db_session).create(
            tenant_id=seed_tenant.id, external_id="ext-never-replies"
        )
        chosen = []
        for status in (None, "delivered"):
            if status is not None:
                await _submit(
                    HomeworkRepository(db_session),
                    tenant=seed_tenant,
                    student=student,
                    root=seed_root_node,
                    doc=seed_material_entry,
                    status=status,
                )
            chosen.append(
                await resolve_submission_state(
                    db_session,
                    student_id=student.id,
                    authored_document_id=seed_material_entry.id,
                )
            )

        assert chosen == [
            SubmissionState.FIRST,
            SubmissionState.REPEAT_WITHOUT_REPLIES,
        ]
        assert SubmissionState.REPEAT_WITH_REPLIES not in chosen


class TestTheRepliesState:
    """Task 12a, K3: the third state — the switch on, and replies to answer.

    On real rows: the attempts, the task's version, and the replies in K1's
    table, read by the store the body reads them by.
    """

    _VERSION = "the-version-in-force"

    async def _a_repeat(
        self,
        session: AsyncSession,
        tenant: Tenant,
        root: CourseNode,
        doc: AuthoredDocument,
        name: str,
    ) -> tuple[Student, HomeworkSubmission, HomeworkSubmission]:
        """A reviewed attempt at the version in force, and the next submission."""
        doc.content_hash = self._VERSION
        student = await StudentRepository(session).create(
            tenant_id=tenant.id, external_id=f"ext-replies-{name}"
        )
        repo = HomeworkRepository(session)
        reviewed = await _submit(
            repo, tenant=tenant, student=student, root=root, doc=doc, status="delivered"
        )
        reviewed.task_version = self._VERSION
        current = await _submit(
            repo, tenant=tenant, student=student, root=root, doc=doc, status="received"
        )
        await session.flush()
        return student, reviewed, current

    @staticmethod
    async def _state(
        session: AsyncSession,
        student: Student,
        doc: AuthoredDocument,
        current: HomeworkSubmission,
        *,
        replies_enabled: bool = True,
        named: bool = True,
    ) -> SubmissionState:
        return await resolve_submission_state(
            session,
            student_id=student.id,
            authored_document_id=doc.id,
            submission_id=current.id if named else None,
            tenant_id=current.tenant_id if named else None,
            task_type=AssignmentType.TASK if named else None,
            config=_config(replies_enabled=replies_enabled),
        )

    async def test_replies_to_answer_make_the_replies_state(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        student, reviewed, current = await self._a_repeat(
            db_session, seed_tenant, seed_root_node, seed_material_entry, "yes"
        )
        await a_stored_comment(db_session, reviewed)

        state = await self._state(db_session, student, seed_material_entry, current)

        assert state is SubmissionState.REPEAT_WITH_REPLIES

    async def test_the_choice_walks_the_replies_path(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        student, reviewed, current = await self._a_repeat(
            db_session, seed_tenant, seed_root_node, seed_material_entry, "path"
        )
        await a_stored_comment(db_session, reviewed)

        choice = await choose_path(
            db_session,
            task_type=AssignmentType.TASK,
            student_id=student.id,
            authored_document_id=seed_material_entry.id,
            config=_config(replies_enabled=True),
            submission_id=current.id,
            tenant_id=current.tenant_id,
        )

        assert choice is not None
        assert choice.key == PathKey(
            AssignmentType.TASK, SubmissionState.REPEAT_WITH_REPLIES
        )
        assert choice.stages == ("safety", "attempt_classifier")

    async def test_the_switch_off_never_chooses_it(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Replies left from a day the switch was on: no answer comes, no state."""
        student, reviewed, current = await self._a_repeat(
            db_session, seed_tenant, seed_root_node, seed_material_entry, "off"
        )
        await a_stored_comment(db_session, reviewed)

        state = await self._state(
            db_session, student, seed_material_entry, current, replies_enabled=False
        )

        assert state is SubmissionState.REPEAT_WITHOUT_REPLIES

    async def test_answered_replies_and_the_mentors_answers_do_not_count(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        student, reviewed, current = await self._a_repeat(
            db_session, seed_tenant, seed_root_node, seed_material_entry, "answered"
        )
        question = await a_stored_comment(db_session, reviewed, answered_in=reviewed.id)
        await a_stored_comment(
            db_session,
            reviewed,
            author=CommentAuthor.MENTOR,
            kind=CommentKind.ANSWER,
            parent_id=question.id,
            minute=1,
        )

        state = await self._state(db_session, student, seed_material_entry, current)

        assert state is SubmissionState.REPEAT_WITHOUT_REPLIES

    async def test_replies_to_an_earlier_review_than_the_last_do_not_count(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """The submission answers the replies to its previous attempt alone."""
        student, reviewed, _ = await self._a_repeat(
            db_session, seed_tenant, seed_root_node, seed_material_entry, "earlier"
        )
        await a_stored_comment(db_session, reviewed)
        repo = HomeworkRepository(db_session)
        last = await _submit(
            repo,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
            status="completed",
        )
        last.task_version = self._VERSION
        later = await _submit(
            repo,
            tenant=seed_tenant,
            student=student,
            root=seed_root_node,
            doc=seed_material_entry,
            status="received",
        )

        state = await self._state(db_session, student, seed_material_entry, later)

        assert state is SubmissionState.REPEAT_WITHOUT_REPLIES

    async def test_replies_to_a_review_of_another_version_do_not_count(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """The author changed the task: its remarks may be other criteria now."""
        student, reviewed, current = await self._a_repeat(
            db_session, seed_tenant, seed_root_node, seed_material_entry, "version"
        )
        reviewed.task_version = "the-version-the-review-read"
        await a_stored_comment(db_session, reviewed)

        state = await self._state(db_session, student, seed_material_entry, current)

        assert state is SubmissionState.REPEAT_WITHOUT_REPLIES

    async def test_a_caller_that_names_no_submission_is_not_asked_about_them(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """The price quote: no submission yet, no replies asked."""
        student, reviewed, current = await self._a_repeat(
            db_session, seed_tenant, seed_root_node, seed_material_entry, "quote"
        )
        await a_stored_comment(db_session, reviewed)

        state = await self._state(
            db_session, student, seed_material_entry, current, named=False
        )

        assert state is SubmissionState.REPEAT_WITHOUT_REPLIES
