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
  and writes nothing.

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
from structlog.testing import capture_logs

from course_supporter.criteria_kinds import (
    CriteriaLayer,
    VerdictItemKind,
    VerdictValue,
    WeightCategory,
)
from course_supporter.homework.criteria_evaluation import evaluate_criteria
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
)
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
) -> StageContext:
    config = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")
    return StageContext(
        session=session,
        router=router,
        submission=submission,
        submission_text=_WORK,
        language="ukr",
        path_key=PathKey(AssignmentType.TASK, SubmissionState.FIRST),
        stage_name=CRITERIA_EVALUATION,
        stage=config.stages[CRITERIA_EVALUATION],
        session_factory=AsyncMock(),
        work=work,
    )


async def _evaluate(
    session: AsyncSession,
    router: Any,
    submission: HomeworkSubmission,
    *,
    source: _Source | None = None,
    work: tuple[WorkFile, ...] = _FILES,
) -> StageOutcome:
    context = _context(session, router, submission, work=work)
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
