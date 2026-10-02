"""The explanation stage against a live database (mentor-rebuild task 09b, K5).

The stage with its real description from ``config/submission_paths.yaml``,
the real facts and check, the real verdict repository and the real lists of
task 08 — and the router as a double, or as the real router over two fake
providers (the alternative is a paid call):

* the explanation is written for the builder, in the student's language and
  else in the course root's (``PRE-FLIGHT.md`` 9.9);
* the facts come from the list the verdicts were judged against, read by the
  rows' address — not from the list in force now — and rows of another
  task's list, or none at all, are a defect, never an explanation;
* an explanation that disagrees with the facts is retried on the same rung
  and never kept (``TASK.md`` lock 7); a ladder that gives none keeps nothing.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework.criteria_form import Criterion, criteria_to_document
from course_supporter.homework.path_config import (
    PathKey,
    SubmissionState,
    load_path_config,
)
from course_supporter.homework.path_stages import (
    REVIEW_EXPLANATION,
    StageContext,
    StageOutcome,
    _execution,
)
from course_supporter.homework.review_explanation import explain_verdicts
from course_supporter.homework.verdict_explanation import ExplanationAnswer
from course_supporter.llm.error_categories import ErrorCategory, LadderExhaustedError
from course_supporter.llm.ladder_config import LadderConfig, StageConfig
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import load_registry
from course_supporter.llm.schemas import LLMResponse
from course_supporter.llm.stage_router import StageResult, StageRouter
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
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
    VerdictRecord,
)
from course_supporter.storage.task_criteria_list_repository import (
    TaskCriteriaListRepository,
)
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORK = "--- solution.py ---\ndef fibonacci(n):\n    return n\n"


def _criterion(cid: str, weight: str, points: int = 0, *, text: str = "") -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": text or f"Criterion {cid}",
            "evidence": f"What shows {cid}",
            "weight": weight,
            "check_method": "mandatory_points" if points else "model_verdict",
            "soft_descent": False,
            "concepts": [],
            "mandatory_points": [
                {"id": f"{cid}.p{n}", "text": f"Point {n} of {cid}"}
                for n in range(1, points + 1)
            ],
        }
    )


_CRITERIA = (_criterion("c1", "must"), _criterion("c2", "should", points=2))

# c1 met; c2 not — its second point is not: (3) of (3 + 2), passed.
_RECORDS = (
    VerdictRecord(
        "c1",
        VerdictItemKind.CRITERION,
        WeightCategory.MUST,
        VerdictValue.MET,
        VerdictValue.MET,
        quote="def fibonacci(n):",
        quote_file="solution.py",
        quote_lines=(1, 1),
    ),
    VerdictRecord(
        "c2",
        VerdictItemKind.CRITERION,
        WeightCategory.SHOULD,
        VerdictValue.NOT_MET,
        None,
    ),
    VerdictRecord(
        "c2.p1",
        VerdictItemKind.POINT,
        None,
        VerdictValue.MET,
        VerdictValue.MET,
        quote="return n",
        quote_file="solution.py",
        quote_lines=(2, 2),
    ),
    VerdictRecord(
        "c2.p2",
        VerdictItemKind.POINT,
        None,
        VerdictValue.NOT_MET,
        VerdictValue.NOT_MET,
        missing="Немає жодного тесту.",
    ),
)


def _answer(*, passed: bool = True, remarks: tuple[str, ...] = ("c2",)) -> str:
    return json.dumps(
        {
            "passed": passed,
            "why": "Робота зарахована: функція є й рекурсивна.",
            "remarks": [
                {
                    "id": cid,
                    "what": "Тестів у роботі немає.",
                    "why": "Без них не видно, що базовий випадок працює.",
                    "todo": "Додайте тест для n = 0 і n = 1.",
                }
                for cid in remarks
            ],
            "mentor_voice": "Гарна назва функції.",
        },
        ensure_ascii=False,
    )


class _Router:
    """Router double: records each request, runs the validator, answers."""

    def __init__(self, *contents: str) -> None:
        self._contents = list(contents)
        self.requests: list[dict[str, Any]] = []

    async def execute_stage(
        self,
        stage: StageConfig,
        stage_name: str,
        /,
        *,
        response_validator: Callable[[str], None] | None = None,
        contents: Any = None,
        expects_json: bool = False,
        response_schema: dict[str, Any] | None = None,
        stop_on_output_ceiling: bool = False,
        money_ceiling_usd: float | None = None,
        **render: Any,
    ) -> StageResult:
        self.requests.append(
            {
                "stage_name": stage_name,
                "ladder": [(r.provider, r.model) for r in stage.ladder],
                "money_ceiling_usd": money_ceiling_usd,
                "stop_on_output_ceiling": stop_on_output_ceiling,
                "render": render,
            }
        )
        content = self._contents.pop(0)
        if response_validator is not None:
            response_validator(content)
        rung = stage.ladder[0]
        return StageResult(
            content=content,
            provider_used=rung.provider,
            model_used=rung.model,
            attempt_count=1,
        )


@pytest.fixture()
async def root(db_session: AsyncSession, seed_tenant: Tenant) -> CourseNode:
    node = make_root_course_node(
        tenant_id=seed_tenant.id, title="Course", order=0, default_language="eng"
    )
    db_session.add(node)
    await db_session.flush()
    return node


@pytest.fixture()
async def doc(db_session: AsyncSession, root: CourseNode) -> AuthoredDocument:
    return await _task(db_session, root)


async def _task(session: AsyncSession, root: CourseNode) -> AuthoredDocument:
    doc = AuthoredDocument(
        course_node_id=root.id,
        course_root_id=root.id,
        source_type="text",
        source_url=f"https://example.com/{uuid.uuid4().hex}",
        task_type="task",
    )
    session.add(doc)
    await session.flush()
    return doc


@pytest.fixture()
async def submission(
    db_session: AsyncSession,
    seed_tenant: Tenant,
    root: CourseNode,
    doc: AuthoredDocument,
) -> HomeworkSubmission:
    student: Student = await StudentRepository(db_session).create(
        tenant_id=seed_tenant.id, external_id=f"ext-{uuid.uuid4().hex[:8]}"
    )
    return await HomeworkRepository(db_session).create(
        tenant_id=seed_tenant.id,
        student_id=student.id,
        course_node_id=root.id,
        node_id=root.id,
        authored_document_id=doc.id,
        file_url="s3://bucket/solution.py",
        file_type="text/plain",
        original_filename="solution.py",
    )


async def _authors_list(
    session: AsyncSession,
    doc: AuthoredDocument,
    criteria: tuple[Criterion, ...] = _CRITERIA,
) -> uuid.UUID:
    edit = await TaskCriteriaOverrideRepository(session).replace(
        authored_document_id=doc.id,
        source_content_hash="h" * 64,
        source_task_type="task",
        criteria=criteria_to_document(criteria),
    )
    return edit.id


async def _models_list(session: AsyncSession, doc: AuthoredDocument) -> uuid.UUID:
    repo = TaskCriteriaListRepository(session)
    row = await repo.claim(
        authored_document_id=doc.id,
        source_content_hash="h" * 64,
        source_task_type="task",
        form_version=2,
    )
    assert row is not None
    assert await repo.mark_ready(
        row.id,
        claimed_at=row.claimed_at,
        criteria=criteria_to_document(_CRITERIA),
        contradictions=[],
        concepts_in_input=False,
        dropped_concept_count=0,
        input_fingerprint="f" * 64,
    )
    return row.id


async def _verdicts(
    session: AsyncSession,
    submission: HomeworkSubmission,
    *,
    layer: CriteriaLayer,
    source_id: uuid.UUID,
) -> None:
    await SubmissionVerdictRepository(session).replace_for_submission(
        tenant_id=submission.tenant_id,
        submission_id=submission.id,
        criteria_layer=layer,
        criteria_source_id=source_id,
        records=_RECORDS,
    )


def _context(
    session: AsyncSession,
    router: Any,
    submission: HomeworkSubmission,
    *,
    language: str | None = "ukr",
) -> StageContext:
    config = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")
    return StageContext(
        session=session,
        router=router,
        submission=submission,
        submission_text=_WORK,
        language=language,
        path_key=PathKey(AssignmentType.TASK, SubmissionState.FIRST),
        stage_name=REVIEW_EXPLANATION,
        stage=config.stages[REVIEW_EXPLANATION],
        session_factory=AsyncMock(),
    )


async def _explain(
    session: AsyncSession,
    router: Any,
    submission: HomeworkSubmission,
    *,
    language: str | None = "ukr",
) -> StageOutcome:
    context = _context(session, router, submission, language=language)
    return await explain_verdicts(context, execution=_execution(context))


async def _kept(
    session: AsyncSession, submission: HomeworkSubmission
) -> tuple[str, dict[str, Any]] | None:
    row = await SubmissionVerdictRepository(session).get_explanation(
        tenant_id=submission.tenant_id, submission_id=submission.id
    )
    return None if row is None else (row.language, row.body)


class TestTheExplanationIsKept:
    async def test_the_answer_is_kept_in_the_students_language(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=source_id
        )
        router = _Router(_answer())

        outcome = await _explain(db_session, router, submission)

        assert outcome == StageOutcome.ok()
        (request,) = router.requests
        assert request["stage_name"] == REVIEW_EXPLANATION
        assert request["ladder"] == [
            ("dashscope", "qwen3.7-max"),
            ("deepseek", "deepseek-flash"),
        ]
        assert request["money_ceiling_usd"] == 0.18
        assert request["stop_on_output_ceiling"] is True
        render = request["render"]
        assert render["result"] == {"passed": True, "score": 60}
        assert [c["id"] for c in render["criteria"]] == ["c1", "c2"]
        assert render["criteria"][0]["place"] == {
            "file": "solution.py",
            "lines": [1, 1],
        }
        assert render["submission_text"] == _WORK
        assert render["language"] == "Ukrainian"
        expected = ExplanationAnswer.model_validate_json(_answer())
        assert await _kept(db_session, submission) == (
            "ukr",
            expected.model_dump(mode="json"),
        )


class TestTheLanguage:
    """``PRE-FLIGHT.md`` 9.9: the student's, else the course root's."""

    async def test_without_the_students_the_roots_is_taken(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=source_id
        )
        router = _Router(_answer())

        await _explain(db_session, router, submission, language=None)

        assert router.requests[0]["render"]["language"] == "English"
        kept = await _kept(db_session, submission)
        assert kept is not None
        assert kept[0] == "eng"

    async def test_the_students_comes_before_the_roots(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=source_id
        )
        router = _Router(_answer())

        await _explain(db_session, router, submission, language="pol")

        assert router.requests[0]["render"]["language"] == "Polish"
        kept = await _kept(db_session, submission)
        assert kept is not None
        assert kept[0] == "pol"


class TestTheListTheVerdictsWereJudgedAgainst:
    async def test_an_edit_replaced_since_still_gives_its_own_texts(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        judged = await _authors_list(
            db_session,
            doc,
            (_criterion("c1", "must", text="Judged c1"), _CRITERIA[1]),
        )
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=judged
        )
        # The author edits the list after the evaluation: the judged edit is
        # now a soft-deleted snapshot, and another edit is in force.
        await _authors_list(
            db_session, doc, (_criterion("c1", "must", text="Newer c1"), _CRITERIA[1])
        )
        router = _Router(_answer())

        await _explain(db_session, router, submission)

        assert router.requests[0]["render"]["criteria"][0]["text"] == "Judged c1"

    async def test_the_models_list_is_read_as_well(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        source_id = await _models_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.MODEL, source_id=source_id
        )
        router = _Router(_answer())

        assert await _explain(db_session, router, submission) == StageOutcome.ok()
        assert [c["id"] for c in router.requests[0]["render"]["criteria"]] == [
            "c1",
            "c2",
        ]

    async def test_another_tasks_list_is_a_defect(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        root: CourseNode,
    ) -> None:
        other = await _authors_list(db_session, await _task(db_session, root))
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=other
        )
        router = _Router(_answer())

        with pytest.raises(ValueError, match="is not this task's"):
            await _explain(db_session, router, submission)

        assert router.requests == []
        assert await _kept(db_session, submission) is None

    async def test_the_address_names_its_layer(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        """An author's edit read as the model's list is not there."""
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.MODEL, source_id=source_id
        )

        with pytest.raises(ValueError, match="is not this task's"):
            await _explain(db_session, _Router(_answer()), submission)


class TestWithoutVerdicts:
    async def test_no_rows_is_a_defect_and_costs_nothing(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        router = _Router(_answer())

        with pytest.raises(ValueError, match="has none"):
            await _explain(db_session, router, submission)

        assert router.requests == []
        assert await _kept(db_session, submission) is None


def _provider(*responses: LLMResponse) -> Any:
    provider = AsyncMock(spec=LLMProvider)
    provider.enabled = True
    provider.complete = AsyncMock(side_effect=list(responses))
    provider.classify_error = lambda _exc: ErrorCategory.SEMANTIC
    return provider


def _response(content: str) -> LLMResponse:
    return LLMResponse(content=content, provider="p", model_id="m")


def _real_router(qwen: Any, deepseek: Any) -> StageRouter:
    return StageRouter(
        LadderConfig(),
        {"dashscope": qwen, "deepseek": deepseek},
        registry=load_registry(_REPO_ROOT / "config" / "external_services.yaml"),
        prompt_base_path=_REPO_ROOT,
    )


class TestAnExplanationAgainstTheFacts:
    """Lock 7 through the real router: what disagrees is never kept."""

    async def test_it_is_retried_and_only_the_agreeing_one_is_kept(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=source_id
        )
        # The unmet "should" left without its remark, then mended.
        qwen = _provider(_response(_answer(remarks=())), _response(_answer()))
        deepseek = _provider()

        await _explain(db_session, _real_router(qwen, deepseek), submission)

        assert qwen.complete.await_count == 2
        deepseek.complete.assert_not_awaited()
        kept = await _kept(db_session, submission)
        assert kept is not None
        assert [r["id"] for r in kept[1]["remarks"]] == ["c2"]

    async def test_a_ladder_that_gives_none_keeps_nothing(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=source_id
        )
        wrong = _response(_answer(passed=False))
        qwen = _provider(wrong, wrong)
        deepseek = _provider(wrong, wrong)

        with pytest.raises(LadderExhaustedError):
            await _explain(db_session, _real_router(qwen, deepseek), submission)

        assert await _kept(db_session, submission) is None
