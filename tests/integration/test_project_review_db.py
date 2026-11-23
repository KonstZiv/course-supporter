"""A project's review, by the builder its type is given (project chain K6, lock a).

``get_result_builder(PROJECT)`` builds a project's review from what its stages
wrote -- recorded verdict rows, a recorded explanation and the course links
settled for it -- on a live database, exactly as it builds a ``task``'s: the
verdict, the phrase of the pass rule (``pass_decision``), the remarks and the
links. The one difference is the project's own: a "not met" whose row keeps
the place of its problem in the student's files (chain K5) prints it,
``*файл stats.py:4*``, above its remark.

Requires PostgreSQL (``--run-db``).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework.course_links import (
    CriterionLinks,
    FoundCourseLinks,
    LinkCandidate,
)
from course_supporter.homework.criteria_form import Criterion, criteria_to_document
from course_supporter.homework.result_builders import (
    BuildContext,
    BuiltResult,
    get_result_builder,
)
from course_supporter.homework.text_result import TextResultBuilder
from course_supporter.models.review_structure import Position
from course_supporter.models.source import AssignmentType
from course_supporter.storage.document_segment_repository import SegmentAnchor
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import AuthoredDocument, CourseNode, Tenant
from course_supporter.storage.student_repository import StudentRepository
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
    VerdictRecord,
)
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


def _criterion(cid: str, weight: str) -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": f"Criterion {cid}",
            "evidence": f"What shows {cid}",
            "weight": weight,
            "check_method": "model_verdict",
            "soft_descent": False,
            "concepts": ["recursion"],
            "mandatory_points": [],
        }
    )


# The author's edit: c1 a "must" the author made mandatory themselves
# (``must_by_author``, 2026-11-17), so the strict branch of the pass decides.
_CRITERIA = (
    _criterion("c1", "must").model_copy(update={"must_by_author": True}),
    _criterion("c2", "should").model_copy(update={"must_by_author": False}),
)

_QUOTE = "return sum(len(line) for line in lines) / len(lines)"


def _rows(*, placed: bool) -> tuple[VerdictRecord, ...]:
    """c1 "must" not met -- its problem placed in ``stats.py`` line 4 when
    ``placed``, as a project's row keeps it (K5); c2 "should" met."""
    return (
        VerdictRecord(
            "c1",
            VerdictItemKind.CRITERION,
            WeightCategory.MUST,
            VerdictValue.NOT_MET,
            VerdictValue.NOT_MET,
            quote=_QUOTE if placed else None,
            quote_file="stats.py" if placed else None,
            quote_lines=(4, 4) if placed else None,
            missing="Порожній список рядків не обробляється окремо.",
        ),
        VerdictRecord(
            "c2",
            VerdictItemKind.CRITERION,
            WeightCategory.SHOULD,
            VerdictValue.MET,
            VerdictValue.MET,
            quote="def mean_length(lines):",
            quote_file="stats.py",
            quote_lines=(1, 1),
        ),
    )


_EXPLANATION: dict[str, Any] = {
    "passed": False,
    "why": "Модуль статистики охайний, але порожній файл його ламає.",
    "remarks": [
        {
            "id": "c1",
            "what": "Середня довжина рахується діленням на нуль для порожнього файла.",
            "why": "Порожній файл -- звичайний вхід, і програма на ньому падає.",
            "todo": "Поверніть 0.0, якщо рядків немає.",
            "links": ["c1.l1"],
        }
    ],
    "mentor_voice": None,
}

_TITLE = "Рекурсія і краї входу"
_SEGMENT = uuid.UUID("00000000-0000-4000-8000-0000000000c1")


def _settled_links(material_id: uuid.UUID) -> dict[str, Any]:
    """What a first build settled for c1: one place of the course."""
    place = LinkCandidate(
        segment_id=_SEGMENT,
        authored_document_id=material_id,
        material_title=_TITLE,
        source_type="text",
        anchor=SegmentAnchor("paragraph", 2, 3),
        excerpt="Порожній вхід -- перший край, який варто перевірити.",
        later_in_course=False,
        confidence=0.9,
        matched_concepts=("recursion",),
    )
    found = CriterionLinks(item_id="c1", candidates=(place,))
    return FoundCourseLinks(criteria=(found,), settled=(found,)).model_dump(mode="json")


async def _review(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    tenant: Tenant,
    root: CourseNode,
    task_type: AssignmentType,
) -> BuiltResult:
    """A submission of ``task_type`` with its recorded rows, explanation and
    links, built by the builder its type is given."""
    document = AuthoredDocument(
        course_node_id=root.id,
        course_root_id=root.id,
        source_type="text",
        source_url=f"https://example.com/{uuid.uuid4().hex}",
        task_type=task_type.value,
    )
    session.add(document)
    await session.flush()
    student = await StudentRepository(session).create(
        tenant_id=tenant.id, external_id=f"ext-{uuid.uuid4().hex[:8]}"
    )
    submission = await HomeworkRepository(session).create(
        tenant_id=tenant.id,
        student_id=student.id,
        course_node_id=root.id,
        node_id=root.id,
        authored_document_id=document.id,
        file_url="s3://bucket/work.zip",
        file_type="application/zip",
        original_filename="work.zip",
    )
    edit = await TaskCriteriaOverrideRepository(session).replace(
        authored_document_id=document.id,
        source_content_hash="h" * 64,
        source_task_type=task_type.value,
        criteria=criteria_to_document(_CRITERIA),
    )
    stored = SubmissionVerdictRepository(session)
    await stored.replace_for_submission(
        tenant_id=tenant.id,
        submission_id=submission.id,
        criteria_layer=CriteriaLayer.AUTHOR,
        criteria_source_id=edit.id,
        records=_rows(placed=task_type is AssignmentType.PROJECT),
    )
    await stored.store_explanation(
        tenant_id=tenant.id,
        submission_id=submission.id,
        language="ukr",
        body=_EXPLANATION,
    )
    await stored.store_candidates(
        tenant_id=tenant.id,
        submission_id=submission.id,
        body=_settled_links(document.id),
    )
    await session.flush()

    builder = get_result_builder(task_type)
    assert builder is not None
    return await builder.build(
        BuildContext(
            session=session,
            session_factory=session_factory,
            submission=submission,
            submission_text="the door's text, unread by the builder",
            review_language="ukr",
            redis=None,
        )
    )


@pytest.fixture()
async def root(db_session: AsyncSession, seed_tenant: Tenant) -> CourseNode:
    node = make_root_course_node(tenant_id=seed_tenant.id, title="Course", order=0)
    db_session.add(node)
    await db_session.flush()
    return node


class TestAProjectsReview:
    def test_a_project_is_built_by_the_text_tasks_builder(self) -> None:
        assert isinstance(get_result_builder(AssignmentType.PROJECT), TextResultBuilder)

    async def test_the_same_review_as_a_tasks_with_the_place_of_its_problem(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        seed_tenant: Tenant,
        root: CourseNode,
    ) -> None:
        project = await _review(
            db_session, session_factory, seed_tenant, root, AssignmentType.PROJECT
        )
        task = await _review(
            db_session, session_factory, seed_tenant, root, AssignmentType.TASK
        )

        # The verdict and its rule: counted by pass_decision, an author's
        # "must" not met -- not passed, whatever the explanation says.
        verdict = project.structure.verdict
        assert verdict is not None
        assert verdict.passed is False
        assert verdict.rule is not None
        assert verdict.rule.basis == "author_musts_unmet"
        assert project.score == task.score
        # The remark: the explanation's three parts, the course link settled
        # for it, and -- the project's own -- the place of its problem.
        [remark] = project.structure.new_remarks
        assert remark.position == Position(kind="file", value="stats.py:4")
        assert [link.title for link in remark.read] == [_TITLE]
        # Everything else is the task's, field for field.
        [tasks_remark] = task.structure.new_remarks
        assert tasks_remark.position is None
        assert (
            project.structure.model_copy(
                update={"new_remarks": [remark.model_copy(update={"position": None})]}
            )
            == task.structure
        )

        markdown = project.markdown
        assert "Не зараховано" in markdown
        assert (
            "Не виконано обов'язкові критерії автора, тому роботу не "
            f"зараховано. Ваш бал — {project.score} зі 100." in markdown
        )
        assert (
            "*файл stats.py:4*\n\n- **Що не так:** Середня довжина рахується"
            in markdown
        )
        assert _TITLE in markdown
        # The task's review is the same text without the line of the place.
        assert markdown.replace("*файл stats.py:4*\n\n", "") == task.markdown
