"""Today's Mentor road with the criteria list, end to end (task 08, K4).

Requires ``docker compose up -d`` (PostgreSQL).
Run with ``uv run pytest --run-db`` against this file.

``vision-rules#25``: the acceptance of task 08 is end to end, so it is proved
end to end. The real homework job (``arq_process_homework``) runs the real
safety stage, sanity gate, review graph, criteria-list service and decomposer
agent; only the model is a double — a router that renders each stage's real
prompt from the shipped ladders (the decomposition's is v2) and answers it as a
model would. What the locks watch:

* the first submission of a text task makes one decomposition call and leaves
  one ready row in ``task_criteria_lists``; the phase-1 prompt holds the list
  as pairs — the weight label first, the mandatory points after the evidence;
  a second submission makes no call;
* an exhausted decomposition ladder still gives a review, marked in its result,
  and leaves no ready list;
* the author's edit in force reaches the phase-1 prompt instead of the model's
  list.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.agents.criteria_decomposer import STAGE_NAME
from course_supporter.agents.mentor_review import STAGE_NODE_COURSE
from course_supporter.api.tasks import arq_process_homework
from course_supporter.criteria_list_state import CriteriaListState
from course_supporter.llm.error_categories import LadderExhaustedError
from course_supporter.llm.ladder_config import StageConfig, load_ladder_config
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.stage_router import StageResult
from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    HomeworkSubmission,
    Job,
    Student,
    TaskCriteriaList,
    Tenant,
)
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_REPO = Path(__file__).resolve().parents[2]
_TASK_TEXT = "Write a recursive factorial function. It must handle n = 0."
_SUBMISSION = "def factorial(n):\n    return 1 if n == 0 else n * factorial(n - 1)\n"

_LAYER = {
    "score": 80,
    "rationale": "solid",
    "strengths": ["base case"],
    "weaknesses": [],
}
_ANSWERS: dict[str, str] = {
    "safety_check": json.dumps(
        {"is_safe": True, "violations": [], "confidence": 0.9, "reasoning": "code"}
    ),
    "sanity_check": json.dumps(
        {"verdict": "match", "confidence": 0.9, "reason": "an honest attempt"}
    ),
    STAGE_NAME: json.dumps(
        {
            "criteria": [
                {
                    "text": "Handles n = 0",
                    "evidence": "returns 1 for n = 0",
                    "weight": "must",
                    "check_method": "mandatory_points",
                    "mandatory_points": ["returns 1 for 0", "no recursion error"],
                },
                {
                    "text": "Recurses toward the base case",
                    "evidence": "the argument shrinks",
                    "weight": "should",
                    "check_method": "model_verdict",
                },
            ],
            "contradictions": [],
        }
    ),
    STAGE_NODE_COURSE: json.dumps({"node": _LAYER, "course": _LAYER}),
    "mentor_layered_evaluation_industry": json.dumps({"industry": _LAYER}),
    "mentor_denoising": json.dumps(
        {"recidivism": [], "corrections": [], "delta": 0, "summary": "first try"}
    ),
    "mentor_synthesis": "# Review\n\nWell done.",
}


class _Model:
    """Both router entry points: the stage's real prompt, rendered and answered."""

    def __init__(self, *, decomposition_fails: bool = False) -> None:
        self._ladders = load_ladder_config(_REPO / "config")
        self._decomposition_fails = decomposition_fails
        self.prompts: dict[str, list[str]] = {}

    def _answer(
        self,
        stage_name: str,
        prompt_ref: str,
        context: dict[str, Any],
        validator: Callable[[str], None] | None,
    ) -> StageResult:
        prompt = load_prompt(prompt_ref, base_path=_REPO).render(**context)
        self.prompts.setdefault(stage_name, []).append(
            "\n".join(s for s in (prompt.system, prompt.user) if s)
        )
        if stage_name == STAGE_NAME and self._decomposition_fails:
            raise LadderExhaustedError(stage_name, [("double", "m", "no answer")])
        content = _ANSWERS[stage_name]
        if validator is not None:
            validator(content)
        return StageResult(
            content=content, provider_used="double", model_used="m", attempt_count=1
        )

    async def execute_for_stage(
        self,
        stage_name: str,
        *,
        response_validator: Callable[[str], None] | None = None,
        contents: list[bytes] | None = None,
        expects_json: bool = False,
        **render_context: Any,
    ) -> StageResult:
        prompt_ref = self._ladders.get_stage(stage_name).prompt_ref
        return self._answer(stage_name, prompt_ref, render_context, response_validator)

    async def execute_stage(
        self,
        stage: StageConfig,
        stage_name: str,
        /,
        *,
        response_validator: Callable[[str], None] | None = None,
        contents: list[bytes] | None = None,
        expects_json: bool = False,
        stop_on_output_ceiling: bool = False,
        money_ceiling_usd: float | None = None,
        **render_context: Any,
    ) -> StageResult:
        return self._answer(
            stage_name, stage.prompt_ref, render_context, response_validator
        )


class _Course:
    def __init__(self, ids: dict[str, uuid.UUID]) -> None:
        self.ids = ids


@pytest.fixture()
async def course(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[_Course]:
    """An ingested text task on a course and a student; cleaned up after."""
    async with session_factory() as session:
        tenant = Tenant(name=f"criteria-road-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = make_root_course_node(tenant_id=tenant.id, title="Python", order=0)
        session.add(node)
        await session.flush()
        task = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url="https://example.com/task",
            task_type="task",
            language="ukr",
        )
        session.add(task)
        await session.flush()
        summary = await DocumentSummaryRepository(session).create(
            authored_document_id=task.id,
            title="Factorial",
            description="Recursion.",
            main_concepts=[],
            secondary_concepts=[],
            content_char_count=len(_TASK_TEXT),
        )
        session.add(
            DocumentSegment(
                document_summary_id=summary.id,
                course_root_id=summary.course_root_id,
                order=0,
                start_pos=0,
                end_pos=len(_TASK_TEXT),
                content=_TASK_TEXT,
            )
        )
        student = Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:6]}")
        session.add(student)
        await session.commit()
        world = _Course(
            {
                "tenant_id": tenant.id,
                "node_id": node.id,
                "task_id": task.id,
                "student_id": student.id,
            }
        )

    yield world

    async with session_factory() as session:
        await session.execute(
            delete(HomeworkSubmission).where(
                HomeworkSubmission.tenant_id == world.ids["tenant_id"]
            )
        )
        await session.execute(
            delete(Job).where(Job.tenant_id == world.ids["tenant_id"])
        )
        await session.execute(
            delete(Student).where(Student.tenant_id == world.ids["tenant_id"])
        )
        await session.execute(
            delete(AuthoredDocument).where(AuthoredDocument.id == world.ids["task_id"])
        )
        await session.execute(
            delete(CourseNode).where(CourseNode.id == world.ids["node_id"])
        )
        await session.execute(delete(Tenant).where(Tenant.id == world.ids["tenant_id"]))
        await session.commit()


async def _submit(
    session_factory: async_sessionmaker[AsyncSession],
    course: _Course,
    model: _Model,
    tmp_path: Path,
) -> HomeworkSubmission:
    """One submission of the task, through the whole homework job."""
    ids = course.ids
    async with session_factory() as session:
        submission = HomeworkSubmission(
            tenant_id=ids["tenant_id"],
            student_id=ids["student_id"],
            course_node_id=ids["node_id"],
            node_id=ids["node_id"],
            authored_document_id=ids["task_id"],
            file_url="s3://bucket/homework/factorial.py",
            file_type="text/x-python",
            original_filename="factorial.py",
            status="received",
            response_language="en",
        )
        session.add(submission)
        await session.flush()
        job = Job(
            tenant_id=ids["tenant_id"],
            course_node_id=ids["node_id"],
            job_type="homework_processing",
            subject_type="homework_submission",
            subject_id=submission.id,
            input_params={"submission_id": str(submission.id)},
            status="queued",
        )
        session.add(job)
        await session.commit()
        submission_id, job_id = submission.id, job.id
    source = tmp_path / f"{uuid.uuid4().hex}.py"
    source.write_text(_SUBMISSION, encoding="utf-8")
    s3 = MagicMock()
    s3.extract_key = MagicMock(return_value="homework/factorial.py")
    s3.download_file = AsyncMock(return_value=source)
    ctx = {
        "session_factory": session_factory,
        "stage_router": model,
        "s3_client": s3,
        "job_try": 1,
    }

    await arq_process_homework(ctx, str(job_id), str(submission_id))

    async with session_factory() as session:
        reviewed = await session.get(HomeworkSubmission, submission_id)
    assert reviewed is not None
    return reviewed


async def _lists(
    session_factory: async_sessionmaker[AsyncSession], task_id: uuid.UUID
) -> list[TaskCriteriaList]:
    async with session_factory() as session:
        result = await session.execute(
            select(TaskCriteriaList).where(
                TaskCriteriaList.authored_document_id == task_id
            )
        )
        return list(result.scalars())


Submit = Callable[[_Model], Awaitable[HomeworkSubmission]]


@pytest.fixture()
def submit(
    session_factory: async_sessionmaker[AsyncSession],
    course: _Course,
    tmp_path: Path,
) -> Submit:
    async def _go(model: _Model) -> HomeworkSubmission:
        return await _submit(session_factory, course, model, tmp_path)

    return _go


class TestTheRoad:
    async def test_the_list_is_composed_once_and_read_as_weighted_pairs(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: _Course,
        submit: Submit,
    ) -> None:
        model = _Model()

        first = await submit(model)
        second = await submit(model)

        # One decomposition, for the first submission only.
        assert len(model.prompts[STAGE_NAME]) == 1
        # The decomposition read the v2 prompt — the ladder's since K4.
        assert "`check_method`" in model.prompts[STAGE_NAME][0]
        [listed] = await _lists(session_factory, course.ids["task_id"])
        assert listed.state == CriteriaListState.READY.value
        assert listed.deleted_at is None
        # Both reviews read the same list as pairs, weight label first and the
        # mandatory points after the evidence (task 08, decision 3.5).
        assert len(model.prompts[STAGE_NODE_COURSE]) == 2
        for phase_1 in model.prompts[STAGE_NODE_COURSE]:
            assert (
                "- [must] Handles n = 0 — evidence: returns 1 for n = 0 — mandatory "
                "points: returns 1 for 0; no recursion error" in phase_1
            )
            assert (
                "- [should] Recurses toward the base case — evidence: the argument "
                "shrinks" in phase_1
            )
        for reviewed in (first, second):
            assert reviewed.status == "completed"
            assert reviewed.review_result is not None
            assert reviewed.review_result["criteria_unavailable"] is None

    async def test_an_exhausted_ladder_still_gives_a_marked_review(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: _Course,
        submit: Submit,
    ) -> None:
        model = _Model(decomposition_fails=True)

        reviewed = await submit(model)

        assert reviewed.status == "completed"
        assert reviewed.review_result is not None
        assert reviewed.review_result["criteria_unavailable"] == "composition_failed"
        [phase_1] = model.prompts[STAGE_NODE_COURSE]
        assert "(No cached criteria available" in phase_1
        [listed] = await _lists(session_factory, course.ids["task_id"])
        assert listed.state == CriteriaListState.FAILED.value
        assert listed.deleted_at is not None

    async def test_the_authors_edit_in_force_replaces_the_models_list(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        course: _Course,
        submit: Submit,
    ) -> None:
        async with session_factory() as session:
            task = await session.get(AuthoredDocument, course.ids["task_id"])
            assert task is not None
            assert task.content_hash is not None
            assert task.task_type is not None
            await TaskCriteriaOverrideRepository(session).replace(
                authored_document_id=task.id,
                source_content_hash=task.content_hash,
                source_task_type=task.task_type,
                criteria=[
                    {
                        "id": "c1",
                        "text": "Explains the recursion in a comment",
                        "evidence": "a comment names the base case",
                        "weight": "may",
                        "check_method": "model_verdict",
                        "soft_descent": False,
                        "concepts": [],
                        "mandatory_points": [],
                    }
                ],
            )
            await session.commit()
        model = _Model()

        reviewed = await submit(model)

        assert STAGE_NAME not in model.prompts
        [phase_1] = model.prompts[STAGE_NODE_COURSE]
        assert (
            "- [may] Explains the recursion in a comment — evidence: a comment names "
            "the base case" in phase_1
        )
        assert "Handles n = 0" not in phase_1
        assert reviewed.review_result is not None
        assert reviewed.review_result["criteria_unavailable"] is None
        assert await _lists(session_factory, course.ids["task_id"]) == []
