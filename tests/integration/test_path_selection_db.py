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
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    Student,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.requires_db


def _config(*, served_by: ServedBy = ServedBy.NEW_PATH) -> PathConfig:
    """A config whose ``task`` type is on the new path, with three described states."""
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
                    "paths": {
                        "first": ["safety", "attempt_classifier"],
                        "repeat_without_replies": ["safety"],
                        "repeat_with_replies": ["safety"],
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

        Every type in the shipped file is on today's Mentor, so every type
        answers ``None`` — which is also the production fact this task ships
        with (acceptance criterion 13).
        """
        student = await StudentRepository(db_session).create(
            tenant_id=seed_tenant.id, external_id="ext-shipped-switch"
        )

        for task_type in AssignmentType:
            choice = await choose_path(
                db_session,
                task_type=task_type,
                student_id=student.id,
                authored_document_id=seed_material_entry.id,
            )
            assert choice is None, f"{task_type.value} is not on today's Mentor"

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

    async def test_the_replies_state_is_never_chosen(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Described, reachable only from task 12 — never from today's data.

        Both reachable arrangements are exercised: a student with no review and
        a student with one. Neither can produce the third state, because nothing
        in the data says a submission carries replies.
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
