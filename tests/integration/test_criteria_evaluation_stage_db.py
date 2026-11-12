"""The evaluation stage against a live database (mentor-rebuild task 09b, K3).

The stage's own locks, with the stage's real description from
``config/submission_paths.yaml``, the real rules, the real verdict repository —
and two doubles: the router (the alternative is a paid call) and the source of
the criteria list (composing one is task 08's, locked there). ``TASK.md``
section 5:

* lock 4 — a quote the work does not have gives no "met"; only the verdicts
  that cannot stand are asked about again, each with its reason, of the rung
  that answered; a second failure reads "not met", flagged for the author;
* lock 5 — the safeguard keeps an earlier "met" of the same list whose quote
  is still in the work, says so in the row and in the log; another list's
  verdict is not kept;
* lock 6 — without a list the revision is held and no evaluation call is
  made;
* an answer cut off at the output ceiling stops the stage after one paid call
  and writes nothing;
* K4 — a stage that got the list puts back the task's revisions that wait for
  it, in a session of its own, and a queue that refuses costs this submission
  nothing;
* project chain K3 — a quote of the project's base is asked about again; a
  second one, or a "met" taken back, is written "not met" with
  ``quote_from_base``; a task without a base is asked exactly as before.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.agents.criteria_evaluator import EvaluationInput
from course_supporter.agents.criteria_evaluator import (
    render_context as evaluation_render_context,
)
from course_supporter.agents.review_explainer import ExplanationInput, render_context
from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework.criteria_evaluation import (
    _course_language,
    evaluate_criteria,
)
from course_supporter.homework.criteria_form import Criterion
from course_supporter.homework.criteria_list_service import (
    CriteriaInForce,
    CriteriaUnavailable,
    UnavailableReason,
)
from course_supporter.homework.criteria_verdicts import (
    MAX_QUOTE_CHARS,
    RepeatItem,
    RepeatReason,
    WorkFile,
    is_passed,
    score_percent,
)
from course_supporter.homework.doors import DoorReading
from course_supporter.homework.path_checkpoint import FreezeReason
from course_supporter.homework.path_config import (
    PathKey,
    SubmissionState,
    load_path_config,
)
from course_supporter.homework.path_stages import (
    CRITERIA_EVALUATION,
    StageContext,
    StageOutcome,
    _execution,
)
from course_supporter.homework.task_context import load_task_context
from course_supporter.homework.verdict_explanation import explanation_facts
from course_supporter.llm.error_categories import (
    ErrorCategory,
    LadderExhaustedError,
    LadderStop,
)
from course_supporter.llm.finish_reason import FinishReason
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
    SubmissionCriterionVerdict,
    Tenant,
)
from course_supporter.storage.student_repository import StudentRepository
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
    VerdictRecord,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIST_ID = uuid.UUID("01999999-0000-7000-8000-0000000000a1")
_OTHER_LIST_ID = uuid.UUID("01999999-0000-7000-8000-0000000000a2")

_WORK = (
    "def fibonacci(n):\n"  # 1
    "    if n < 2:\n"  # 2
    "        return n\n"  # 3
    "    return fibonacci(n - 1) + fibonacci(n - 2)\n"  # 4
)
_FILES = (WorkFile("solution.py", _WORK, True),)
_C1_QUOTE = "def fibonacci(n):"
_P1_QUOTE = "return fibonacci(n - 1) + fibonacci(n - 2)"
_P2_MISSING = "Немає жодного тесту."


def _criterion(cid: str, weight: str, points: int = 0) -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": f"Criterion {cid}",
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


def _in_force(source_id: uuid.UUID = _LIST_ID) -> CriteriaInForce:
    return CriteriaInForce(
        criteria=_CRITERIA, layer=CriteriaLayer.MODEL, source_id=source_id
    )


def _met(item: str, quote: str | None) -> dict[str, Any]:
    return {"id": item, "verdict": "met", "quote": quote, "missing": None}


def _not_met(item: str, missing: str | None = _P2_MISSING) -> dict[str, Any]:
    return {"id": item, "verdict": "not_met", "quote": None, "missing": missing}


def _answer(*verdicts: dict[str, Any]) -> str:
    return json.dumps({"verdicts": list(verdicts)}, ensure_ascii=False)


_ALL_STAND = _answer(_met("c1", _C1_QUOTE), _met("c2.p1", _P1_QUOTE), _not_met("c2.p2"))


class _Source:
    """The criteria source double: answers what the test says, counts asks."""

    def __init__(self, answer: CriteriaInForce | CriteriaUnavailable) -> None:
        self._answer = answer
        self.asked: list[uuid.UUID] = []

    async def get_or_compose(
        self, authored_document_id: uuid.UUID
    ) -> CriteriaInForce | CriteriaUnavailable:
        self.asked.append(authored_document_id)
        return self._answer


class _Router:
    """Router double: records each request, runs the validator, answers.

    ``by`` names the rung that answered each request — the first of the ladder
    it was handed, unless the test says another.
    """

    def __init__(self, *contents: str, by: tuple[str, str] | None = None) -> None:
        self._contents = list(contents)
        self._by = by
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
        provider, model = self._by or (stage.ladder[0].provider, stage.ladder[0].model)
        self._by = None
        return StageResult(
            content=content, provider_used=provider, model_used=model, attempt_count=1
        )


async def _task(
    session: AsyncSession, root: CourseNode, *, language: str | None = None
) -> AuthoredDocument:
    doc = AuthoredDocument(
        course_node_id=root.id,
        course_root_id=root.id,
        source_type="text",
        source_url="https://example.com/task",
        task_type="task",
        language=language,
    )
    session.add(doc)
    await session.flush()
    return doc


async def _submission(
    session: AsyncSession,
    *,
    tenant: Tenant,
    student: Student,
    root: CourseNode,
    doc: AuthoredDocument,
) -> HomeworkSubmission:
    return await HomeworkRepository(session).create(
        tenant_id=tenant.id,
        student_id=student.id,
        course_node_id=root.id,
        node_id=root.id,
        authored_document_id=doc.id,
        file_url="s3://bucket/solution.py",
        file_type="text/plain",
        original_filename="solution.py",
    )


@pytest.fixture()
async def root(db_session: AsyncSession, seed_tenant: Tenant) -> CourseNode:
    node = make_root_course_node(tenant_id=seed_tenant.id, title="Course", order=0)
    db_session.add(node)
    await db_session.flush()
    return node


@pytest.fixture()
async def student(db_session: AsyncSession, seed_tenant: Tenant) -> Student:
    return await StudentRepository(db_session).create(
        tenant_id=seed_tenant.id, external_id=f"ext-{uuid.uuid4().hex[:8]}"
    )


@pytest.fixture()
async def doc(db_session: AsyncSession, root: CourseNode) -> AuthoredDocument:
    return await _task(db_session, root)


@pytest.fixture()
async def submission(
    db_session: AsyncSession,
    seed_tenant: Tenant,
    student: Student,
    root: CourseNode,
    doc: AuthoredDocument,
) -> HomeworkSubmission:
    return await _submission(
        db_session, tenant=seed_tenant, student=student, root=root, doc=doc
    )


def _context(
    session: AsyncSession,
    router: Any,
    submission: HomeworkSubmission,
    *,
    work: tuple[WorkFile, ...] = _FILES,
    submission_text: str = _WORK,
    door: DoorReading | None = None,
) -> StageContext:
    config = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")
    return StageContext(
        session=session,
        router=router,
        submission=submission,
        submission_text=submission_text,
        language="ukr",
        path_key=PathKey(AssignmentType.TASK, SubmissionState.FIRST),
        stage_name=CRITERIA_EVALUATION,
        stage=config.stages[CRITERIA_EVALUATION],
        session_factory=AsyncMock(),
        door=door,
        work=work,
    )


async def _evaluate(
    session: AsyncSession,
    router: Any,
    submission: HomeworkSubmission,
    *,
    source: _Source | None = None,
    work: tuple[WorkFile, ...] = _FILES,
    submission_text: str = _WORK,
    door: DoorReading | None = None,
) -> StageOutcome:
    context = _context(
        session,
        router,
        submission,
        work=work,
        submission_text=submission_text,
        door=door,
    )
    return await evaluate_criteria(
        context,
        execution=_execution(context),
        criteria_source=source or _Source(_in_force()),
    )


async def _rows(
    session: AsyncSession, submission: HomeworkSubmission
) -> dict[str, SubmissionCriterionVerdict]:
    rows = await SubmissionVerdictRepository(session).list_for_submission(
        tenant_id=submission.tenant_id, submission_id=submission.id
    )
    return {row.item_id: row for row in rows}


class TestOneRequestWhenEveryVerdictStands:
    async def test_the_rows_are_written_with_their_places(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        router = _Router(_ALL_STAND)

        outcome = await _evaluate(db_session, router, submission)

        assert outcome == StageOutcome.ok()
        (request,) = router.requests
        assert request["stage_name"] == CRITERIA_EVALUATION
        assert request["ladder"] == [
            ("dashscope", "qwen3.7-max"),
            ("deepseek_thinking", "deepseek-v4-pro"),
        ]
        assert request["render"]["items"] == ["c1", "c2.p1", "c2.p2"]
        rows = await _rows(db_session, submission)
        assert list(rows) == ["c1", "c2", "c2.p1", "c2.p2"]
        assert rows["c1"].verdict == VerdictValue.MET
        assert (rows["c1"].quote, rows["c1"].quote_file) == (_C1_QUOTE, "solution.py")
        assert (rows["c1"].quote_line_start, rows["c1"].quote_line_end) == (1, 1)
        assert rows["c2.p1"].verdict == VerdictValue.MET
        assert (rows["c2.p1"].quote_line_start, rows["c2.p1"].quote_line_end) == (4, 4)
        assert rows["c2.p2"].verdict == VerdictValue.NOT_MET
        assert rows["c2.p2"].missing == _P2_MISSING
        # c2 is the code's: every point or nothing (decision 3).
        assert rows["c2"].verdict == VerdictValue.NOT_MET
        assert rows["c2"].model_verdict is None
        assert rows["c2"].item_kind == VerdictItemKind.CRITERION
        assert rows["c2"].weight == WeightCategory.SHOULD
        assert {row.criteria_source_id for row in rows.values()} == {_LIST_ID}
        assert not any(row.retried for row in rows.values())


class TestTheRepeat:
    """Lock 4: only what cannot stand is asked again; a second miss is flagged."""

    async def test_only_the_unfound_quote_is_asked_again_of_the_rung_that_answered(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        router = _Router(
            _answer(
                _met("c1", "def fib(n): pass"),
                _met("c2.p1", _P1_QUOTE),
                _not_met("c2.p2"),
            ),
            _answer(_met("c1", _C1_QUOTE)),
        )

        await _evaluate(db_session, router, submission)

        first, again = router.requests
        assert again["render"]["items"] == ["c1"]
        assert [note["id"] for note in again["render"]["repeat"]] == ["c1"]
        assert again["ladder"] == [("dashscope", "qwen3.7-max")]
        # The repeat keeps the stage's own limits.
        assert again["money_ceiling_usd"] == first["money_ceiling_usd"] == 0.55
        assert again["stop_on_output_ceiling"] is True
        rows = await _rows(db_session, submission)
        assert rows["c1"].verdict == VerdictValue.MET
        assert rows["c1"].quote == _C1_QUOTE
        assert rows["c1"].retried is True
        assert rows["c1"].quote_not_found is False
        assert rows["c2.p1"].retried is False

    async def test_a_quote_not_found_twice_reads_not_met_flagged_for_the_author(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        router = _Router(
            _answer(
                _met("c1", "def fib(n): pass"),
                _met("c2.p1", _P1_QUOTE),
                _not_met("c2.p2"),
            ),
            _answer(_met("c1", "def fib(n): return n")),
        )

        await _evaluate(db_session, router, submission)

        rows = await _rows(db_session, submission)
        assert rows["c1"].verdict == VerdictValue.NOT_MET
        assert rows["c1"].model_verdict == VerdictValue.MET
        assert rows["c1"].quote_not_found is True
        assert rows["c1"].retried is True
        assert rows["c1"].quote_file is None

    @pytest.mark.parametrize(
        ("said", "reason"),
        [
            pytest.param(_met("c1", None), RepeatReason.NO_QUOTE, id="no-quote"),
            pytest.param(
                _met("c1", "def fibonacci(n):\n    if n < 2:"),
                RepeatReason.QUOTE_NOT_ONE_LINE,
                id="two-lines",
            ),
            pytest.param(
                _met("c1", _C1_QUOTE + "#" * MAX_QUOTE_CHARS),
                RepeatReason.QUOTE_TOO_LONG,
                id="too-long",
            ),
            pytest.param(
                _met("c1", "n < 2:"), RepeatReason.QUOTE_TOO_SHORT, id="too-short"
            ),
            pytest.param(
                _met("c1", "def fib(n): pass"),
                RepeatReason.QUOTE_NOT_FOUND,
                id="not-found",
            ),
            pytest.param(
                _not_met("c1", None), RepeatReason.NO_SENTENCE, id="no-sentence"
            ),
        ],
    )
    async def test_each_defect_of_one_verdict_is_asked_again_with_its_reason(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        said: dict[str, Any],
        reason: RepeatReason,
    ) -> None:
        router = _Router(
            _answer(said, _met("c2.p1", _P1_QUOTE), _not_met("c2.p2")),
            _answer(_met("c1", _C1_QUOTE)),
        )

        await _evaluate(db_session, router, submission)

        _, again = router.requests
        (note,) = again["render"]["repeat"]
        assert note["id"] == "c1"
        assert note["problem"] == RepeatItem("c1", reason).feedback

    async def test_when_the_fallback_answered_the_repeat_goes_to_it_alone(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        router = _Router(
            _answer(
                _met("c1", "def fib(n): pass"),
                _met("c2.p1", _P1_QUOTE),
                _not_met("c2.p2"),
            ),
            _answer(_met("c1", _C1_QUOTE)),
            by=("deepseek_thinking", "deepseek-v4-pro"),
        )

        await _evaluate(db_session, router, submission)

        _, again = router.requests
        assert again["ladder"] == [("deepseek_thinking", "deepseek-v4-pro")]


class TestTheSafeguard:
    """Lock 5: within one list, an earlier "met" still in the work stands."""

    async def _earlier(
        self,
        session: AsyncSession,
        *,
        tenant: Tenant,
        student: Student,
        root: CourseNode,
        doc: AuthoredDocument,
        source_id: uuid.UUID,
    ) -> HomeworkSubmission:
        earlier = await _submission(
            session, tenant=tenant, student=student, root=root, doc=doc
        )
        await SubmissionVerdictRepository(session).replace_for_submission(
            tenant_id=tenant.id,
            submission_id=earlier.id,
            criteria_layer=CriteriaLayer.MODEL,
            criteria_source_id=source_id,
            records=[
                VerdictRecord(
                    item_id="c1",
                    item_kind=VerdictItemKind.CRITERION,
                    weight=WeightCategory.MUST,
                    verdict=VerdictValue.MET,
                    model_verdict=VerdictValue.MET,
                    quote=_C1_QUOTE,
                    quote_file="solution.py",
                    quote_lines=(1, 1),
                )
            ],
        )
        return earlier

    async def test_an_earlier_met_of_the_same_list_is_kept_and_logged(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        student: Student,
        root: CourseNode,
        doc: AuthoredDocument,
    ) -> None:
        earlier = await self._earlier(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=root,
            doc=doc,
            source_id=_LIST_ID,
        )
        current = await _submission(
            db_session, tenant=seed_tenant, student=student, root=root, doc=doc
        )
        router = _Router(
            _answer(
                _not_met("c1", "Функцію не визначено."),
                _met("c2.p1", _P1_QUOTE),
                _not_met("c2.p2"),
            )
        )

        with capture_logs() as logs:
            await _evaluate(db_session, router, current)

        rows = await _rows(db_session, current)
        assert rows["c1"].verdict == VerdictValue.MET
        assert rows["c1"].model_verdict == VerdictValue.NOT_MET
        assert rows["c1"].safeguard_submission_id == earlier.id
        assert rows["c1"].quote == _C1_QUOTE
        kept = [e for e in logs if e["event"] == "criteria_evaluation_safeguard_kept"]
        assert [(e["item_id"], e["earlier_submission_id"]) for e in kept] == [
            ("c1", str(earlier.id))
        ]

    async def test_another_lists_met_is_not_kept(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        student: Student,
        root: CourseNode,
        doc: AuthoredDocument,
    ) -> None:
        await self._earlier(
            db_session,
            tenant=seed_tenant,
            student=student,
            root=root,
            doc=doc,
            source_id=_OTHER_LIST_ID,
        )
        current = await _submission(
            db_session, tenant=seed_tenant, student=student, root=root, doc=doc
        )
        router = _Router(
            _answer(
                _not_met("c1", "Функцію не визначено."),
                _met("c2.p1", _P1_QUOTE),
                _not_met("c2.p2"),
            )
        )

        await _evaluate(db_session, router, current)

        rows = await _rows(db_session, current)
        assert rows["c1"].verdict == VerdictValue.NOT_MET
        assert rows["c1"].safeguard_submission_id is None


class TestWithoutAList:
    """Lock 6: no list — the revision is held, and nothing is asked."""

    @pytest.mark.parametrize("reason", list(UnavailableReason))
    async def test_the_stage_freezes_without_a_call(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        reason: UnavailableReason,
    ) -> None:
        router = _Router()
        source = _Source(CriteriaUnavailable(reason))

        outcome = await _evaluate(db_session, router, submission, source=source)

        assert outcome == StageOutcome.freezes(FreezeReason.CRITERIA_UNAVAILABLE)
        assert outcome.carry_on is False
        assert outcome.terminal_status is None
        assert source.asked == [submission.authored_document_id]
        assert router.requests == []
        assert await _rows(db_session, submission) == {}


class TestTheWaitingRevisionsAreContinued:
    """Entry 1 of the continuation (task 09b, K4; ``PRE-FLIGHT.md`` 9.2)."""

    _RESUME = "course_supporter.homework.criteria_evaluation.resume_awaiting_criteria"

    @staticmethod
    def _with_a_queue(context: StageContext, session: AsyncSession) -> StageContext:
        @asynccontextmanager
        async def own_session() -> AsyncIterator[AsyncSession]:
            yield session

        # Stands in for a session factory: opened and closed, nothing else.
        factory = cast("async_sessionmaker[AsyncSession]", own_session)
        return replace(context, arq=MagicMock(), session_factory=factory)

    async def _evaluate_with_a_queue(
        self,
        session: AsyncSession,
        submission: HomeworkSubmission,
        source: _Source,
    ) -> StageOutcome:
        context = self._with_a_queue(
            _context(session, _Router(_ALL_STAND), submission), session
        )
        return await evaluate_criteria(
            context, execution=_execution(context), criteria_source=source
        )

    async def test_the_list_the_stage_got_continues_its_tasks_revisions(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        with patch(self._RESUME, new=AsyncMock(return_value=2)) as resume:
            outcome = await self._evaluate_with_a_queue(
                db_session, submission, _Source(_in_force())
            )

        assert outcome == StageOutcome.ok()
        resume.assert_awaited_once()
        assert resume.await_args is not None
        assert resume.await_args.kwargs == {
            "tenant_id": submission.tenant_id,
            "authored_document_id": submission.authored_document_id,
        }

    async def test_no_list_continues_nothing(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        source = _Source(CriteriaUnavailable(UnavailableReason.COMPOSITION_FAILED))
        with patch(self._RESUME, new=AsyncMock()) as resume:
            outcome = await self._evaluate_with_a_queue(db_session, submission, source)

        assert outcome == StageOutcome.freezes(FreezeReason.CRITERIA_UNAVAILABLE)
        resume.assert_not_awaited()

    async def test_a_queue_that_refuses_costs_this_submission_nothing(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        failing = AsyncMock(side_effect=ConnectionError("the queue is away"))
        with (
            patch(self._RESUME, new=failing),
            capture_logs() as logs,
        ):
            outcome = await self._evaluate_with_a_queue(
                db_session, submission, _Source(_in_force())
            )

        assert outcome == StageOutcome.ok()
        assert set(await _rows(db_session, submission)) == {
            "c1",
            "c2",
            "c2.p1",
            "c2.p2",
        }
        assert [
            e for e in logs if e["event"] == "criteria_evaluation_continuation_failed"
        ]

    async def test_without_a_queue_nothing_is_tried(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        with patch(self._RESUME, new=AsyncMock()) as resume:
            outcome = await _evaluate(db_session, _Router(_ALL_STAND), submission)

        assert outcome == StageOutcome.ok()
        resume.assert_not_awaited()


def _provider(*responses: LLMResponse) -> Any:
    provider = AsyncMock(spec=LLMProvider)
    provider.enabled = True
    provider.complete = AsyncMock(side_effect=list(responses))
    provider.classify_error = lambda _exc: ErrorCategory.SEMANTIC
    return provider


class TestACutAnswer:
    async def test_it_stops_the_stage_after_one_call_and_writes_nothing(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        qwen = _provider(
            LLMResponse(
                content=_ALL_STAND[:60],
                provider="dashscope",
                model_id="qwen3.7-max",
                finish_reason=FinishReason.OUTPUT_CEILING,
            )
        )
        deepseek = _provider()
        router = StageRouter(
            LadderConfig(),
            {"dashscope": qwen, "deepseek_thinking": deepseek},
            registry=load_registry(_REPO_ROOT / "config" / "external_services.yaml"),
            prompt_base_path=_REPO_ROOT,
        )

        with pytest.raises(LadderExhaustedError) as caught:
            await _evaluate(db_session, router, submission)

        assert caught.value.stop is LadderStop.OUTPUT_CEILING
        assert qwen.complete.await_count == 1
        deepseek.complete.assert_not_awaited()
        assert await _rows(db_session, submission) == {}


class TestTheLanguageOfTheSentences:
    """PRE-FLIGHT 9.9: the course's language — the task's, else the root's."""

    async def test_a_task_without_a_language_takes_the_roots(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        router = _Router(_ALL_STAND)

        await _evaluate(db_session, router, submission)

        assert router.requests[0]["render"]["language"] == "Ukrainian"

    async def test_the_tasks_own_language_comes_first(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        student: Student,
        root: CourseNode,
    ) -> None:
        doc = await _task(db_session, root, language="eng")
        submission = await _submission(
            db_session, tenant=seed_tenant, student=student, root=root, doc=doc
        )
        router = _Router(_ALL_STAND)

        await _evaluate(db_session, router, submission)

        assert router.requests[0]["render"]["language"] == "English"


class TestWithoutFiles:
    async def test_a_work_without_files_is_a_defect_not_a_verdict(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        """Every "met" would read "not met" with nothing to find quotes in."""
        router = _Router(_ALL_STAND)

        with pytest.raises(ValueError, match="file by file"):
            await _evaluate(db_session, router, submission, work=())

        assert router.requests == []


# The student's problem 2 (2026-11-07), through the stage: a mandatory point
# asks for the line ".venv/" in .gitignore — six characters, under the
# minimum of a quote, and the whole line. c1 "must" by one point, c2 "should":
# weights 3 + 2 = 5.
_GITIGNORE_CRITERIA = (_criterion("c1", "must", points=1), _criterion("c2", "should"))
_MAIN_PY = "print('hello, world')\n"
_VENV = ".venv/"
_C2_QUOTE = "print('hello, world')"
_LENGTH_SENTENCE = (
    "Рядок .venv/ є, але цитата має менше 8 символів, тож її не прийнято."
)


def _project(gitignore: str) -> tuple[WorkFile, ...]:
    return (
        WorkFile("main.py", _MAIN_PY, True),
        WorkFile(".gitignore", gitignore, True),
    )


def _framed(gitignore: str) -> str:
    """The work as the doors frame it for the model."""
    return f"--- main.py ---\n{_MAIN_PY}--- .gitignore ---\n{gitignore}"


def _gitignore_source() -> _Source:
    return _Source(
        CriteriaInForce(
            criteria=_GITIGNORE_CRITERIA, layer=CriteriaLayer.MODEL, source_id=_LIST_ID
        )
    )


async def _evaluate_project(
    session: AsyncSession,
    router: _Router,
    submission: HomeworkSubmission,
    gitignore: str,
) -> dict[str, SubmissionCriterionVerdict]:
    outcome = await _evaluate(
        session,
        router,
        submission,
        source=_gitignore_source(),
        work=_project(gitignore),
        submission_text=_framed(gitignore),
    )
    assert outcome == StageOutcome.ok()
    return await _rows(session, submission)


class TestAShortWholeLineIsAQuote:
    """The whole-line lock, end to end through the stage.

    The recorded answer, the rows written, the score and the pass recounted
    from the stored rows as the review's builder does.
    """

    async def _assert_met_on_line_3_without_a_repeat(
        self,
        session: AsyncSession,
        submission: HomeworkSubmission,
        gitignore: str,
    ) -> None:
        router = _Router(_answer(_met("c1.p1", _VENV), _met("c2", _C2_QUOTE)))

        rows = await _evaluate_project(session, router, submission, gitignore)

        assert len(router.requests) == 1
        point = rows["c1.p1"]
        assert point.verdict == VerdictValue.MET
        assert (point.quote, point.quote_file) == (_VENV, ".gitignore")
        assert (point.quote_line_start, point.quote_line_end) == (3, 3)
        assert (point.retried, point.quote_not_found) == (False, False)
        assert rows["c1"].verdict == VerdictValue.MET
        stored = list(rows.values())
        assert is_passed(stored) is True
        assert score_percent(stored) == 100

    async def test_the_venv_line_of_a_gitignore_is_met_without_a_repeat(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        await self._assert_met_on_line_3_without_a_repeat(
            db_session, submission, "__pycache__/\n*.pyc\n.venv/\n"
        )

    async def test_the_venv_line_with_spaces_around_is_met_without_a_repeat(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        await self._assert_met_on_line_3_without_a_repeat(
            db_session, submission, "__pycache__/\n*.pyc\n   .venv/  \t\n"
        )


_PIECE = "__pycache__/\n*.pyc\ncache/.venv/old\n"  # ".venv/" is a piece of line 3


class TestAShortPieceOfALongerLine:
    """A short quote inside a longer line is too short, as before 2026-11-07."""

    async def test_it_is_asked_again_as_too_short(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        router = _Router(
            _answer(_met("c1.p1", _VENV), _met("c2", _C2_QUOTE)),
            _answer(_met("c1.p1", "cache/.venv/old")),
        )

        rows = await _evaluate_project(db_session, router, submission, _PIECE)

        _, again = router.requests
        (note,) = again["render"]["repeat"]
        assert note["id"] == "c1.p1"
        assert (
            note["problem"]
            == RepeatItem("c1.p1", RepeatReason.QUOTE_TOO_SHORT).feedback
        )
        assert rows["c1.p1"].verdict == VerdictValue.MET
        assert rows["c1.p1"].retried is True
        assert (rows["c1.p1"].quote_line_start, rows["c1.p1"].quote_line_end) == (3, 3)


class TestAMetTakenBackAfterItsQuoteFell:
    """Unconfirmed: it does not fail the work, and the explanation is told
    the state, never the check's mechanics."""

    async def _settled(
        self, session: AsyncSession, submission: HomeworkSubmission
    ) -> dict[str, SubmissionCriterionVerdict]:
        router = _Router(
            _answer(_met("c1.p1", _VENV), _met("c2", _C2_QUOTE)),
            _answer(_not_met("c1.p1", _LENGTH_SENTENCE)),
        )
        rows = await _evaluate_project(session, router, submission, _PIECE)
        assert len(router.requests) == 2
        return rows

    async def test_it_reads_unconfirmed_and_does_not_fail_the_work(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        rows = await self._settled(db_session, submission)

        point = rows["c1.p1"]
        assert point.verdict == VerdictValue.NOT_MET
        assert point.model_verdict == VerdictValue.MET
        assert (point.quote, point.quote_file) == (_VENV, None)
        assert point.missing is None
        assert (point.retried, point.quote_not_found) == (True, True)
        criterion = rows["c1"]
        assert (criterion.verdict, criterion.quote_not_found) == (
            VerdictValue.NOT_MET,
            True,
        )
        stored = list(rows.values())
        assert is_passed(stored) is True
        # 3 (c1, unconfirmed, counted as met) + 2 (c2) of 5 -> 100.
        assert score_percent(stored) == 100

    async def test_the_explanation_is_shown_its_state_and_no_mechanics(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        rows = await self._settled(db_session, submission)

        facts = explanation_facts(_GITIGNORE_CRITERIA, rows.values())
        shown = render_context(
            ExplanationInput(
                task_title="Project setup",
                task_description="Set the project up.",
                task_text="Add a .gitignore that keeps the virtual environment out.",
                facts=facts,
                submission_text=_framed(_PIECE),
                language="Ukrainian",
                course_links={},
            )
        )

        c1, _ = shown["criteria"]
        assert c1["points"] == [
            {
                "id": "c1.p1",
                "text": "Point 1 of c1",
                "verdict": "not_met",
                "missing": None,
                "quote_not_found": True,
            }
        ]
        assert shown["result"] == {"passed": True, "score": 100}
        rendered = json.dumps(shown, ensure_ascii=False)
        assert _LENGTH_SENTENCE not in rendered
        for word in ("символ", "цитат", "не прийнято", "не знайдено", "too short"):
            assert word not in rendered, word


class TestARealNotMetStays:
    """A "not met" of the first answer fails a "must", as before 2026-11-07;
    so does a "met" on a quote the work lacks — of any length — taken back on
    the repeat."""

    async def test_a_not_met_in_the_first_answer_fails_the_work(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        missing = "У .gitignore немає рядка .venv/."
        router = _Router(_answer(_not_met("c1.p1", missing), _met("c2", _C2_QUOTE)))

        rows = await _evaluate_project(
            db_session, router, submission, "__pycache__/\n*.pyc\n"
        )

        assert len(router.requests) == 1
        point = rows["c1.p1"]
        assert (point.verdict, point.quote_not_found, point.missing) == (
            VerdictValue.NOT_MET,
            False,
            missing,
        )
        assert (rows["c1"].verdict, rows["c1"].quote_not_found) == (
            VerdictValue.NOT_MET,
            False,
        )
        stored = list(rows.values())
        assert is_passed(stored) is False
        # 2 (c2) of 5 -> 40.
        assert score_percent(stored) == 40

    async def test_a_met_on_a_quote_the_work_lacks_taken_back_fails_the_work(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        missing = "У .gitignore немає рядка venv/."
        router = _Router(
            _answer(_met("c1.p1", "venv/  # local env"), _met("c2", _C2_QUOTE)),
            _answer(_not_met("c1.p1", missing)),
        )

        rows = await _evaluate_project(
            db_session, router, submission, "__pycache__/\n*.pyc\n"
        )

        _, again = router.requests
        (note,) = again["render"]["repeat"]
        assert (
            note["problem"]
            == RepeatItem("c1.p1", RepeatReason.QUOTE_NOT_FOUND).feedback
        )
        point = rows["c1.p1"]
        assert (point.verdict, point.model_verdict) == (
            VerdictValue.NOT_MET,
            VerdictValue.NOT_MET,
        )
        assert (point.quote_not_found, point.missing, point.retried) == (
            False,
            missing,
            True,
        )
        assert (rows["c1"].verdict, rows["c1"].quote_not_found) == (
            VerdictValue.NOT_MET,
            False,
        )
        stored = list(rows.values())
        assert is_passed(stored) is False
        # 2 (c2) of 5 -> 40.
        assert score_percent(stored) == 40

    async def test_a_too_short_met_the_work_lacks_taken_back_fails_the_work(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        missing = "У .gitignore немає рядка .venv/."
        router = _Router(
            _answer(_met("c1.p1", _VENV), _met("c2", _C2_QUOTE)),
            _answer(_not_met("c1.p1", missing)),
        )

        rows = await _evaluate_project(
            db_session, router, submission, "__pycache__/\n*.pyc\n"
        )

        _, again = router.requests
        (note,) = again["render"]["repeat"]
        assert (
            note["problem"]
            == RepeatItem("c1.p1", RepeatReason.QUOTE_TOO_SHORT).feedback
        )
        point = rows["c1.p1"]
        assert (point.verdict, point.model_verdict) == (
            VerdictValue.NOT_MET,
            VerdictValue.NOT_MET,
        )
        assert (point.quote_not_found, point.missing) == (False, missing)
        assert (rows["c1"].verdict, rows["c1"].quote_not_found) == (
            VerdictValue.NOT_MET,
            False,
        )
        stored = list(rows.values())
        assert is_passed(stored) is False
        # 2 (c2) of 5 -> 40.
        assert score_percent(stored) == 40


# Project chain K3: the author's base the student built on. The work is
# solution.py as above; the author gave its first line -- the signature -- and
# the student wrote the body.
_BASE_FILES = (
    WorkFile("README.md", "# Fib\n", True),
    WorkFile("solution.py", "def fibonacci(n):\n    pass\n", True),
)
_PROJECT_DOOR = DoorReading(text=_WORK, files=_FILES, base_files=_BASE_FILES)


class TestAQuoteOfTheProjectsBase:
    """Lock b on a real database: the row the author reads."""

    async def _evaluate_with_base(
        self, session: AsyncSession, router: _Router, submission: HomeworkSubmission
    ) -> dict[str, SubmissionCriterionVerdict]:
        outcome = await _evaluate(session, router, submission, door=_PROJECT_DOOR)
        assert outcome == StageOutcome.ok()
        return await _rows(session, submission)

    async def test_it_is_asked_again_and_a_quote_of_the_students_stands(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        router = _Router(
            _answer(_met("c1", _C1_QUOTE), _met("c2.p1", _P1_QUOTE), _not_met("c2.p2")),
            _answer(_met("c1", "return fibonacci(n - 1)")),
        )

        rows = await self._evaluate_with_base(db_session, router, submission)

        _, again = router.requests
        assert again["render"]["items"] == ["c1"]
        (note,) = again["render"]["repeat"]
        assert (
            note["problem"] == RepeatItem("c1", RepeatReason.QUOTE_FROM_BASE).feedback
        )
        assert (rows["c1"].verdict, rows["c1"].quote_line_start) == (
            VerdictValue.MET,
            4,
        )
        assert (rows["c1"].retried, rows["c1"].quote_from_base) == (True, False)
        assert not any(row.quote_from_base for row in rows.values())

    async def test_a_second_quote_of_the_base_is_not_met_with_the_flag(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        router = _Router(
            _answer(_met("c1", _C1_QUOTE), _met("c2.p1", _P1_QUOTE), _not_met("c2.p2")),
            _answer(_met("c1", _C1_QUOTE)),
        )

        with capture_logs() as logs:
            rows = await self._evaluate_with_base(db_session, router, submission)

        row = rows["c1"]
        assert (row.verdict, row.model_verdict) == (
            VerdictValue.NOT_MET,
            VerdictValue.MET,
        )
        assert (row.quote, row.quote_file) == (_C1_QUOTE, None)
        assert (row.quote_from_base, row.quote_not_found, row.retried) == (
            True,
            False,
            True,
        )
        assert [
            log["item_id"]
            for log in logs
            if log["event"] == ("criteria_evaluation_quote_from_base")
        ] == ["c1"]
        stored = list(rows.values())
        assert is_passed(stored) is False
        # c1 "must" (3) not met, c2 (2) not met: 0 of 5.
        assert score_percent(stored) == 0

    async def test_a_met_taken_back_is_not_met_with_the_flag(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        missing = "Сигнатуру дав автор; власного коду немає."
        router = _Router(
            _answer(_met("c1", _C1_QUOTE), _met("c2.p1", _P1_QUOTE), _not_met("c2.p2")),
            _answer(_not_met("c1", missing)),
        )

        rows = await self._evaluate_with_base(db_session, router, submission)

        row = rows["c1"]
        assert (row.verdict, row.model_verdict, row.missing) == (
            VerdictValue.NOT_MET,
            VerdictValue.NOT_MET,
            missing,
        )
        assert (row.quote_from_base, row.quote_not_found) == (True, False)

    async def test_a_made_up_second_quote_is_not_met_with_the_flag(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        """Answer 1 (#649): the model never showed the student's code."""
        router = _Router(
            _answer(_met("c1", _C1_QUOTE), _met("c2.p1", _P1_QUOTE), _not_met("c2.p2")),
            _answer(_met("c1", "def fib(n): pass")),
        )

        rows = await self._evaluate_with_base(db_session, router, submission)

        row = rows["c1"]
        assert (row.verdict, row.model_verdict, row.quote) == (
            VerdictValue.NOT_MET,
            VerdictValue.MET,
            "def fib(n): pass",
        )
        assert (row.quote_from_base, row.quote_not_found) == (True, False)
        assert is_passed(list(rows.values())) is False

    async def test_a_criterion_carries_its_points_flag(
        self, db_session: AsyncSession, submission: HomeworkSubmission
    ) -> None:
        """Answer 2 (#649): c2 is checked by points; c2.p1 fell for the base."""
        router = _Router(
            _answer(
                _met("c1", _P1_QUOTE),
                _met("c2.p1", _C1_QUOTE),
                _met("c2.p2", _P1_QUOTE),
            ),
            _answer(_met("c2.p1", _C1_QUOTE)),
        )

        rows = await self._evaluate_with_base(db_session, router, submission)

        assert (rows["c2.p1"].verdict, rows["c2.p1"].quote_from_base) == (
            VerdictValue.NOT_MET,
            True,
        )
        assert (rows["c2"].verdict, rows["c2"].quote_from_base) == (
            VerdictValue.NOT_MET,
            True,
        )
        assert (rows["c1"].quote_from_base, rows["c2.p2"].quote_from_base) == (
            False,
            False,
        )


class TestATaskWithoutABase:
    """Lock d: a task's door carries no base, and the model is asked as before."""

    async def test_one_request_rendered_as_without_the_rule(
        self,
        db_session: AsyncSession,
        doc: AuthoredDocument,
        submission: HomeworkSubmission,
    ) -> None:
        router = _Router(_ALL_STAND)
        door = DoorReading(text=_WORK, files=_FILES)

        await _evaluate(db_session, router, submission, door=door)

        (request,) = router.requests
        title, description, text = await load_task_context(db_session, doc.id)
        shown = EvaluationInput(
            task_title=title,
            task_description=description,
            task_text=text,
            criteria=_CRITERIA,
            submission_text=_WORK,
            language=await _course_language(db_session, submission),
        )
        expected = evaluation_render_context(
            shown, expected=["c1", "c2.p1", "c2.p2"], repeat=[]
        )
        assert json.dumps(request["render"], ensure_ascii=False, sort_keys=True) == (
            json.dumps(expected, ensure_ascii=False, sort_keys=True)
        )
        rows = await _rows(db_session, submission)
        assert not any(row.quote_from_base or row.retried for row in rows.values())
