"""Build MentorContext from available data for the Mentor review pipeline.

Assembles all available information about the submission, task, course,
and student history into a single context object. Empty/missing fields
are None — the prompt builder includes only what is present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.homework.language import detect_response_language
from course_supporter.security.schemas import SubmissionContent
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.editable_repository import EditableRepository
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.student_repository import StudentRepository

if TYPE_CHECKING:
    from course_supporter.storage.orm import HomeworkSubmission

logger = structlog.get_logger()


@dataclass
class PastSubmissionSummary:
    """Compact summary of a previous submission for context."""

    task_title: str
    score: int | None
    passed: bool | None
    issues_summary: list[str]
    notable_solutions_summary: list[str]


@dataclass
class MentorContext:
    """All available data for Mentor review, assembled from DB.

    Fields are None/empty when data is unavailable — the prompt
    builder only includes non-empty sections.
    """

    # Always present
    submission_content: str
    task_title: str
    node_type: str
    response_language: str

    # Task details (from StructureNodeEditable)
    task_description: str | None = None
    learning_goal: str | None = None
    difficulty: str | None = None
    estimated_duration: int | None = None
    success_criteria: str | None = None
    assessment_method: str | None = None

    # Methodist output (from methodological_content JSONB)
    learning_objectives: list[str] = field(default_factory=list)
    grading_criteria: str | None = None
    assignment_description: str | None = None
    assignment_steps: list[str] = field(default_factory=list)
    common_mistakes: list[str] = field(default_factory=list)
    common_misconceptions: list[str] = field(default_factory=list)
    key_concepts: list[dict[str, str]] = field(default_factory=list)
    teaching_recommendations: str | None = None

    # Student history
    student_history: list[PastSubmissionSummary] = field(default_factory=list)

    # Course info
    course_title: str | None = None
    course_description: str | None = None


class MethodistFields(TypedDict, total=False):
    """Typed return value for _extract_methodist_fields."""

    learning_objectives: list[str]
    common_misconceptions: list[str]
    teaching_recommendations: str | None
    key_concepts: list[dict[str, str]]
    grading_criteria: str | None
    assignment_description: str | None
    assignment_steps: list[str]


def _extract_methodist_fields(
    methodological_content: dict[str, Any] | None,
    task_title: str | None,
) -> MethodistFields:
    """Extract relevant fields from MethodistNodeOutput JSONB.

    Matches the assignment recommendation by title when possible,
    falls back to the first assignment otherwise.
    """
    result = MethodistFields()
    if not methodological_content:
        return result

    result["learning_objectives"] = methodological_content.get(
        "learning_objectives",
        [],
    )
    result["common_misconceptions"] = methodological_content.get(
        "common_misconceptions",
        [],
    )
    result["teaching_recommendations"] = methodological_content.get(
        "teaching_recommendations",
    )

    # Extract key concepts (simplified)
    raw_concepts = methodological_content.get("key_concepts_detailed", [])
    result["key_concepts"] = [
        {"name": c.get("name", ""), "definition": c.get("definition", "")}
        for c in raw_concepts
        if c.get("name")
    ]

    # Find matching assignment recommendation
    assignments = methodological_content.get("recommended_assignments", [])
    if assignments:
        assignment = _find_assignment(assignments, task_title)
        result["grading_criteria"] = assignment.get("grading_criteria")
        result["assignment_description"] = assignment.get("description")
        result["assignment_steps"] = assignment.get("steps", [])

    return result


def _find_assignment(
    assignments: list[dict[str, Any]],
    task_title: str | None,
) -> dict[str, Any]:
    """Find the assignment matching task_title, or fall back to first."""
    if task_title:
        title_lower = task_title.lower()
        for assignment in assignments:
            if assignment.get("title", "").lower() == title_lower:
                return assignment
    return assignments[0]


def _build_student_history(
    past_submissions: list[HomeworkSubmission],
    *,
    max_entries: int = 5,
) -> list[PastSubmissionSummary]:
    """Build compact history from past submissions.

    Extracts key info from review_result JSONB if available.
    Malformed entries are skipped to avoid breaking the pipeline.
    """
    history: list[PastSubmissionSummary] = []
    for sub in past_submissions[:max_entries]:
        try:
            history.append(_parse_submission_summary(sub))
        except Exception:
            logger.warning(
                "student_history_parse_error",
                submission_id=getattr(sub, "id", None),
            )

    return history


def _parse_submission_summary(sub: HomeworkSubmission) -> PastSubmissionSummary:
    """Parse a single submission into a history summary.

    Raises on malformed data — caller decides whether to skip.
    """
    review = sub.review_result or {}
    analysis = review.get("analysis", {})
    matched = sub.matched_task
    task_title = matched.title if matched else "(unknown)"

    raw_issues = analysis.get("issues", [])
    issues_summary = (
        [i["description"] for i in raw_issues[:3] if isinstance(i, dict)]
        if isinstance(raw_issues, list)
        else []
    )

    raw_notable = analysis.get("notable_solutions", [])
    notable_summary = (
        [n["description"] for n in raw_notable[:3] if isinstance(n, dict)]
        if isinstance(raw_notable, list)
        else []
    )

    return PastSubmissionSummary(
        task_title=task_title,
        score=analysis.get("score"),
        passed=analysis.get("passed"),
        issues_summary=issues_summary,
        notable_solutions_summary=notable_summary,
    )


async def build_mentor_context(
    *,
    submission_content: SubmissionContent,
    submission: HomeworkSubmission,
    session: AsyncSession,
) -> MentorContext:
    """Assemble MentorContext from all available sources.

    Loads the matched task, Methodist output, course info,
    student history, and detects response language.

    Args:
        submission_content: Extracted text content from the submission.
        submission: HomeworkSubmission ORM object.
        session: Active DB session.

    Returns:
        Populated MentorContext ready for prompt building.
    """
    editable_repo = EditableRepository(session)
    node_repo = CourseNodeRepository(session)
    hw_repo = HomeworkRepository(session)
    student_repo = StudentRepository(session)

    # Load matched task (StructureNodeEditable)
    task = None
    if submission.matched_task_id:
        task = await editable_repo.get_by_id(submission.matched_task_id)

    # Load course node
    course_node = await node_repo.get_by_id(submission.course_node_id)

    # Load student
    student = await student_repo.get_by_id(submission.student_id)

    # Language detection
    response_language = detect_response_language(
        request_language=submission.response_language,
        student=student,
        submission_content=submission_content,
        course_node=course_node,
    )

    # Base context
    ctx = MentorContext(
        submission_content=submission_content.full_text,
        task_title=task.title if task else "(task not identified)",
        node_type=task.node_type if task else "unknown",
        response_language=response_language,
        course_title=course_node.title if course_node else None,
        course_description=course_node.description if course_node else None,
    )

    # Enrich from task
    if task:
        ctx.task_description = task.description
        ctx.learning_goal = task.learning_goal
        ctx.difficulty = task.difficulty
        ctx.estimated_duration = task.estimated_duration
        ctx.success_criteria = task.success_criteria
        ctx.assessment_method = task.assessment_method
        ctx.common_mistakes = task.common_mistakes or []

        # Methodist output
        methodist = _extract_methodist_fields(
            task.methodological_content,
            task.title,
        )
        ctx.learning_objectives = methodist.get("learning_objectives", [])
        ctx.grading_criteria = methodist.get("grading_criteria")
        ctx.assignment_description = methodist.get("assignment_description")
        ctx.assignment_steps = methodist.get("assignment_steps", [])
        ctx.common_misconceptions = methodist.get("common_misconceptions", [])
        ctx.key_concepts = methodist.get("key_concepts", [])
        ctx.teaching_recommendations = methodist.get("teaching_recommendations")

    # Student history
    if student:
        past = await hw_repo.get_for_student(
            student.id,
            course_node_id=submission.course_node_id,
        )
        # Exclude current submission
        past = [s for s in past if s.id != submission.id and s.review_result]
        ctx.student_history = _build_student_history(past)

    logger.info(
        "mentor_context_built",
        task_title=ctx.task_title,
        node_type=ctx.node_type,
        language=ctx.response_language,
        has_grading_criteria=ctx.grading_criteria is not None,
        has_methodist=bool(ctx.learning_objectives),
        history_entries=len(ctx.student_history),
    )

    return ctx
