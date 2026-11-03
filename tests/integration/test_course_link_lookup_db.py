"""The path's search for course links against a live database (task 10, K5).

Lock b: the step keeps places for the failed criteria alone; with none failed
it keeps an empty result and never asks the finder; a repeat run replaces the
row. The course, the list, the verdicts and the finder are the real ones —
the finder is swapped for a spy only where what it is ASKED is the point.

Requires PostgreSQL (``--run-db``). One course: the task on ``lesson``, a
material before it on the same lesson teaching "recursion".
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework.course_link_position import position_of
from course_supporter.homework.course_links import (
    FailedCriterion,
    FoundCourseLinks,
    LinkCandidate,
    SubmissionScope,
    look_up_course_links,
)
from course_supporter.homework.criteria_form import Criterion, criteria_to_document
from course_supporter.models.review_structure import Position
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    HomeworkSubmission,
    SubmissionCourseLinkCandidates,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
    VerdictRecord,
)
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)
from tests.integration.test_document_segment_repository import (
    _seed_child,
    _seed_material,
    _seed_root,
    _seed_segment,
    _seed_summary,
)

pytestmark = pytest.mark.requires_db

_FINDER = "course_supporter.homework.course_links.build_course_link_finder"


def _criterion(cid: str, weight: str, concepts: list[str]) -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": f"Criterion {cid}",
            "evidence": f"What shows {cid}",
            "weight": weight,
            "check_method": "model_verdict",
            "soft_descent": False,
            "concepts": concepts,
            "mandatory_points": [],
        }
    )


# c1 and c3 are about recursion, which the course teaches; c2 about closures,
# which it does not.
_CRITERIA = (
    _criterion("c1", "must", ["recursion"]),
    _criterion("c2", "should", ["closure"]),
    _criterion("c3", "may", ["Recursion"]),
)
_WEIGHTS = {"c1": WeightCategory.MUST, "c2": WeightCategory.SHOULD}


def _records(*, unmet: Sequence[str]) -> tuple[VerdictRecord, ...]:
    weights = {**_WEIGHTS, "c3": WeightCategory.MAY}
    return tuple(
        VerdictRecord(
            c.id,
            VerdictItemKind.CRITERION,
            weights[c.id],
            VerdictValue.NOT_MET if c.id in unmet else VerdictValue.MET,
            VerdictValue.NOT_MET if c.id in unmet else VerdictValue.MET,
            quote=None if c.id in unmet else "def fib(n):",
            quote_file=None if c.id in unmet else "solution.py",
            quote_lines=None if c.id in unmet else (1, 1),
            missing="Немає." if c.id in unmet else None,
        )
        for c in _CRITERIA
    )


@dataclass
class _World:
    root: CourseNode
    lesson: CourseNode
    task: AuthoredDocument
    segment: DocumentSegment
    submission: HomeworkSubmission
    list_id: uuid.UUID


@pytest.fixture()
async def world(db_session: AsyncSession, seed_tenant: Tenant) -> _World:
    root = await _seed_root(db_session, seed_tenant)
    lesson = await _seed_child(db_session, seed_tenant, root, order=1)
    material = await _seed_material(db_session, lesson, root, order=0)
    summary = await _seed_summary(db_session, material, root, title="Рекурсія")
    segment = await _seed_segment(
        db_session,
        summary,
        root,
        main=["recursion"],
        content="Рекурсія — це коли функція викликає сама себе.",
        start_paragraph=3,
        end_paragraph=4,
    )
    task = await _seed_material(db_session, lesson, root, task_type="task", order=5)
    student = await StudentRepository(db_session).create(
        tenant_id=seed_tenant.id, external_id=f"ext-{uuid.uuid4().hex[:8]}"
    )
    submission = await HomeworkRepository(db_session).create(
        tenant_id=seed_tenant.id,
        student_id=student.id,
        course_node_id=root.id,
        node_id=lesson.id,
        authored_document_id=task.id,
        file_url="s3://bucket/solution.py",
        file_type="text/plain",
        original_filename="solution.py",
    )
    edit = await TaskCriteriaOverrideRepository(db_session).replace(
        authored_document_id=task.id,
        source_content_hash="h" * 64,
        source_task_type="task",
        criteria=criteria_to_document(_CRITERIA),
    )
    return _World(root, lesson, task, segment, submission, edit.id)


async def _verdicts(
    session: AsyncSession, world: _World, *, unmet: Sequence[str]
) -> None:
    await SubmissionVerdictRepository(session).replace_for_submission(
        tenant_id=world.submission.tenant_id,
        submission_id=world.submission.id,
        criteria_layer=CriteriaLayer.AUTHOR,
        criteria_source_id=world.list_id,
        records=_records(unmet=unmet),
    )


async def _kept(session: AsyncSession, world: _World) -> FoundCourseLinks | None:
    row = await SubmissionVerdictRepository(session).get_candidates(
        tenant_id=world.submission.tenant_id, submission_id=world.submission.id
    )
    return None if row is None else FoundCourseLinks.model_validate(row.body)


@dataclass
class _SpyFinder:
    """Records what it is asked; answers nothing for every criterion."""

    asked: list[tuple[SubmissionScope, list[FailedCriterion]]] = field(
        default_factory=list
    )

    async def find(
        self, scope: SubmissionScope, failed: Sequence[FailedCriterion]
    ) -> Mapping[str, tuple[LinkCandidate, ...]]:
        self.asked.append((scope, list(failed)))
        return {criterion.id: () for criterion in failed}


class TestOnlyTheFailedCriteriaAreLinked:
    async def test_the_places_of_the_failed_criteria_are_kept(
        self, db_session: AsyncSession, world: _World
    ) -> None:
        """The real finder: c1 is taught, c2 is not, c3 is met and not asked."""
        await _verdicts(db_session, world, unmet=("c1", "c2"))

        found = await look_up_course_links(db_session, world.submission)

        assert [c.item_id for c in found.criteria] == ["c1", "c2"]
        (link,) = found.candidates_of("c1")
        assert link.segment_id == world.segment.id
        assert link.material_title == "Рекурсія"
        assert link.excerpt == "Рекурсія — це коли функція викликає сама себе."
        assert link.later_in_course is False
        assert position_of(link.anchor, link.source_type) == Position(
            kind="paragraph", value="3"
        )
        assert found.candidates_of("c2") == ()
        assert found.candidates_of("c3") == ()
        assert await _kept(db_session, world) == found

    async def test_the_finder_is_asked_about_the_failed_criteria_alone(
        self,
        db_session: AsyncSession,
        world: _World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Their texts and concepts, and the submission's own scope."""
        spy = _SpyFinder()
        monkeypatch.setattr(_FINDER, lambda _session: spy)
        await _verdicts(db_session, world, unmet=("c1", "c2"))

        await look_up_course_links(db_session, world.submission)

        ((scope, failed),) = spy.asked
        assert scope == SubmissionScope(
            tenant_id=world.submission.tenant_id,
            course_root_id=world.root.id,
            task_node_id=world.lesson.id,
            task_document_id=world.task.id,
        )
        assert failed == [
            FailedCriterion(id="c1", text="Criterion c1", concepts=("recursion",)),
            FailedCriterion(id="c2", text="Criterion c2", concepts=("closure",)),
        ]


class TestNothingFailed:
    async def test_an_empty_result_is_kept_and_the_finder_is_never_asked(
        self,
        db_session: AsyncSession,
        world: _World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        spy = _SpyFinder()
        monkeypatch.setattr(_FINDER, lambda _session: spy)
        await _verdicts(db_session, world, unmet=())

        found = await look_up_course_links(db_session, world.submission)

        assert found == FoundCourseLinks(criteria=())
        assert spy.asked == []
        assert await _kept(db_session, world) == FoundCourseLinks(criteria=())


class TestARepeatRun:
    async def test_it_replaces_the_row(
        self, db_session: AsyncSession, world: _World
    ) -> None:
        """A stage run again — after a crash before its checkpoint, or on a
        later revision's verdicts — leaves one row, the latest."""
        await _verdicts(db_session, world, unmet=("c1",))
        first = await look_up_course_links(db_session, world.submission)
        assert [c.item_id for c in first.criteria] == ["c1"]

        await _verdicts(db_session, world, unmet=())
        await look_up_course_links(db_session, world.submission)

        count = await db_session.scalar(
            select(func.count())
            .select_from(SubmissionCourseLinkCandidates)
            .where(SubmissionCourseLinkCandidates.submission_id == world.submission.id)
        )
        assert count == 1
        assert await _kept(db_session, world) == FoundCourseLinks(criteria=())


class TestWithoutVerdicts:
    async def test_no_rows_is_a_defect_and_keeps_nothing(
        self, db_session: AsyncSession, world: _World
    ) -> None:
        with pytest.raises(ValueError, match="has none"):
            await look_up_course_links(db_session, world.submission)

        assert await _kept(db_session, world) is None
