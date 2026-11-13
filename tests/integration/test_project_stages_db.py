"""A project's own two stages on recorded answers (project chain K4).

``project_criteria_evaluation`` and ``project_review_explanation`` with their
real descriptions from ``config/submission_paths.yaml``, resolved by name as
the path's body resolves them, on what the project's door really reads
(``tests/_helpers/recorded_project.py``) — through the real router over two
fake providers, so the project's own prompts are loaded and rendered as in
production, and the real verdict repository on a live database:

* lock v — a quote of the student's NEW file stands; a quote of the author's
  NEIGHBOR file, or of a line the author left in a changed file, is asked
  about again with its reason (``RepeatReason.QUOTE_FROM_BASE``); a second
  one is "not met" with ``quote_from_base`` — chain K3, through the new stage;
* lock h — the explanation of those verdicts is shown the flag where it
  stands, and an answer that says it in plain words is kept as it was
  written, with none of the check's mechanics.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.criteria_kinds import CriteriaLayer, VerdictValue
from course_supporter.homework.criteria_form import Criterion, criteria_to_document
from course_supporter.homework.criteria_list_service import CriteriaInForce
from course_supporter.homework.criteria_verdicts import RepeatItem, RepeatReason
from course_supporter.homework.doors import DoorReading
from course_supporter.homework.path_config import (
    PathKey,
    SubmissionState,
    load_path_config,
)
from course_supporter.homework.path_stages import (
    PROJECT_CRITERIA_EVALUATION,
    PROJECT_REVIEW_EXPLANATION,
    StageContext,
    StageOutcome,
    get_stage_executor,
)
from course_supporter.llm.error_categories import ErrorCategory
from course_supporter.llm.ladder_config import LadderConfig
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import load_registry
from course_supporter.llm.schemas import LLMRequest, LLMResponse
from course_supporter.llm.stage_router import StageRouter
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
)
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)
from tests._helpers.course_node_factory import make_root_course_node
from tests._helpers.recorded_project import (
    QUOTE_OF_AUTHORS_LINE,
    QUOTE_OF_CHANGED,
    QUOTE_OF_NEIGHBOR,
    QUOTE_OF_NEW,
    read_recorded_project,
)

pytestmark = pytest.mark.requires_db

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FROM_BASE = RepeatItem("c2.p1", RepeatReason.QUOTE_FROM_BASE).feedback

# What a remark to the student must never name: the check's mechanics.
_MECHANICS = ("цитат", "перепит", "парсер", "quote", "repeat", "parser")


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


# c1 "must" on its own; c2 "should" by two points: p1 — reading the lines,
# p2 — printing the statistics.
_CRITERIA = (_criterion("c1", "must"), _criterion("c2", "should", points=2))


def _verdicts(*verdicts: dict[str, Any]) -> str:
    return json.dumps({"verdicts": list(verdicts)}, ensure_ascii=False)


def _met(item: str, quote: str) -> dict[str, Any]:
    return {"id": item, "verdict": "met", "quote": quote, "missing": None}


_HUMAN_EXPLANATION = json.dumps(
    {
        "passed": False,
        "why": (
            "Ви додали власний модуль статистики, і середню довжину рядка "
            "пораховано охайно."
        ),
        "remarks": [
            {
                "id": "c2",
                "what": (
                    "Читання рядків з файлу є лише в коді стартового проєкту, "
                    "а не у вашій роботі."
                ),
                "why": (
                    "Завдання просило, щоб цю частину написали саме ви: без "
                    "неї не видно, що ви розібралися з читанням файлів."
                ),
                "todo": (
                    "Напишіть читання файлу у власному модулі й скористайтеся "
                    "ним у main."
                ),
                "links": [],
            }
        ],
        "mentor_voice": None,
    },
    ensure_ascii=False,
)


def _provider(*contents: str) -> Any:
    provider = AsyncMock(spec=LLMProvider)
    provider.enabled = True
    provider.complete = AsyncMock(
        side_effect=[
            LLMResponse(content=content, provider="p", model_id="m")
            for content in contents
        ]
    )
    provider.classify_error = lambda _exc: ErrorCategory.SEMANTIC
    return provider


def _router(qwen: Any) -> StageRouter:
    """The real router; qwen answers, the fallbacks are never asked."""
    return StageRouter(
        LadderConfig(),
        {"dashscope": qwen, "deepseek_thinking": _provider(), "deepseek": _provider()},
        registry=load_registry(_REPO_ROOT / "config" / "external_services.yaml"),
        prompt_base_path=_REPO_ROOT,
    )


def _sent(provider: Any) -> list[LLMRequest]:
    return [call.args[0] for call in provider.complete.await_args_list]


@pytest.fixture()
async def reading(monkeypatch: pytest.MonkeyPatch) -> DoorReading:
    return await read_recorded_project(monkeypatch)


@pytest.fixture()
async def root(db_session: AsyncSession, seed_tenant: Tenant) -> CourseNode:
    node = make_root_course_node(tenant_id=seed_tenant.id, title="Course", order=0)
    db_session.add(node)
    await db_session.flush()
    return node


@pytest.fixture()
async def doc(db_session: AsyncSession, root: CourseNode) -> AuthoredDocument:
    document = AuthoredDocument(
        course_node_id=root.id,
        course_root_id=root.id,
        source_type="text",
        source_url=f"https://example.com/{uuid.uuid4().hex}",
        task_type="project",
    )
    db_session.add(document)
    await db_session.flush()
    return document


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
        file_url="s3://bucket/proj.zip",
        file_type="application/zip",
        original_filename="proj.zip",
    )


@pytest.fixture()
async def authors_list(
    db_session: AsyncSession, doc: AuthoredDocument, monkeypatch: pytest.MonkeyPatch
) -> CriteriaInForce:
    """The author's list, live — and the one the evaluation is handed."""
    edit = await TaskCriteriaOverrideRepository(db_session).replace(
        authored_document_id=doc.id,
        source_content_hash="h" * 64,
        source_task_type="project",
        criteria=criteria_to_document(_CRITERIA),
    )
    in_force = CriteriaInForce(
        criteria=_CRITERIA, layer=CriteriaLayer.AUTHOR, source_id=edit.id
    )

    class _Source:
        async def get_or_compose(self, _document_id: uuid.UUID) -> CriteriaInForce:
            return in_force

    monkeypatch.setattr(
        "course_supporter.homework.criteria_evaluation.build_criteria_list_service",
        lambda *_args, **_kwargs: _Source(),
    )
    return in_force


def _context(
    session: AsyncSession,
    router: StageRouter,
    submission: HomeworkSubmission,
    reading: DoorReading,
    stage_name: str,
) -> StageContext:
    """What the path's body hands a project's stage (``path_runner._run_stage``)."""
    config = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")
    return StageContext(
        session=session,
        router=router,
        submission=submission,
        submission_text=reading.text,
        language="ukr",
        path_key=PathKey(AssignmentType.PROJECT, SubmissionState.FIRST),
        stage_name=stage_name,
        stage=config.stages[stage_name],
        session_factory=AsyncMock(),
        door=reading,
        work=reading.files,
    )


async def _evaluate(
    session: AsyncSession,
    submission: HomeworkSubmission,
    reading: DoorReading,
    *answers: str,
) -> tuple[Any, dict[str, SubmissionCriterionVerdict]]:
    qwen = _provider(*answers)
    context = _context(
        session, _router(qwen), submission, reading, PROJECT_CRITERIA_EVALUATION
    )

    outcome = await get_stage_executor(PROJECT_CRITERIA_EVALUATION)(context)

    assert outcome == StageOutcome.ok()
    rows = await SubmissionVerdictRepository(session).list_for_submission(
        tenant_id=submission.tenant_id, submission_id=submission.id
    )
    return qwen, {row.item_id: row for row in rows}


@pytest.mark.usefixtures("authors_list")
class TestTheEvaluationOfAProject:
    """Lock v: chain K3's rule, through the project's own stage and prompt."""

    async def test_the_request_is_the_projects_prompt_over_its_context(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        reading: DoorReading,
    ) -> None:
        qwen, _ = await _evaluate(
            db_session,
            submission,
            reading,
            _verdicts(
                _met("c1", QUOTE_OF_NEW),
                _met("c2.p1", QUOTE_OF_CHANGED),
                _met("c2.p2", QUOTE_OF_CHANGED),
            ),
        )

        [request] = _sent(qwen)
        assert request.system_prompt is not None
        assert "`NEIGHBOR` — a file of the base" in request.system_prompt
        assert "a quote of the author's code does not count" in " ".join(
            request.system_prompt.split()
        )
        work = request.prompt.split("\n<student_submission>\n", 1)[1]
        assert work.startswith(reading.text + "\n</student_submission>\n")

    async def test_a_quote_of_a_new_file_stands_without_a_repeat(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        reading: DoorReading,
    ) -> None:
        qwen, rows = await _evaluate(
            db_session,
            submission,
            reading,
            _verdicts(
                _met("c1", QUOTE_OF_NEW),
                _met("c2.p1", QUOTE_OF_CHANGED),
                _met("c2.p2", QUOTE_OF_CHANGED),
            ),
        )

        assert qwen.complete.await_count == 1
        c1 = rows["c1"]
        assert c1.verdict == VerdictValue.MET
        assert (c1.quote_file, c1.quote_line_start) == ("stats.py", 4)
        assert (c1.retried, c1.quote_from_base) == (False, False)
        assert rows["c2.p2"].quote_file == "app.py"
        assert not any(row.quote_from_base for row in rows.values())

    @pytest.mark.parametrize(
        "authors", [QUOTE_OF_NEIGHBOR, QUOTE_OF_AUTHORS_LINE], ids=["neighbor", "kept"]
    )
    async def test_a_quote_of_the_authors_code_is_asked_again_then_flagged(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        reading: DoorReading,
        authors: str,
    ) -> None:
        qwen, rows = await _evaluate(
            db_session,
            submission,
            reading,
            _verdicts(
                _met("c1", QUOTE_OF_NEW),
                _met("c2.p1", authors),
                _met("c2.p2", QUOTE_OF_CHANGED),
            ),
            _verdicts(_met("c2.p1", authors)),
        )

        first, again = _sent(qwen)
        assert '["c2.p1"]' in again.prompt
        problems = again.prompt.split("\n<previous_problems>\n", 1)[1]
        (note,) = json.loads(problems.split("\n</previous_problems>\n", 1)[0])
        assert note["problem"] == _FROM_BASE
        assert again.system_prompt is not None
        assert "second request" in again.system_prompt
        p1 = rows["c2.p1"]
        assert (p1.verdict, p1.model_verdict) == (
            VerdictValue.NOT_MET,
            VerdictValue.MET,
        )
        assert (p1.quote_from_base, p1.quote_not_found, p1.retried) == (
            True,
            False,
            True,
        )
        assert (rows["c2"].verdict, rows["c2"].quote_from_base) == (
            VerdictValue.NOT_MET,
            True,
        )
        assert rows["c1"].verdict == VerdictValue.MET
        # Both requests are the project's prompt, on the rung that answered.
        assert first.system_prompt is not None
        assert "`NEIGHBOR`" in first.system_prompt
        assert "`NEIGHBOR`" in again.system_prompt

    async def test_a_repeat_on_the_students_own_line_stands(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        reading: DoorReading,
    ) -> None:
        _, rows = await _evaluate(
            db_session,
            submission,
            reading,
            _verdicts(
                _met("c1", QUOTE_OF_NEW),
                _met("c2.p1", QUOTE_OF_NEIGHBOR),
                _met("c2.p2", QUOTE_OF_CHANGED),
            ),
            _verdicts(_met("c2.p1", QUOTE_OF_CHANGED)),
        )

        p1 = rows["c2.p1"]
        assert (p1.verdict, p1.quote_file, p1.quote_line_start) == (
            VerdictValue.MET,
            "app.py",
            8,
        )
        assert (p1.retried, p1.quote_from_base) == (True, False)


@pytest.mark.usefixtures("authors_list")
class TestTheExplanationOfAProject:
    """Lock h: the flag reaches the explanation, and a plain answer is kept."""

    async def test_the_authors_code_is_said_in_plain_words(
        self,
        db_session: AsyncSession,
        submission: HomeworkSubmission,
        reading: DoorReading,
    ) -> None:
        await _evaluate(
            db_session,
            submission,
            reading,
            _verdicts(
                _met("c1", QUOTE_OF_NEW),
                _met("c2.p1", QUOTE_OF_NEIGHBOR),
                _met("c2.p2", QUOTE_OF_CHANGED),
            ),
            _verdicts(_met("c2.p1", QUOTE_OF_NEIGHBOR)),
        )
        qwen = _provider(_HUMAN_EXPLANATION)
        context = _context(
            db_session, _router(qwen), submission, reading, PROJECT_REVIEW_EXPLANATION
        )

        outcome = await get_stage_executor(PROJECT_REVIEW_EXPLANATION)(context)

        assert outcome == StageOutcome.ok()
        [request] = _sent(qwen)
        assert request.system_prompt is not None
        assert "present only in the code of the starting project" in " ".join(
            request.system_prompt.split()
        )
        verdicts = request.prompt.split("\n<verdicts>\n", 1)[1].split(
            "\n</verdicts>\n", 1
        )[0]
        [c1, c2] = json.loads(verdicts)
        p1, p2 = c2["points"]
        assert p1["quote_from_base"] is True
        # The author's line the model gave is never shown to it again.
        assert "quote" not in p1
        assert QUOTE_OF_NEIGHBOR not in verdicts
        assert "quote_from_base" not in p2
        assert "quote_from_base" not in c1

        kept = await SubmissionVerdictRepository(db_session).get_explanation(
            tenant_id=submission.tenant_id, submission_id=submission.id
        )
        assert kept is not None
        assert kept.body == json.loads(_HUMAN_EXPLANATION)
        [remark] = kept.body["remarks"]
        assert remark["id"] == "c2"
        assert "стартового проєкту" in remark["what"]
        texts = " ".join(
            [kept.body["why"], remark["what"], remark["why"], remark["todo"]]
        ).lower()
        assert not [word for word in _MECHANICS if word in texts]
