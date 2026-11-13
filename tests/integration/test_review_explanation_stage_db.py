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
  and never kept (``TASK.md`` lock 7); a ladder that gives none keeps nothing;
* the places of the course the search step kept are shown under local
  identifiers, and an identifier the answer names that was not shown for its
  criterion is cut from what is kept and leaves a ``course_link_fabricated``
  row in the call register — no retry, no model call (task 10, K5, lock h).

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import uuid
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.agents import review_explainer
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
from course_supporter.homework.explanation_input_rebuild import (
    rebuild_input,
    verify_against_register,
)
from course_supporter.homework.explanation_measure import input_of, record_of
from course_supporter.homework.path_config import (
    PathKey,
    SubmissionState,
    load_path_config,
)
from course_supporter.homework.path_runner import _open_the_doors
from course_supporter.homework.path_stages import (
    REVIEW_EXPLANATION,
    StageContext,
    StageOutcome,
    _execution,
)
from course_supporter.homework.review_explanation import (
    explain_verdicts,
    explanation_input,
)
from course_supporter.homework.verdict_explanation import ExplanationAnswer
from course_supporter.llm.error_categories import ErrorCategory, LadderExhaustedError
from course_supporter.llm.ladder_config import LadderConfig, StageConfig
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import load_registry
from course_supporter.llm.schemas import LLMResponse
from course_supporter.llm.stage_router import StageResult, StageRouter
from course_supporter.models.source import AssignmentType
from course_supporter.service_logging import CourseLinkTrace, job_scope
from course_supporter.storage.document_segment_repository import SegmentAnchor
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
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

# c1 met; c2 not — its second point is not: (3) of (3 + 2) -> 60. Not passed
# on either list (the pass rule of 2026-11-12): below 70 on the author's,
# below 80 on the model's.
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


def _answer(
    *,
    passed: bool = False,
    remarks: tuple[str, ...] = ("c2",),
    links: tuple[str, ...] = (),
) -> str:
    return json.dumps(
        {
            "passed": passed,
            "why": "Робота не зарахована: функція є й рекурсивна, але тестів немає.",
            "remarks": [
                {
                    "id": cid,
                    "what": "Тестів у роботі немає.",
                    "why": "Без них не видно, що базовий випадок працює.",
                    "todo": "Додайте тест для n = 0 і n = 1.",
                    "links": list(links),
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
    session_factory: Any = None,
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
        session_factory=session_factory or AsyncMock(),
    )


async def _explain(
    session: AsyncSession,
    router: Any,
    submission: HomeworkSubmission,
    *,
    language: str | None = "ukr",
    session_factory: Any = None,
) -> StageOutcome:
    context = _context(
        session, router, submission, language=language, session_factory=session_factory
    )
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
        assert request["money_ceiling_usd"] == 0.24
        assert request["stop_on_output_ceiling"] is True
        render = request["render"]
        assert render["result"] == {"passed": False, "score": 60}
        assert [c["id"] for c in render["criteria"]] == ["c1", "c2"]
        assert render["criteria"][0]["place"] == {
            "file": "solution.py",
            "lines": [1, 1],
        }
        assert render["submission_text"] == _WORK
        assert render["language"] == "Ukrainian"
        # No search kept anything for this submission: nothing is shown.
        assert render["course_links"] == [{"criterion": "c2", "links": []}]
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
        wrong = _response(_answer(passed=True))
        qwen = _provider(wrong, wrong)
        deepseek = _provider(wrong, wrong)

        with pytest.raises(LadderExhaustedError):
            await _explain(db_session, _real_router(qwen, deepseek), submission)

        assert await _kept(db_session, submission) is None


# ── Course links: shown, chosen, and only the shown ones kept (task 10, K5) ──


def _place(title: str, paragraph: int, confidence: float = 0.9) -> LinkCandidate:
    return LinkCandidate(
        segment_id=uuid.uuid4(),
        authored_document_id=uuid.uuid4(),
        material_title=title,
        source_type="text",
        anchor=SegmentAnchor("paragraph", paragraph, paragraph + 1),
        excerpt=f"Початок {title}",
        later_in_course=False,
        confidence=confidence,
        matched_concepts=("testing",),
    )


async def _found(session: AsyncSession, submission: HomeworkSubmission) -> None:
    """What the search step keeps: two places for c2 and — stale, from an
    earlier run's verdicts — one for c1, which is met now."""
    found = FoundCourseLinks(
        criteria=(
            CriterionLinks(
                item_id="c2", candidates=(_place("Тести", 3), _place("pytest", 7))
            ),
            CriterionLinks(item_id="c1", candidates=(_place("Функції", 1),)),
        )
    )
    await SubmissionVerdictRepository(session).store_candidates(
        tenant_id=submission.tenant_id,
        submission_id=submission.id,
        body=found.model_dump(mode="json"),
    )


@pytest.fixture()
async def register_job(
    session_factory: async_sessionmaker[AsyncSession],
    committed_seeds: dict[str, uuid.UUID],
) -> AsyncGenerator[uuid.UUID]:
    """A committed job: register rows are written in their own session."""
    async with session_factory() as session:
        job = Job(
            tenant_id=committed_seeds["tenant_id"],
            course_node_id=committed_seeds["course_node_id"],
            job_type="homework_processing",
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    yield job_id

    async with session_factory() as session:
        await session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id == job_id)
        )
        await session.execute(delete(Job).where(Job.id == job_id))
        await session.commit()


async def _traces(
    session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> list[ExternalServiceCall]:
    async with session_factory() as session:
        rows = await session.execute(
            select(ExternalServiceCall)
            .where(ExternalServiceCall.job_id == job_id)
            .order_by(ExternalServiceCall.created_at)
        )
        return list(rows.scalars())


class TestTheCourseLinks:
    async def test_the_kept_places_are_shown_under_local_identifiers(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=source_id
        )
        await _found(db_session, submission)
        router = _Router(_answer(links=("c2.l2",)))

        await _explain(db_session, router, submission)

        assert router.requests[0]["render"]["course_links"] == [
            {
                "criterion": "c2",
                "links": [
                    {
                        "id": "c2.l1",
                        "material": "Тести",
                        "position": {"kind": "paragraph", "value": "3"},
                        "excerpt": "Початок Тести",
                    },
                    {
                        "id": "c2.l2",
                        "material": "pytest",
                        "position": {"kind": "paragraph", "value": "7"},
                        "excerpt": "Початок pytest",
                    },
                ],
            }
        ]
        kept = await _kept(db_session, submission)
        assert kept is not None
        assert [r["links"] for r in kept[1]["remarks"]] == [["c2.l2"]]

    async def test_a_link_not_shown_for_its_criterion_is_cut_and_traced(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        register_job: uuid.UUID,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        """Lock h: ``c1.l1`` was kept for c1 — met, so never shown — and
        ``c2.l9`` was shown to no one; ``c2.l1`` was shown for c2 and stays.
        One request: a made-up link is cut, never retried."""
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=source_id
        )
        await _found(db_session, submission)
        router = _Router(_answer(links=("c1.l1", "c2.l1", "c2.l9")))

        with job_scope(register_job):
            outcome = await _explain(
                db_session, router, submission, session_factory=session_factory
            )

        assert outcome == StageOutcome.ok()
        assert len(router.requests) == 1
        kept = await _kept(db_session, submission)
        assert kept is not None
        assert [r["links"] for r in kept[1]["remarks"]] == [["c2.l1"]]
        (trace,) = await _traces(session_factory, register_job)
        assert (
            trace.action
            == CourseLinkTrace.FABRICATED.value
            == ("course_link_fabricated")
        )
        assert json.loads(trace.output_text or "") == {
            "criterion_id": "c2",
            "ids": ["c1.l1", "c2.l9"],
        }
        # A row without a call: out of every cost sum and of the failure count.
        assert (trace.provider, trace.model_id, trace.success) == (None, None, None)
        assert (trace.cost_usd, trace.error_message, trace.outcome) == (
            None,
            None,
            None,
        )
        # Identifiers only: nothing of the student's work, nothing of the texts.
        assert "fibonacci" not in (trace.output_text or "")
        assert "Тестів" not in (trace.output_text or "")

    async def test_only_shown_links_leave_no_trace(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        register_job: uuid.UUID,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=source_id
        )
        await _found(db_session, submission)
        router = _Router(_answer(links=("c2.l1", "c2.l2")))

        with job_scope(register_job):
            await _explain(
                db_session, router, submission, session_factory=session_factory
            )

        kept = await _kept(db_session, submission)
        assert kept is not None
        assert [r["links"] for r in kept[1]["remarks"]] == [["c2.l1", "c2.l2"]]
        assert await _traces(session_factory, register_job) == []

    async def test_an_anchor_that_does_not_fit_is_shown_without_a_place(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        """Task 10, K6, lock e: a kept place whose anchor does not fit its
        material is shown without a position, the log names its fragment, and
        the stage goes on."""
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=source_id
        )
        # A video's place with a paragraph's anchor.
        misfit = dataclasses.replace(_place("Тести", 3), source_type="video")
        await SubmissionVerdictRepository(db_session).store_candidates(
            tenant_id=submission.tenant_id,
            submission_id=submission.id,
            body=FoundCourseLinks(
                criteria=(CriterionLinks(item_id="c2", candidates=(misfit,)),)
            ).model_dump(mode="json"),
        )
        router = _Router(_answer(links=("c2.l1",)))

        with capture_logs() as logs:
            outcome = await _explain(db_session, router, submission)

        assert outcome == StageOutcome.ok()
        assert router.requests[0]["render"]["course_links"] == [
            {
                "criterion": "c2",
                "links": [
                    {
                        "id": "c2.l1",
                        "material": "Тести",
                        "position": None,
                        "excerpt": "Початок Тести",
                    }
                ],
            }
        ]
        skipped = [e for e in logs if e["event"] == "course_link_position_skipped"]
        assert [e["segment_id"] for e in skipped] == [str(misfit.segment_id)]


# ── Places below the score threshold (task 10, decision of 2026-11-13) ──────


async def _found_c2(
    session: AsyncSession,
    submission: HomeworkSubmission,
    *places: LinkCandidate,
) -> None:
    """What the search step keeps for c2 alone — every place it found."""
    await SubmissionVerdictRepository(session).store_candidates(
        tenant_id=submission.tenant_id,
        submission_id=submission.id,
        body=FoundCourseLinks(
            criteria=(CriterionLinks(item_id="c2", candidates=places),)
        ).model_dump(mode="json"),
    )


async def _kept_confidences(
    session: AsyncSession, submission: HomeworkSubmission
) -> list[float]:
    """The confidences of c2's places in the search's row, as it is kept."""
    row = await SubmissionVerdictRepository(session).get_candidates(
        tenant_id=submission.tenant_id, submission_id=submission.id
    )
    assert row is not None
    kept = FoundCourseLinks.model_validate(row.body)
    return [place.confidence for place in kept.candidates_of("c2")]


class TestAPlaceBelowTheThreshold:
    """The row keeps every place; the model is shown those from 0.3 up."""

    async def test_a_weak_place_stays_in_the_row_and_is_not_shown(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        """Lock a through the stage: 0.30 is shown, 0.29 and 0.14 are not —
        and the row they are kept in is not touched by the stage."""
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=source_id
        )
        await _found_c2(
            db_session,
            submission,
            _place("Тести", 3, 0.30),
            _place("pytest", 7, 0.29),
            _place("Фінальний проєкт", 2, 0.14),
        )
        router = _Router(_answer(links=("c2.l1",)))

        await _explain(db_session, router, submission)

        assert router.requests[0]["render"]["course_links"] == [
            {
                "criterion": "c2",
                "links": [
                    {
                        "id": "c2.l1",
                        "material": "Тести",
                        "position": {"kind": "paragraph", "value": "3"},
                        "excerpt": "Початок Тести",
                    }
                ],
            }
        ]
        kept = await _kept(db_session, submission)
        assert kept is not None
        assert [r["links"] for r in kept[1]["remarks"]] == [["c2.l1"]]
        assert await _kept_confidences(db_session, submission) == [0.30, 0.29, 0.14]

    async def test_a_criterion_with_only_weak_places_is_shown_none(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        """Lock b through the stage: production's two places of 0.14 give the
        model an empty list for c2, as a criterion the course does not teach."""
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=source_id
        )
        await _found_c2(
            db_session,
            submission,
            _place("Фінальний проєкт", 2, 0.14),
            _place("Підсумкове заняття", 5, 0.14),
        )
        router = _Router(_answer(links=()))

        await _explain(db_session, router, submission)

        assert router.requests[0]["render"]["course_links"] == [
            {"criterion": "c2", "links": []}
        ]
        kept = await _kept(db_session, submission)
        assert kept is not None
        assert [r["links"] for r in kept[1]["remarks"]] == [[]]
        assert await _kept_confidences(db_session, submission) == [0.14, 0.14]

    async def test_a_recorded_answer_that_names_the_weak_places_is_cut_and_traced(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        register_job: uuid.UUID,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        """Lock v through the stage: the answer production's model gave — both
        places of 0.14 chosen — names what this model was not shown. Both are
        cut, once, with a trace and no retry."""
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=source_id
        )
        await _found_c2(
            db_session,
            submission,
            _place("Фінальний проєкт", 2, 0.14),
            _place("Підсумкове заняття", 5, 0.14),
        )
        router = _Router(_answer(links=("c2.l1", "c2.l2")))

        with job_scope(register_job):
            await _explain(
                db_session, router, submission, session_factory=session_factory
            )

        assert len(router.requests) == 1
        kept = await _kept(db_session, submission)
        assert kept is not None
        assert [r["links"] for r in kept[1]["remarks"]] == [[]]
        (trace,) = await _traces(session_factory, register_job)
        assert trace.action == CourseLinkTrace.FABRICATED.value
        assert json.loads(trace.output_text or "") == {
            "criterion_id": "c2",
            "ids": ["c2.l1", "c2.l2"],
        }


# ── Prompt v3 (2026-11-13): what the stage sends, and its input rebuilt ──


def _v3_router(
    qwen: Any, session_factory: async_sessionmaker[AsyncSession] | None = None
) -> StageRouter:
    return StageRouter(
        LadderConfig(),
        {"dashscope": qwen, "deepseek": _provider()},
        registry=load_registry(_REPO_ROOT / "config" / "external_services.yaml"),
        session_factory=session_factory,
        prompt_base_path=_REPO_ROOT,
    )


class TestTheStageSendsPromptV3:
    """Lock c of prompt v3, through the real router: the system message is
    v3's, the user message the one v2 renders from the same input."""

    async def test_v3s_system_and_v2s_user_go_on_the_wire(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=source_id
        )
        await _found(db_session, submission)
        qwen = _provider(_response(_answer()))

        await _explain(db_session, _v3_router(qwen), submission)

        (request,) = [call.args[0] for call in qwen.complete.await_args_list]
        shown, _ = await explanation_input(
            db_session, submission, submission_text=_WORK, language="ukr"
        )
        context = review_explainer.render_context(shown)
        v2 = load_prompt("prompts/review_explanation/v2.md", base_path=_REPO_ROOT)
        v3 = load_prompt("prompts/review_explanation/v3.md", base_path=_REPO_ROOT)
        assert request.system_prompt == v3.render(**context).system
        assert request.prompt == v2.render(**context).user
        assert request.system_prompt != v2.render(**context).system


_STORED = b"def fibonacci(n):\n    return n\n"


class _Storage:
    """The S3 client double: one stored file, under one key."""

    def __init__(self, content: bytes) -> None:
        self.content = content

    def extract_key(self, url: str) -> str:
        return "homework/solution.py"

    async def download_file(self, key: str, dest: Path | None = None) -> Path:
        path = dest or Path(tempfile.mkdtemp()) / "download.py"
        path.write_bytes(self.content)
        return path


@pytest.fixture()
async def submission_job(
    session_factory: async_sessionmaker[AsyncSession],
    committed_seeds: dict[str, uuid.UUID],
    submission: HomeworkSubmission,
) -> AsyncGenerator[uuid.UUID]:
    """A committed job whose subject is the submission, as the path's job is."""
    async with session_factory() as session:
        job = Job(
            tenant_id=committed_seeds["tenant_id"],
            course_node_id=committed_seeds["course_node_id"],
            job_type="homework_processing",
            subject_type="homework_submission",
            subject_id=submission.id,
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    yield job_id

    async with session_factory() as session:
        await session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id == job_id)
        )
        await session.execute(delete(Job).where(Job.id == job_id))
        await session.commit()


class TestTheInputIsRebuiltFromWhatTheStageLeft:
    """The recorder of the live measurement (``scripts/record_explanation_input.py``):
    the stage's input rebuilt from the rows, the stored file read again — and
    the request rebuilt from it hashes to the register's row of the run.

    The run's work is read by the path runner's own door, so a door the
    rebuild copies wrongly is red here.
    """

    async def _run_the_stage(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        job_id: uuid.UUID,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> dict[str, Any]:
        source_id = await _authors_list(db_session, doc)
        await _verdicts(
            db_session, submission, layer=CriteriaLayer.AUTHOR, source_id=source_id
        )
        await _found(db_session, submission)
        _, reading, language = await _open_the_doors(
            db_session,
            _Storage(_STORED),  # type: ignore[arg-type]
            HomeworkRepository(db_session),
            submission,
            job_id=job_id,
            log=structlog.get_logger(),
        )
        assert reading is not None
        context = dataclasses.replace(
            _context(
                db_session,
                _v3_router(_provider(_response(_answer())), session_factory),
                submission,
                language=language,
                session_factory=session_factory,
            ),
            submission_text=reading.text,
        )
        with job_scope(job_id):
            await explain_verdicts(context, execution=_execution(context))
        shown, _ = await explanation_input(
            db_session, submission, submission_text=reading.text, language=language
        )
        return review_explainer.render_context(shown)

    async def test_the_rebuilt_request_is_the_one_the_register_holds(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        submission_job: uuid.UUID,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        sent = await self._run_the_stage(
            db_session, session_factory, submission_job, submission, doc
        )

        rebuilt = await rebuild_input(
            db_session,
            _Storage(_STORED),  # type: ignore[arg-type]
            submission.id,
        )
        stage = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")
        verification = await verify_against_register(
            rebuilt,
            stage=stage.stages[REVIEW_EXPLANATION],  # type: ignore[arg-type]
            registry=load_registry(_REPO_ROOT / "config" / "external_services.yaml"),
            prompt_base_path=_REPO_ROOT,
        )

        assert verification["matched"] is True
        assert verification["prompts"]["prompts/review_explanation/v3.md"]["rows"] == 1
        assert review_explainer.render_context(rebuilt.shown) == sent
        assert rebuilt.stored_explanation is not None
        assert rebuilt.task_type == "task"
        # The record carries the input whole, and reads back to it.
        assert input_of(record_of(rebuilt.shown)) == rebuilt.shown

    async def test_a_file_changed_since_is_not_confirmed(
        self,
        db_session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        submission_job: uuid.UUID,
        submission: HomeworkSubmission,
        doc: AuthoredDocument,
    ) -> None:
        await self._run_the_stage(
            db_session, session_factory, submission_job, submission, doc
        )

        rebuilt = await rebuild_input(
            db_session,
            _Storage(_STORED + b"# edited\n"),  # type: ignore[arg-type]
            submission.id,
        )
        stage = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")
        verification = await verify_against_register(
            rebuilt,
            stage=stage.stages[REVIEW_EXPLANATION],  # type: ignore[arg-type]
            registry=load_registry(_REPO_ROOT / "config" / "external_services.yaml"),
            prompt_base_path=_REPO_ROOT,
        )

        assert verification["matched"] is False
        assert verification["first_rung_rows"] == 1
