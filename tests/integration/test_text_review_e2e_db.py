"""A text task reviewed end to end on the new path (mentor-rebuild task 09b, K6).

``vision-rules#25``: the criteria of ``TASK.md`` that read end to end are
proved by one walk, not by the sum of the stages' own suites. Here the type is
``task``, switched onto the new path inside the test only, and everything
below the model layer is the real thing — the ARQ task, the seam, the body,
the four shipped stages with their descriptions from
``config/submission_paths.yaml``, the verdict repository, the explanation's
check, the builder, the assembler, the webhook payload.

The doubles, and why each:

* the ROUTER — the alternative is a paid call. It answers each stage with a
  recorded answer, runs the stage's own check on it, and leaves the register
  row a real call would, so each stage's price is summed from real rows;
* the CRITERIA SOURCE — the production service composes a list on a miss
  (another paid call) and needs an ingested task. The list itself is seeded
  for real: the explanation stage reads it back by the address the verdict
  rows carry;
* the FUNDS PORT and the webhook's transport — nothing to spend, nowhere to
  send.

The walks: a first submission (rows, score, pass, structure, markdown,
webhook — locks 1 and 2 through the body); the same machine input once more
(lock 9); a second submission with the same quote (the safeguard, lock 5); a
submission with no list (held, then exactly one continuation and one review
once a list appears — lock 6); the language of a review the doors gave none;
the builder's refusal when either part is missing; and the shipped file, which
leaves a text task with today's Mentor (lock 8).

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.routes._portal_shared import (
    curated_presentation,
    curated_verdict,
)
from course_supporter.api.schemas import PortalVerdict
from course_supporter.api.tasks import arq_process_homework
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.funds_port import (
    FUNDS_PORT_ACTION,
    FundsAnswer,
    SubmissionContext,
    SubmissionOutcome,
    VersionWorkContext,
)
from course_supporter.homework.criteria_form import Criterion, criteria_to_document
from course_supporter.homework.criteria_list_service import (
    CriteriaInForce,
    CriteriaUnavailable,
    UnavailableReason,
)
from course_supporter.homework.path_config import (
    PathConfig,
    ServedBy,
    load_path_config,
)
from course_supporter.homework.path_continuation import resume_awaiting_criteria
from course_supporter.homework.path_runner import run_new_path_if_switched
from course_supporter.homework.path_stages import (
    ATTEMPT_CLASSIFIER,
    CRITERIA_EVALUATION,
    REVIEW_EXPLANATION,
    SAFETY,
)
from course_supporter.homework.result_builders import (
    BuildContext,
    ResultNotBuiltError,
)
from course_supporter.homework.review_assembler import assemble_review
from course_supporter.homework.text_result import (
    REVIEW_PARTS_MISSING,
    TextResultBuilder,
)
from course_supporter.llm.ladder_config import StageConfig
from course_supporter.llm.stage_router import StageResult
from course_supporter.models.review_structure import Remark, ReviewStructureV1
from course_supporter.models.webhook import WebhookReviewedPayload
from course_supporter.service_logging import _persist
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
    SubmissionCriterionVerdict,
    SubmissionExplanation,
    Tenant,
)
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)
from course_supporter.storage.task_criteria_list_repository import (
    TaskCriteriaListRepository,
)

pytestmark = pytest.mark.requires_db

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WEBHOOK_URL = "https://example.com/hook"
_WORK = "def solve(n):\n    return n * 2\n"


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


# Weights 3 / 2 / 1; c2 is checked by two mandatory points.
_CRITERIA = (
    _criterion("c1", "must"),
    _criterion("c2", "should", 2),
    _criterion("c3", "may"),
)


def _verdict(
    item: str, verdict: str, *, quote: str | None = None, missing: str | None = None
) -> dict[str, Any]:
    return {"id": item, "verdict": verdict, "quote": quote, "missing": missing}


def _evaluation(*verdicts: dict[str, Any]) -> str:
    return json.dumps({"verdicts": list(verdicts)}, ensure_ascii=False)


# c1 met; c2's first point met, its second not; c3 not met.
_FIRST_EVALUATION = _evaluation(
    _verdict("c1", "met", quote="def solve(n):"),
    _verdict("c2.p1", "met", quote="return n * 2"),
    _verdict("c2.p2", "not_met", missing="Немає перевірки від'ємного n."),
    _verdict("c3", "not_met", missing="Немає докстрінга."),
)

# The same work again, and the model now calls c1 not met: its earlier quote
# is still in the work, so the safeguard keeps the earlier "met".
_SECOND_EVALUATION = _evaluation(
    _verdict("c1", "not_met", missing="Функцію не знайдено."),
    _verdict("c2.p1", "met", quote="return n * 2"),
    _verdict("c2.p2", "not_met", missing="Немає перевірки від'ємного n."),
    _verdict("c3", "not_met", missing="Немає докстрінга."),
)

_WHY = "Робота зарахована: функція solve є й подвоює число."
_VOICE = "Назва функції говорить сама за себе."


def _remark(cid: str) -> dict[str, str]:
    return {
        "id": cid,
        "what": f"Зараз у роботі {cid} не виконано.",
        "why": f"Через це {cid} не працює.",
        "todo": f"Зробіть {cid}.",
    }


# The remarks in the order the model gave them — the lightest first — which
# the builder puts heaviest first.
_EXPLANATION = json.dumps(
    {
        "passed": True,
        "why": _WHY,
        "remarks": [_remark("c3"), _remark("c2")],
        "mentor_voice": _VOICE,
    },
    ensure_ascii=False,
)

_SAFETY = (
    '{"source": "stage2", "is_safe": true, "violations": [], '
    '"confidence": 0.99, "reasoning": "nothing of the sort"}'
)
_ATTEMPT = '{"verdict": "match", "confidence": 0.9, "reason": "an honest attempt"}'

_PRICE = {
    SAFETY: 0.004,
    ATTEMPT_CLASSIFIER: 0.006,
    CRITERIA_EVALUATION: 0.02,
    REVIEW_EXPLANATION: 0.008,
}


class _RouterDouble:
    """Answers each stage with a recorded answer and leaves its register row.

    The stage's own check runs on every answer, as the real router runs it;
    the answer is credited to the stage's first rung, as if it answered.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.calls: list[str] = []
        self.content: dict[str, str] = {
            SAFETY: _SAFETY,
            ATTEMPT_CLASSIFIER: _ATTEMPT,
            CRITERIA_EVALUATION: _FIRST_EVALUATION,
            REVIEW_EXPLANATION: _EXPLANATION,
        }
        self._session_factory = session_factory

    def count(self, stage_name: str) -> int:
        return self.calls.count(stage_name)

    async def execute_stage(
        self,
        stage: StageConfig,
        stage_name: str,
        /,
        *,
        response_validator: Callable[[str], None] | None = None,
        contents: list[bytes] | None = None,
        expects_json: bool = False,
        response_schema: dict[str, Any] | None = None,
        stop_on_output_ceiling: bool = False,
        money_ceiling_usd: float | None = None,
        **render_context: Any,
    ) -> StageResult:
        self.calls.append(stage_name)
        rung = stage.ladder[0]
        await _persist(
            self._session_factory,
            action=stage_name,
            strategy="default",
            provider=rung.provider,
            model_id=rung.model,
            unit_type="tokens",
            cost_usd=_PRICE[stage_name],
        )
        content = self.content[stage_name]
        if response_validator is not None:
            response_validator(content)
        return StageResult(
            content=content,
            provider_used=rung.provider,
            model_used=rung.model,
            attempt_count=1,
        )


class _ListSource:
    """The criteria source: the seeded list, or none until it "appears"."""

    def __init__(self, in_force: CriteriaInForce | None) -> None:
        self._in_force = in_force
        self.answer: CriteriaInForce | CriteriaUnavailable = (
            in_force
            if in_force is not None
            else CriteriaUnavailable(UnavailableReason.COMPOSITION_FAILED)
        )

    def appears(self, in_force: CriteriaInForce) -> None:
        self.answer = in_force

    async def get_or_compose(
        self, authored_document_id: uuid.UUID
    ) -> CriteriaInForce | CriteriaUnavailable:
        return self.answer


class _PortDouble:
    """Always allows; records what it was told."""

    def __init__(self) -> None:
        self.reserved: list[float] = []
        self.accounted: list[float] = []
        self.released: list[SubmissionOutcome] = []

    async def check_and_reserve(
        self, context: SubmissionContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        self.reserved.append(ceiling_estimate_usd)
        return FundsAnswer.allowed()

    async def account_stage_cost(
        self, context: SubmissionContext, stage_cost_usd: float
    ) -> None:
        self.accounted.append(stage_cost_usd)

    async def release_remainder(
        self, context: SubmissionContext, outcome: SubmissionOutcome
    ) -> None:
        self.released.append(outcome)

    async def check_and_reserve_for_version(
        self, context: VersionWorkContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        raise AssertionError("the submission path must not use the version operations")

    async def account_version_work_cost(
        self, context: VersionWorkContext, actual_usd: float
    ) -> None:
        raise AssertionError("the submission path must not use the version operations")


def _switched() -> PathConfig:
    """The shipped file, with ``task`` moved onto the new path — here only."""
    shipped = load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")
    raw = shipped.model_dump(mode="json")
    raw["task_types"]["task"]["served_by"] = ServedBy.NEW_PATH.value
    return PathConfig.model_validate(raw)


# ── The world: a course, a task, its list, a student ────────────────────────


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, Any]]:
    """A ``task`` with a ready model list, and one student; no submission yet."""
    async with session_factory() as session:
        tenant = Tenant(name=f"text-e2e-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = CourseNode(
            tenant_id=tenant.id, title="Course", order=0, default_language="ukr"
        )
        session.add(node)
        await session.flush()
        doc = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url=f"https://example.com/{uuid.uuid4().hex}",
            task_type="task",
            language="ukr",
        )
        session.add(doc)
        await session.flush()
        lists = TaskCriteriaListRepository(session)
        claimed = await lists.claim(
            authored_document_id=doc.id,
            source_content_hash="h" * 64,
            source_task_type="task",
            form_version=2,
        )
        assert claimed is not None
        assert await lists.mark_ready(
            claimed.id,
            claimed_at=claimed.claimed_at,
            criteria=criteria_to_document(_CRITERIA),
            contradictions=[],
            concepts_in_input=False,
            dropped_concept_count=0,
            input_fingerprint="f" * 64,
        )
        student = Student(
            tenant_id=tenant.id, external_id=f"stu-{uuid.uuid4().hex[:6]}"
        )
        session.add(student)
        await session.commit()
        ids: dict[str, Any] = {
            "tenant_id": tenant.id,
            "node_id": node.id,
            "doc_id": doc.id,
            "student_id": student.id,
            "in_force": CriteriaInForce(
                criteria=_CRITERIA, layer=CriteriaLayer.MODEL, source_id=claimed.id
            ),
            "submissions": [],
        }

    yield ids

    async with session_factory() as session:
        for submission_id in ids["submissions"]:
            job_ids = list(
                (
                    await session.execute(
                        select(Job.id).where(Job.subject_id == submission_id)
                    )
                ).scalars()
            )
            await session.execute(
                delete(ExternalServiceCall).where(
                    ExternalServiceCall.job_id.in_(job_ids)
                )
            )
            await session.execute(
                delete(HomeworkSubmission).where(HomeworkSubmission.id == submission_id)
            )
            await session.execute(delete(Job).where(Job.subject_id == submission_id))
        await session.commit()


async def _submit(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    *,
    student_id: uuid.UUID | None = None,
    response_language: str | None = "uk",
) -> tuple[uuid.UUID, uuid.UUID]:
    """A submission of ``_WORK`` with its first job queued: (submission, job)."""
    async with session_factory() as session:
        submission = HomeworkSubmission(
            tenant_id=world["tenant_id"],
            student_id=student_id or world["student_id"],
            course_node_id=world["node_id"],
            node_id=world["node_id"],
            authored_document_id=world["doc_id"],
            file_url="s3://bucket/homework/answers.py",
            file_type="text/plain",
            original_filename="answers.py",
            webhook_url=_WEBHOOK_URL,
            response_language=response_language,
            status="received",
        )
        session.add(submission)
        await session.flush()
        job = Job(
            tenant_id=world["tenant_id"],
            course_node_id=world["node_id"],
            job_type="homework_processing",
            subject_type="homework_submission",
            subject_id=submission.id,
            input_params={"submission_id": str(submission.id)},
            status="queued",
        )
        session.add(job)
        await session.commit()
        world["submissions"].append(submission.id)
        return submission.id, job.id


async def _another_student(
    session_factory: async_sessionmaker[AsyncSession], world: dict[str, Any]
) -> uuid.UUID:
    async with session_factory() as session:
        student = Student(
            tenant_id=world["tenant_id"], external_id=f"stu-{uuid.uuid4().hex[:6]}"
        )
        session.add(student)
        await session.commit()
        return student.id


def _ctx(
    session_factory: async_sessionmaker[AsyncSession],
    router: _RouterDouble,
    tmp_path: Path,
    *,
    arq: Any = None,
) -> dict[str, Any]:
    answers = tmp_path / "answers.py"

    def _download(_key: str) -> Path:
        # The body deletes its download on the way out; each run gets its own.
        answers.write_text(_WORK, encoding="utf-8")
        return answers

    s3 = MagicMock()
    s3.extract_key = MagicMock(return_value="homework/answers.py")
    s3.download_file = AsyncMock(side_effect=_download)
    ctx: dict[str, Any] = {
        "session_factory": session_factory,
        "stage_router": router,
        "s3_client": s3,
        "job_try": 1,
    }
    if arq is not None:
        ctx["redis"] = arq
    return ctx


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    router: _RouterDouble,
    source: _ListSource,
    webhook: AsyncMock,
    tmp_path: Path,
    *,
    submission_id: uuid.UUID,
    job_id: uuid.UUID,
    port: _PortDouble | None = None,
    arq: Any = None,
    config: PathConfig | None = None,
) -> None:
    """The ARQ task through the seam; ``task`` switched in the test only."""
    config = config or _switched()
    with (
        patch(
            "course_supporter.homework.path_runner.get_path_config",
            return_value=config,
        ),
        patch(
            "course_supporter.homework.path_continuation.get_path_config",
            return_value=config,
        ),
        patch(
            "course_supporter.homework.path_runner.AlwaysEnoughFundsPort",
            return_value=port or _PortDouble(),
        ),
        patch(
            "course_supporter.homework.criteria_evaluation.build_criteria_list_service",
            return_value=source,
        ),
        patch("course_supporter.homework.webhook.deliver_webhook", new=webhook),
    ):
        await arq_process_homework(
            _ctx(session_factory, router, tmp_path, arq=arq),
            str(job_id),
            str(submission_id),
        )


def _webhook() -> AsyncMock:
    return AsyncMock(return_value=True)


def _reviewed(webhook: AsyncMock) -> list[WebhookReviewedPayload]:
    payloads = [call.kwargs["payload"] for call in webhook.await_args_list]
    return [p for p in payloads if isinstance(p, WebhookReviewedPayload)]


async def _submission(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> HomeworkSubmission:
    async with session_factory() as session:
        submission = await session.get(HomeworkSubmission, submission_id)
    assert submission is not None
    return submission


_ROW_FIELDS = (
    "criteria_layer",
    "criteria_source_id",
    "item_id",
    "item_kind",
    "weight",
    "verdict",
    "model_verdict",
    "quote",
    "quote_file",
    "quote_line_start",
    "quote_line_end",
    "missing",
    "retried",
    "quote_not_found",
    "safeguard_fired",
)


async def _rows(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> dict[str, dict[str, Any]]:
    """The submission's verdict rows by item, without ids and times."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(SubmissionCriterionVerdict).where(
                    SubmissionCriterionVerdict.submission_id == submission_id
                )
            )
        ).scalars()
        return {
            row.item_id: {
                **{field: getattr(row, field) for field in _ROW_FIELDS},
                "safeguard_submission_id": row.safeguard_submission_id,
            }
            for row in rows
        }


async def _priced(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> list[str]:
    """The stages the register priced for the submission's jobs, in order."""
    async with session_factory() as session:
        job_ids = select(Job.id).where(Job.subject_id == submission_id)
        rows = (
            await session.execute(
                select(ExternalServiceCall)
                .where(
                    ExternalServiceCall.job_id.in_(job_ids),
                    ExternalServiceCall.action != FUNDS_PORT_ACTION,
                )
                .order_by(ExternalServiceCall.created_at.asc())
            )
        ).scalars()
        return [row.action for row in rows]


# ── The walks ────────────────────────────────────────────────────────────────


class TestAFirstSubmissionIsReviewedFromItsVerdicts:
    """Locks 1 and 2 through the body: from the work to the delivered review."""

    async def test_rows_score_pass_structure_markdown_and_webhook(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        router = _RouterDouble(session_factory)
        webhook = _webhook()
        port = _PortDouble()
        submission_id, job_id = await _submit(session_factory, world)

        await _run(
            session_factory,
            router,
            _ListSource(world["in_force"]),
            webhook,
            tmp_path,
            submission_id=submission_id,
            job_id=job_id,
            port=port,
        )

        # ── Every stage of the first-submission path, once, priced ──
        assert router.calls == [
            SAFETY,
            ATTEMPT_CLASSIFIER,
            CRITERIA_EVALUATION,
            REVIEW_EXPLANATION,
        ]
        assert await _priced(session_factory, submission_id) == router.calls
        assert port.accounted == [pytest.approx(_PRICE[name]) for name in router.calls]
        assert port.released == [SubmissionOutcome.COMPLETED]

        # ── The rows: one per criterion and per point, against the list ──
        rows = await _rows(session_factory, submission_id)
        assert {item: row["verdict"] for item, row in rows.items()} == {
            "c1": "met",
            "c2": "not_met",
            "c2.p1": "met",
            "c2.p2": "not_met",
            "c3": "not_met",
        }
        assert {row["criteria_source_id"] for row in rows.values()} == {
            world["in_force"].source_id
        }
        assert (rows["c1"]["quote"], rows["c1"]["quote_file"]) == (
            "def solve(n):",
            "answers.py",
        )
        assert (rows["c1"]["quote_line_start"], rows["c1"]["quote_line_end"]) == (1, 1)
        assert rows["c2"]["model_verdict"] is None  # derived from its points
        assert not any(row["safeguard_fired"] for row in rows.values())

        # ── The score by hand: c1 (3) of c1 + c2 + c3 (3 + 2 + 1) = 50 % ──
        submission = await _submission(session_factory, submission_id)
        assert submission.status == "delivered"
        assert submission.score == 50

        # ── The structure: the code's pass, the model's words, heaviest first ──
        structure = ReviewStructureV1.model_validate(submission.review_result)
        assert structure.language == "ukr"
        assert structure.verdict is not None
        assert structure.verdict.passed is True
        assert structure.verdict.why == _WHY
        assert structure.new_remarks == [
            Remark(
                what=_remark(cid)["what"],
                why=_remark(cid)["why"],
                todo=_remark(cid)["todo"],
            )
            for cid in ("c2", "c3")
        ]
        assert structure.mentor_voice == _VOICE
        assert submission.review_markdown == assemble_review(structure)

        # ── What leaves the service: the webhook and the portal agree ──
        [payload] = _reviewed(webhook)
        assert payload.structure == structure
        assert payload.review.score == 50
        assert payload.review.passed is True
        assert payload.review.correctness == "partially_correct"
        assert payload.review.review_text == submission.review_markdown
        assert curated_verdict(
            submission.review_result, score=submission.score
        ) == PortalVerdict(passed=True, correctness="partially_correct")


class TestTheSameMachineInputGivesTheSameReview:
    """Lock 9 through the body, on recorded answers of the model."""

    async def test_the_same_score_rows_and_structure(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """Two students, the same work, the same answers: nothing differs.

        Two students, so the second is a first submission too and no
        safeguard stands between the two runs.
        """
        router = _RouterDouble(session_factory)
        source = _ListSource(world["in_force"])
        reviewed = []
        for student_id in (
            world["student_id"],
            await _another_student(session_factory, world),
        ):
            submission_id, job_id = await _submit(
                session_factory, world, student_id=student_id
            )
            await _run(
                session_factory,
                router,
                source,
                _webhook(),
                tmp_path,
                submission_id=submission_id,
                job_id=job_id,
            )
            reviewed.append(
                (
                    await _submission(session_factory, submission_id),
                    await _rows(session_factory, submission_id),
                )
            )

        (first, first_rows), (second, second_rows) = reviewed
        assert first_rows
        assert first_rows == second_rows
        assert (first.score, first.review_result, first.review_markdown) == (
            second.score,
            second.review_result,
            second.review_markdown,
        )
        assert first.score == 50


class TestASecondSubmissionKeepsWhatTheFirstEarned:
    """Lock 5 through the body: the same quote, the same list — "met" stands."""

    async def test_the_safeguard_keeps_the_earlier_met(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        router = _RouterDouble(session_factory)
        source = _ListSource(world["in_force"])
        first_id, first_job = await _submit(session_factory, world)
        await _run(
            session_factory,
            router,
            source,
            _webhook(),
            tmp_path,
            submission_id=first_id,
            job_id=first_job,
        )
        assert (await _submission(session_factory, first_id)).status == "delivered"

        router.calls.clear()
        router.content[CRITERIA_EVALUATION] = _SECOND_EVALUATION
        webhook = _webhook()
        second_id, second_job = await _submit(session_factory, world)
        await _run(
            session_factory,
            router,
            source,
            webhook,
            tmp_path,
            submission_id=second_id,
            job_id=second_job,
        )

        # A repeat: no classifier, and the review written again.
        assert router.calls == [SAFETY, CRITERIA_EVALUATION, REVIEW_EXPLANATION]
        rows = await _rows(session_factory, second_id)
        assert rows["c1"]["verdict"] == "met"
        assert rows["c1"]["model_verdict"] == "not_met"
        assert rows["c1"]["safeguard_fired"] is True
        assert rows["c1"]["safeguard_submission_id"] == first_id
        assert rows["c1"]["quote"] == "def solve(n):"

        second = await _submission(session_factory, second_id)
        assert second.status == "delivered"
        assert second.score == 50
        structure = ReviewStructureV1.model_validate(second.review_result)
        assert structure.verdict is not None
        assert structure.verdict.passed is True
        [payload] = _reviewed(webhook)
        assert payload.review.score == 50


class TestASubmissionWithoutAListWaitsForOne:
    """Lock 6 through the body: held without a paid evaluation, then one review."""

    async def test_held_then_one_continuation_and_one_review(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        router = _RouterDouble(session_factory)
        source = _ListSource(None)
        webhook = _webhook()
        arq = MagicMock()
        arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq-k6"))
        submission_id, job_id = await _submit(session_factory, world)

        await _run(
            session_factory,
            router,
            source,
            webhook,
            tmp_path,
            submission_id=submission_id,
            job_id=job_id,
        )

        held = await _submission(session_factory, submission_id)
        assert held.status == "awaiting_criteria"
        assert curated_presentation(held).reason_code == "awaiting_criteria"
        assert router.calls == [SAFETY, ATTEMPT_CLASSIFIER]
        assert await _rows(session_factory, submission_id) == {}
        assert webhook.await_args_list == []

        source.appears(world["in_force"])

        async def entry() -> int:
            async with session_factory() as session:
                return await resume_awaiting_criteria(
                    session,
                    arq,
                    tenant_id=world["tenant_id"],
                    authored_document_id=world["doc_id"],
                    config=_switched(),
                )

        # Two entries in a row — an author's edit, say, then a worker start.
        assert await entry() == 1
        assert await entry() == 0
        async with session_factory() as session:
            jobs = list(
                (
                    await session.execute(
                        select(Job)
                        .where(Job.subject_id == submission_id)
                        .order_by(Job.queued_at.asc(), Job.id.asc())
                    )
                ).scalars()
            )
        [_, continuation] = jobs

        await _run(
            session_factory,
            router,
            source,
            webhook,
            tmp_path,
            submission_id=submission_id,
            job_id=continuation.id,
        )
        assert await entry() == 0

        # The stages behind the hold are not run again; the two after it once.
        assert router.calls == [
            SAFETY,
            ATTEMPT_CLASSIFIER,
            CRITERIA_EVALUATION,
            REVIEW_EXPLANATION,
        ]
        reviewed = await _submission(session_factory, submission_id)
        assert reviewed.status == "delivered"
        assert reviewed.score == 50
        [payload] = _reviewed(webhook)
        assert payload.review.score == 50
        async with session_factory() as session:
            explanations = (
                await session.execute(
                    select(SubmissionExplanation).where(
                        SubmissionExplanation.submission_id == submission_id
                    )
                )
            ).all()
        assert len(explanations) == 1


class TestTheReviewIsInTheExplanationsLanguage:
    """The builder's language is the explanation's, not the doors' (9.9)."""

    async def test_with_no_language_at_the_door_the_course_roots(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """No language on the submission, the student or the task: the doors
        resolve none, and the explanation is written in the course root's."""
        async with session_factory() as session:
            await session.execute(
                update(AuthoredDocument)
                .where(AuthoredDocument.id == world["doc_id"])
                .values(language=None)
            )
            await session.commit()
        submission_id, job_id = await _submit(
            session_factory, world, response_language=None
        )

        await _run(
            session_factory,
            _RouterDouble(session_factory),
            _ListSource(world["in_force"]),
            _webhook(),
            tmp_path,
            submission_id=submission_id,
            job_id=job_id,
        )

        reviewed = await _submission(session_factory, submission_id)
        assert reviewed.status == "delivered", reviewed.error_message
        structure = ReviewStructureV1.model_validate(reviewed.review_result)
        assert structure.language == "ukr"


def _path_of(*stages: str) -> PathConfig:
    """The switched file, with every path of ``task`` cut to ``stages``."""
    raw = _switched().model_dump(mode="json")
    raw["task_types"]["task"]["paths"] = {
        state: list(stages) for state in raw["task_types"]["task"]["paths"]
    }
    return PathConfig.model_validate(raw)


class TestAReviewWithoutItsPartsFails:
    """The builder's refusal: :data:`REVIEW_PARTS_MISSING`, for either part."""

    @pytest.mark.parametrize(
        "stages",
        [
            (SAFETY,),
            (SAFETY, ATTEMPT_CLASSIFIER, CRITERIA_EVALUATION),
        ],
        ids=["no-verdicts", "verdicts-without-explanation"],
    )
    async def test_failed_through_the_body_with_the_builders_code(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        tmp_path: Path,
        stages: tuple[str, ...],
    ) -> None:
        """A path that does not list the stages the builder reads."""
        webhook = _webhook()
        submission_id, job_id = await _submit(session_factory, world)

        await _run(
            session_factory,
            _RouterDouble(session_factory),
            _ListSource(world["in_force"]),
            webhook,
            tmp_path,
            submission_id=submission_id,
            job_id=job_id,
            config=_path_of(*stages),
        )

        failed = await _submission(session_factory, submission_id)
        assert failed.status == "failed"
        assert failed.error_message == REVIEW_PARTS_MISSING
        assert failed.review_result is None
        assert _reviewed(webhook) == []
        rows = await _rows(session_factory, submission_id)
        assert bool(rows) is (CRITERIA_EVALUATION in stages)

    async def test_an_explanation_without_verdicts_is_refused(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
    ) -> None:
        """Not a state a path leaves — the explanation stage needs the rows —
        but one the data can hold: the refusal is the builder's own."""
        submission_id, _ = await _submit(session_factory, world)
        async with session_factory() as session:
            submission = await session.get(HomeworkSubmission, submission_id)
            assert submission is not None
            await SubmissionVerdictRepository(session).store_explanation(
                tenant_id=world["tenant_id"],
                submission_id=submission_id,
                language="ukr",
                body=json.loads(_EXPLANATION),
            )
            await session.commit()

            with pytest.raises(ResultNotBuiltError) as refused:
                await TextResultBuilder().build(
                    BuildContext(
                        session=session,
                        session_factory=session_factory,
                        submission=submission,
                        submission_text=_WORK,
                        review_language="ukr",
                        redis=None,
                    )
                )

        assert refused.value.code == REVIEW_PARTS_MISSING


class TestTheShippedFileLeavesTextTasksWithTodaysMentor:
    """Lock 8: the paths are described, the switch is not thrown."""

    async def test_the_new_path_does_not_take_a_task(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        router = _RouterDouble(session_factory)
        submission_id, job_id = await _submit(session_factory, world)

        handled = await run_new_path_if_switched(
            _ctx(session_factory, router, tmp_path), job_id, submission_id
        )

        assert handled is False
        assert router.calls == []
        submission = await _submission(session_factory, submission_id)
        assert submission.status == "received"
        assert await _rows(session_factory, submission_id) == {}
