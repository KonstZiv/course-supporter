"""The same work sent again, answered with its earlier review (task 13, K3).

The locks of the commit, one class each, on a real database and the real body:

* а. end to end: a ``task`` submission gets its review; the same work again, in
  CP1251 with CRLF, gets that review again — no model asked (the router and
  every provider untouched), no new row in the call register, the review the
  first one with ``unchanged_from_attempt = 1``, the phrase ``review.unchanged``
  opening its markdown, ``dedup_of = 1`` in its receipt, and delivered;
* б. the funds port is not asked on a repeat, and is asked as before on an
  ordinary submission;
* в. a new version of the task, or a new version of its criteria list (an
  author's edit), is an ordinary review — and each key of the match, on its
  own, at the repository;
* г. a submission refused for safety, judged off-task or failed, with the same
  fingerprint, is no source of a copy;
* ґ. a test is scored anew every time; today's Mentor repeats nothing, even
  when the new path reviewed the same work before.

And the road of a failure: a lookup whose query breaks in the database leaves
the body's session usable (a savepoint), and the work is reviewed as usual.

The evaluator's version in the key (task 13), its locks б, в, г — one class
each, named ``TestEvaluatorLock…``; its lock а is a unit lock
(``tests/unit/test_homework/test_evaluator_version.py``), its lock ґ (the road
and test mode in the receipt) is in ``test_attempt_ledger_db``:

* б. a ``task`` submission gets its review and its receipt names the
  evaluator; the same work again is a copy — no model asked — whose receipt
  names the same evaluator and ``dedup_of``;
* в. after the evaluator changed — the verdict rules raised, or the source's
  receipt naming another evaluator — the same work is reviewed anew;
* г. an attempt whose receipt names no evaluator (every one before this
  commit) is no source of a copy.

Lock д (a review stored without the new field) is a unit lock:
``tests/unit/test_review_unchanged_note.py``.

The doubles are the ones the walks of the new path already use
(``test_text_review_e2e_db``, ``test_attempt_ledger_db``, ``test_one_door_db``):
no model is called. Run with ``--run-db --run-redis``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from course_supporter.api.tasks import arq_process_homework
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.funds_port import SubmissionOutcome
from course_supporter.homework import criteria_verdicts
from course_supporter.homework.attempt_dedup import DEDUP_RECEIPT_KEY
from course_supporter.homework.criteria_form import Criterion, criteria_to_document
from course_supporter.homework.criteria_list_service import CriteriaInForce
from course_supporter.homework.evaluator_version import (
    EVALUATOR_VERSION_RECEIPT_KEY,
    evaluator_version,
)
from course_supporter.homework.path_config import PathConfig, SubmissionState
from course_supporter.homework.path_stages import (
    ATTEMPT_CLASSIFIER,
    CRITERIA_EVALUATION,
    REVIEW_EXPLANATION,
    SAFETY,
)
from course_supporter.homework.review_assembler import assemble_review
from course_supporter.homework.test_doors import stored_answers
from course_supporter.llm.providers import PROVIDER_REGISTRY
from course_supporter.models import review_structure
from course_supporter.models.review_schema import REVIEW_SCHEMA_VERSION
from course_supporter.models.review_structure import ReviewStructureV1, Verdict
from course_supporter.models.source import AssignmentType
from course_supporter.phrasebook import phrases_for
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    ActionCostEstimate,
    AuthoredDocument,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    SubmissionCriterionVerdict,
)
from course_supporter.storage.task_criteria_list_repository import (
    TaskCriteriaListRepository,
)
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)
from tests._helpers.path_config import shipped_with_todays_mentor_for
from tests.integration.test_attempt_ledger_db import (
    _VERSION_A,
    _VERSION_B,
    _add,
    _drop_world,
    _make_world,
    _RefusingPort,
    _row,
    _set_status,
    _through_the_doors,
)
from tests.integration.test_one_door_db import (
    _RecordingReview,
    _RecordingSanity,
)
from tests.integration.test_one_door_db import (
    _RouterDouble as _TodaysRouter,
)
from tests.integration.test_text_review_e2e_db import (
    _CRITERIA,
    _FIRST_PATH,
    _ListSource,
    _PortDouble,
    _reviewed,
    _RouterDouble,
    _shipped,
    _submit,
    _webhook,
)

pytestmark = pytest.mark.requires_db

# Ukrainian, so CP1251 is another sequence of bytes than UTF-8, and long enough
# for the doors to recover it; it carries the two quotes the recorded
# evaluation of ``test_text_review_e2e_db`` cites ("def solve(n):",
# "return n * 2"), so the first review stands on its verdicts.
_WORK = (
    "# Розв'язок домашнього завдання: подвоєння числа без перевірок.\n"
    "def solve(n):\n"
    '    """Повертає число, помножене на два, як просили в умові."""\n'
    "    return n * 2  # множимо на два й нічого більше не робимо\n"
    "\n"
    "# Перевірено вручну: solve(3) дає шість, а solve(0) дає нуль.\n"
)
_UTF8_LF = _WORK.encode("utf-8")
_CP1251_CRLF = _WORK.replace("\n", "\r\n").encode("cp1251")
_CHANGED = (_WORK + "# Ще один рядок: робота вже інша.\n").encode("utf-8")

_UNSAFE = (
    '{"is_safe": false, "violations": ["prompt_injection"], '
    '"confidence": 0.99, "reasoning": "it tells the reviewer what to say"}'
)
_OFF_TASK = '{"verdict": "mismatch", "confidence": 0.99, "reason": "another task"}'


def _unchanged_phrase(attempt: int) -> str:
    return phrases_for("ukr")["review.unchanged"].replace("{attempt}", str(attempt))


# ── Doubles: a router nobody may ask, providers nobody may call ─────────────


class _NoModelRouter:
    """The router of a repeat: any question to it is the lock broken."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute_stage(self, stage: Any, stage_name: str, /, **_: Any) -> Any:
        self.calls.append(stage_name)
        raise AssertionError(f"a repeated review asked the router for {stage_name}")

    async def execute_for_stage(self, stage_name: str, **_: Any) -> Any:
        self.calls.append(stage_name)
        raise AssertionError(f"a repeated review asked the router for {stage_name}")


@contextmanager
def _providers_watched() -> Iterator[list[AsyncMock]]:
    """Every provider's one call method, replaced by a mock that must stay idle."""
    with ExitStack() as stack:
        mocks = [
            stack.enter_context(
                patch.object(
                    provider,
                    "complete",
                    new=AsyncMock(side_effect=AssertionError("a provider was called")),
                )
            )
            for provider in PROVIDER_REGISTRY.values()
        ]
        yield mocks


# ── The world and the walk ──────────────────────────────────────────────────


@pytest.fixture()
async def task_world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, Any]]:
    """A ``task`` at version A with a ready model list for it, and a student."""
    world = await _make_world(session_factory, task_type="task")
    yield world
    await _drop_world(session_factory, world)


@pytest.fixture()
async def quiz_world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[dict[str, Any]]:
    world = await _make_world(session_factory, task_type="test")
    yield world
    await _drop_world(session_factory, world)


def _s3(raw: bytes, download: Path) -> MagicMock:
    def _download(_key: str) -> Path:
        # The body deletes its download on the way out; each run writes its own.
        download.write_bytes(raw)
        return download

    s3 = MagicMock()
    s3.extract_key = MagicMock(return_value="homework/answers.py")
    s3.download_file = AsyncMock(side_effect=_download)
    return s3


async def _walk(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    raw: bytes,
    tmp_path: Path,
    *,
    router: Any,
    source: _ListSource,
    port: _PortDouble | None = None,
    webhook: AsyncMock | None = None,
    config: PathConfig | None = None,
) -> uuid.UUID:
    """One submission of ``raw`` through the ARQ task and the new path's body."""
    submission_id, job_id = await _submit(session_factory, world)
    config = config or _shipped()
    ctx = {
        "session_factory": session_factory,
        "stage_router": router,
        "s3_client": _s3(raw, tmp_path / f"{submission_id}.py"),
        "job_try": 1,
    }
    with (
        patch(
            "course_supporter.homework.path_runner.get_path_config",
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
        patch(
            "course_supporter.homework.webhook.deliver_webhook",
            new=webhook or _webhook(),
        ),
    ):
        await arq_process_homework(ctx, str(job_id), str(submission_id))
    return submission_id


async def _first_review(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    tmp_path: Path,
    *,
    port: _PortDouble | None = None,
) -> HomeworkSubmission:
    """The student's first attempt, reviewed on the new path: delivered."""
    first_id = await _walk(
        session_factory,
        world,
        _UTF8_LF,
        tmp_path,
        router=_RouterDouble(session_factory),
        source=_ListSource(world["in_force"]),
        port=port,
    )
    first = await _row(session_factory, first_id)
    assert first.status == "delivered"
    assert first.review_result is not None
    return first


async def _register_rows(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        return int(
            await session.scalar(select(func.count()).select_from(ExternalServiceCall))
            or 0
        )


async def _rows_of_jobs(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> int:
    async with session_factory() as session:
        jobs = select(Job.id).where(Job.subject_id == submission_id)
        return int(
            await session.scalar(
                select(func.count())
                .select_from(ExternalServiceCall)
                .where(ExternalServiceCall.job_id.in_(jobs))
            )
            or 0
        )


async def _verdict_rows(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> list[SubmissionCriterionVerdict]:
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(SubmissionCriterionVerdict).where(
                        SubmissionCriterionVerdict.submission_id == submission_id
                    )
                )
            ).scalars()
        )


def _version_of(state: SubmissionState) -> str:
    """The evaluator's version of a ``task`` path in the shipped file."""
    version = evaluator_version(AssignmentType.TASK, state, _shipped())
    assert version is not None
    return version


def _assert_reviewed_anew(submission: HomeworkSubmission) -> None:
    """An ordinary review: no copy, no receipt of one; its evaluator named."""
    assert submission.status == "delivered"
    assert submission.attempt_receipt is not None
    assert DEDUP_RECEIPT_KEY not in submission.attempt_receipt
    assert submission.attempt_receipt[EVALUATOR_VERSION_RECEIPT_KEY].startswith("ev1:")
    structure = ReviewStructureV1.model_validate(submission.review_result)
    assert structure.unchanged_from_attempt is None
    assert submission.review_markdown == assemble_review(structure)


# ── а. End to end ───────────────────────────────────────────────────────────


class TestLockAEndToEnd:
    """а. The same work in CP1251 with CRLF: the review again, nothing paid."""

    async def test_the_same_work_in_cp1251_crlf_gets_its_review_for_nothing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        first = await _first_review(session_factory, task_world, tmp_path)
        assert first.attempt_no == 1
        register_before = await _register_rows(session_factory)

        router = _NoModelRouter()
        port = _PortDouble()
        webhook = _webhook()
        with _providers_watched() as providers:
            second_id = await _walk(
                session_factory,
                task_world,
                _CP1251_CRLF,
                tmp_path,
                router=router,
                source=_ListSource(task_world["in_force"]),
                port=port,
                webhook=webhook,
            )

        # ── No model: neither the router nor any provider was asked ──
        assert router.calls == []
        assert not any(mock.await_count for mock in providers)
        # ── No row in the register: no stage, no funds answer, no metrics ──
        assert await _register_rows(session_factory) == register_before
        assert await _rows_of_jobs(session_factory, second_id) == 0
        # ── No funds question and no estimate of a review never priced ──
        assert (port.reserved, port.accounted, port.released) == ([], [], [])
        async with session_factory() as session:
            estimates = await session.scalar(
                select(func.count())
                .select_from(ActionCostEstimate)
                .where(ActionCostEstimate.submission_id == second_id)
            )
        assert estimates == 0
        # ── Nothing judged: the copy has no verdicts of its own ──
        assert await _verdict_rows(session_factory, second_id) == []

        second = await _row(session_factory, second_id)
        # ── The same work: the ledger's fingerprint, attempt 2 after the first ──
        assert second.fingerprint == first.fingerprint is not None
        assert (second.attempt_no, second.previous_submission_id) == (2, first.id)
        # ── The review: the first one, marked as repeated from attempt 1 ──
        assert first.review_result is not None
        assert second.review_result == {
            **first.review_result,
            "unchanged_from_attempt": 1,
        }
        structure = ReviewStructureV1.model_validate(second.review_result)
        assert structure.unchanged_from_attempt == 1
        assert second.score == first.score
        # ── The markdown: the assembler's, the phrase first, then the first's ──
        assert second.review_markdown == assemble_review(structure)
        assert first.review_markdown is not None
        title, *rest = first.review_markdown.splitlines()
        assert second.review_markdown.splitlines() == [
            title,
            "",
            "Робота не змінилась із попередньої спроби (спроба 1), тому рецензію "
            "повторено без нової перевірки.",
            *rest,
        ]
        assert _unchanged_phrase(1) in second.review_markdown
        # ── The receipt: the ledger's, and the attempt repeated ──
        assert second.attempt_receipt is not None
        assert second.attempt_receipt[DEDUP_RECEIPT_KEY] == 1
        assert second.attempt_receipt["attempt_no"] == 2
        assert second.attempt_receipt["fingerprint"] == first.fingerprint
        assert first.attempt_receipt is not None
        assert (
            second.attempt_receipt[EVALUATOR_VERSION_RECEIPT_KEY]
            == first.attempt_receipt[EVALUATOR_VERSION_RECEIPT_KEY]
        )
        # ── Finished as a review is: completed, the webhook, delivered ──
        assert second.status == "delivered"
        [payload] = _reviewed(webhook)
        assert payload.structure == structure
        assert payload.review.review_text == second.review_markdown
        assert payload.review.score == first.score

    async def test_a_run_re_entered_after_the_copy_only_delivers(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """The worker died between the copy and the delivery: once, not twice."""
        await _first_review(session_factory, task_world, tmp_path)
        failing_webhook = AsyncMock(return_value=False)
        second_id = await _walk(
            session_factory,
            task_world,
            _UTF8_LF,
            tmp_path,
            router=_NoModelRouter(),
            source=_ListSource(task_world["in_force"]),
            webhook=failing_webhook,
        )
        stopped = await _row(session_factory, second_id)
        assert stopped.status == "completed"  # copied, not delivered

        async with session_factory() as session:
            job = Job(
                tenant_id=task_world["tenant_id"],
                course_node_id=task_world["node_id"],
                job_type="homework_processing",
                subject_type="homework_submission",
                subject_id=second_id,
                input_params={"submission_id": str(second_id)},
                status="queued",
            )
            session.add(job)
            await session.commit()
        router, port, webhook = _NoModelRouter(), _PortDouble(), _webhook()
        ctx = {
            "session_factory": session_factory,
            "stage_router": router,
            "s3_client": _s3(_UTF8_LF, tmp_path / "again.py"),
            "job_try": 1,
        }
        with (
            patch(
                "course_supporter.homework.path_runner.get_path_config",
                return_value=_shipped(),
            ),
            patch(
                "course_supporter.homework.path_runner.AlwaysEnoughFundsPort",
                return_value=port,
            ),
            patch("course_supporter.homework.webhook.deliver_webhook", new=webhook),
        ):
            await arq_process_homework(ctx, str(job.id), str(second_id))

        delivered = await _row(session_factory, second_id)
        assert delivered.status == "delivered"
        assert router.calls == [] and port.reserved == []
        assert (delivered.review_result, delivered.review_markdown) == (
            stopped.review_result,
            stopped.review_markdown,
        )
        assert delivered.attempt_receipt == stopped.attempt_receipt
        assert len(_reviewed(webhook)) == 1


# ── б. The funds port ───────────────────────────────────────────────────────


class TestLockBFundsPort:
    """б. Not asked on a repeat; asked as before on an ordinary submission."""

    async def test_asked_for_the_first_and_never_for_the_repeat(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        port = _PortDouble()
        await _first_review(session_factory, task_world, tmp_path, port=port)
        # The ordinary submission, as before: one reserve, every stage
        # accounted, one release.
        assert len(port.reserved) == 1
        assert len(port.accounted) == len(_FIRST_PATH)
        assert port.released == [SubmissionOutcome.COMPLETED]
        asked = (list(port.reserved), list(port.accounted), list(port.released))

        second_id = await _walk(
            session_factory,
            task_world,
            _UTF8_LF,
            tmp_path,
            router=_NoModelRouter(),
            source=_ListSource(task_world["in_force"]),
            port=port,
        )

        receipt = (await _row(session_factory, second_id)).attempt_receipt
        assert receipt is not None
        assert receipt[DEDUP_RECEIPT_KEY] == 1
        assert (port.reserved, port.accounted, port.released) == asked

    async def test_a_continuation_is_past_the_repeat(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """A revision the port already answered is continued, never repeated.

        It was held for funds before the same work was reviewed; by the time
        it is continued an attempt with its fingerprint exists. Its run has a
        checkpoint, so it walks its stages and the port hears its end — a
        repeat here would leave whatever the port reserved unreleased.
        """
        held_id = await _walk(
            session_factory,
            task_world,
            _UTF8_LF,
            tmp_path,
            router=_NoModelRouter(),
            source=_ListSource(task_world["in_force"]),
            port=_RefusingPort(),
        )
        assert (await _row(session_factory, held_id)).status == "awaiting_funds"
        reviewed = await _first_review(session_factory, task_world, tmp_path)
        assert (
            reviewed.fingerprint == (await _row(session_factory, held_id)).fingerprint
        )

        async with session_factory() as session:
            job = Job(
                tenant_id=task_world["tenant_id"],
                course_node_id=task_world["node_id"],
                job_type="homework_processing",
                subject_type="homework_submission",
                subject_id=held_id,
                input_params={"submission_id": str(held_id)},
                status="queued",
            )
            session.add(job)
            await session.commit()
        router, port = _RouterDouble(session_factory), _PortDouble()
        ctx = {
            "session_factory": session_factory,
            "stage_router": router,
            "s3_client": _s3(_UTF8_LF, tmp_path / "continued.py"),
            "job_try": 1,
        }
        with (
            patch(
                "course_supporter.homework.path_runner.get_path_config",
                return_value=_shipped(),
            ),
            patch(
                "course_supporter.homework.path_runner.AlwaysEnoughFundsPort",
                return_value=port,
            ),
            patch(
                "course_supporter.homework.criteria_evaluation."
                "build_criteria_list_service",
                return_value=_ListSource(task_world["in_force"]),
            ),
            patch("course_supporter.homework.webhook.deliver_webhook", new=_webhook()),
        ):
            await arq_process_homework(ctx, str(job.id), str(held_id))

        assert router.calls[0] == SAFETY
        assert len(port.reserved) == 1
        assert port.released == [SubmissionOutcome.COMPLETED]
        _assert_reviewed_anew(await _row(session_factory, held_id))


# ── в. A new version of the task or of its list ─────────────────────────────


def _criterion(cid: str, weight: str) -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": f"Criterion {cid}, as the author put it",
            "evidence": f"What shows {cid}",
            "weight": weight,
            "check_method": "model_verdict",
            "soft_descent": False,
            "concepts": [],
            "mandatory_points": [],
        }
    )


class TestLockVNewVersions:
    """в. Another task version or another list version: an ordinary review."""

    async def test_a_new_task_version_is_reviewed_anew(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        await _first_review(session_factory, task_world, tmp_path)
        # The author edits the task: version B, and its list composed anew.
        async with session_factory() as session:
            await session.execute(
                update(AuthoredDocument)
                .where(AuthoredDocument.id == task_world["doc_id"])
                .values(content_hash=_VERSION_B)
            )
            lists = TaskCriteriaListRepository(session)
            live = await lists.get_live(task_world["doc_id"])
            assert live is not None
            await lists.release(live.id)
            claimed = await lists.claim(
                authored_document_id=task_world["doc_id"],
                source_content_hash=_VERSION_B,
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
                input_fingerprint="e" * 64,
            )
            await session.commit()
        router = _RouterDouble(session_factory)

        second_id = await _walk(
            session_factory,
            task_world,
            _UTF8_LF,
            tmp_path,
            router=router,
            source=_ListSource(
                CriteriaInForce(
                    criteria=_CRITERIA, layer=CriteriaLayer.MODEL, source_id=claimed.id
                )
            ),
        )

        assert router.calls == [SAFETY, CRITERIA_EVALUATION, REVIEW_EXPLANATION]
        second = await _row(session_factory, second_id)
        _assert_reviewed_anew(second)
        assert second.task_version == _VERSION_B
        assert second.attempt_receipt is not None
        assert second.attempt_receipt["task_version_changed"] is True

    async def test_a_new_version_of_the_list_is_reviewed_anew(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """The author's edit of the criteria for the SAME task version."""
        await _first_review(session_factory, task_world, tmp_path)
        edited = (_criterion("c1", "must"), _criterion("c2", "should"))
        async with session_factory() as session:
            override = await TaskCriteriaOverrideRepository(session).replace(
                authored_document_id=task_world["doc_id"],
                source_content_hash=_VERSION_A,
                source_task_type="task",
                criteria=criteria_to_document(edited),
            )
            await session.commit()
        router = _RouterDouble(session_factory)
        router.content[CRITERIA_EVALUATION] = json.dumps(
            {
                "verdicts": [
                    {
                        "id": "c1",
                        "verdict": "met",
                        "quote": "def solve(n):",
                        "missing": None,
                    },
                    {
                        "id": "c2",
                        "verdict": "met",
                        "quote": "return n * 2",
                        "missing": None,
                    },
                ]
            }
        )
        router.content[REVIEW_EXPLANATION] = json.dumps(
            {
                "passed": True,
                "why": "Усе, що просили, є.",
                "remarks": [],
                "mentor_voice": None,
            }
        )

        second_id = await _walk(
            session_factory,
            task_world,
            _UTF8_LF,
            tmp_path,
            router=router,
            source=_ListSource(
                CriteriaInForce(
                    criteria=edited, layer=CriteriaLayer.AUTHOR, source_id=override.id
                )
            ),
        )

        assert router.calls == [SAFETY, CRITERIA_EVALUATION, REVIEW_EXPLANATION]
        second = await _row(session_factory, second_id)
        _assert_reviewed_anew(second)
        assert second.task_version == _VERSION_A  # the task did not change
        assert {
            (row.criteria_layer, row.criteria_source_id)
            for row in await _verdict_rows(session_factory, second_id)
        } == {(CriteriaLayer.AUTHOR, override.id)}


async def _reviewed_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    *,
    minute: int,
    fingerprint: str,
    task_version: str,
    list_address: tuple[str, uuid.UUID] | None,
    status: str = "delivered",
    evaluator: str | None = None,
) -> uuid.UUID:
    """An attempt as the body leaves one: numbered, fingerprinted, reviewed.

    Its receipt names ``evaluator`` — none when ``None``, as every receipt
    written before the evaluator's version.
    """
    receipt: dict[str, Any] = {"attempt_no": minute + 1, "fingerprint": fingerprint}
    if evaluator is not None:
        receipt[EVALUATOR_VERSION_RECEIPT_KEY] = evaluator
    submission_id = await _add(
        session_factory,
        world,
        status=status,
        minute=minute,
        task_version=task_version,
    )
    review = ReviewStructureV1(
        schema_version=REVIEW_SCHEMA_VERSION,
        language="ukr",
        verdict=Verdict(passed=True, why="Усе на місці."),
    )
    async with session_factory() as session:
        await session.execute(
            update(HomeworkSubmission)
            .where(HomeworkSubmission.id == submission_id)
            .values(
                attempt_no=minute + 1,
                fingerprint=fingerprint,
                attempt_receipt=receipt,
                review_result=review.model_dump(mode="json"),
                review_markdown=assemble_review(review),
                score=100,
            )
        )
        if list_address is not None:
            await session.execute(
                text(
                    "INSERT INTO submission_criterion_verdicts (id, tenant_id,"
                    " submission_id, criteria_layer, criteria_source_id, item_id,"
                    " item_kind, weight, verdict, model_verdict, quote) VALUES"
                    " (:id, :tenant, :submission, :layer, :source, 'c1',"
                    " 'criterion', 'must', 'met', 'met', 'def solve(n):')"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant": world["tenant_id"],
                    "submission": submission_id,
                    "layer": list_address[0],
                    "source": list_address[1],
                },
            )
        await session.commit()
    return submission_id


class TestLockVEachKeyAtTheRepository:
    """в at the repository: each key of the match, alone, refuses it."""

    _FP = "f" * 64
    _LIST = "11111111-1111-1111-1111-111111111111"
    _EVALUATOR = "ev1:" + "e" * 16

    def _key(self, **changed: Any) -> dict[str, Any]:
        key: dict[str, Any] = {
            "fingerprint": self._FP,
            "task_version": _VERSION_A,
            "criteria_list": (CriteriaLayer.MODEL.value, uuid.UUID(self._LIST)),
            "evaluator_versions": frozenset({self._EVALUATOR}),
        }
        return key | changed

    async def _lookup(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        world: dict[str, Any],
        **key: Any,
    ) -> uuid.UUID | None:
        asking = await _add(session_factory, world, status="received", minute=30)
        async with session_factory() as session:
            found = await HomeworkRepository(session).latest_unchanged_attempt(
                tenant_id=world["tenant_id"],
                student_id=world["student_id"],
                authored_document_id=world["doc_id"],
                excluding=asking,
                **key,
            )
        return found.id if found is not None else None

    async def test_everything_equal_is_found_and_the_latest_wins(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        key = self._key()
        await _reviewed_attempt(
            session_factory,
            task_world,
            minute=0,
            fingerprint=key["fingerprint"],
            task_version=key["task_version"],
            list_address=key["criteria_list"],
            evaluator=self._EVALUATOR,
        )
        latest = await _reviewed_attempt(
            session_factory,
            task_world,
            minute=1,
            fingerprint=key["fingerprint"],
            task_version=key["task_version"],
            list_address=key["criteria_list"],
            evaluator=self._EVALUATOR,
        )

        assert await self._lookup(session_factory, task_world, **key) == latest

    @pytest.mark.parametrize(
        "changed",
        [
            {"fingerprint": "0" * 64},
            {"task_version": _VERSION_B},
            {"criteria_list": (CriteriaLayer.MODEL.value, uuid.uuid4())},
            {"criteria_list": (CriteriaLayer.AUTHOR.value, uuid.UUID(_LIST))},
            {"evaluator_versions": frozenset({"ev1:" + "0" * 16})},
        ],
        ids=["fingerprint", "task_version", "list_id", "list_layer", "evaluator"],
    )
    async def test_one_key_different_is_not_found(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        changed: dict[str, Any],
    ) -> None:
        stored = self._key()
        await _reviewed_attempt(
            session_factory,
            task_world,
            minute=0,
            fingerprint=stored["fingerprint"],
            task_version=stored["task_version"],
            list_address=stored["criteria_list"],
            evaluator=self._EVALUATOR,
        )

        assert (
            await self._lookup(session_factory, task_world, **self._key(**changed))
            is None
        )

    async def test_without_a_list_the_key_is_fingerprint_and_version(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        """A project's key: an attempt judged against no list still matches."""
        source = await _reviewed_attempt(
            session_factory,
            task_world,
            minute=0,
            fingerprint=self._FP,
            task_version=_VERSION_A,
            list_address=None,
            evaluator=self._EVALUATOR,
        )

        found = await self._lookup(
            session_factory, task_world, **self._key(criteria_list=None)
        )
        assert found == source
        assert (
            await self._lookup(
                session_factory,
                task_world,
                **self._key(criteria_list=None, task_version=_VERSION_B),
            )
            is None
        )

    async def test_the_source_s_evaluator_among_those_in_force_is_found(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        """A review written on ``first`` is found for a ``repeat_*`` asking."""
        source = await _reviewed_attempt(
            session_factory,
            task_world,
            minute=0,
            fingerprint=self._FP,
            task_version=_VERSION_A,
            list_address=self._key()["criteria_list"],
            evaluator=self._EVALUATOR,
        )
        in_force = frozenset({"ev1:" + "1" * 16, self._EVALUATOR})

        found = await self._lookup(
            session_factory, task_world, **self._key(evaluator_versions=in_force)
        )
        assert found == source

    async def test_a_receipt_that_names_no_evaluator_is_never_a_source(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
    ) -> None:
        """Lock г of the evaluator's version, at the repository."""
        await _reviewed_attempt(
            session_factory,
            task_world,
            minute=0,
            fingerprint=self._FP,
            task_version=_VERSION_A,
            list_address=self._key()["criteria_list"],
            evaluator=None,
        )

        assert await self._lookup(session_factory, task_world, **self._key()) is None


# ── г. Only an attempt is a source ──────────────────────────────────────────


class TestLockGOnlyAnAttemptIsASource:
    """г. Refused, off-task or failed — the same fingerprint, still no copy."""

    @pytest.mark.parametrize(
        ("stage", "answer", "status"),
        [(SAFETY, _UNSAFE, "rejected"), (ATTEMPT_CLASSIFIER, _OFF_TASK, "mismatch")],
        ids=["refused_for_safety", "not_an_attempt"],
    )
    async def test_a_refused_first_then_the_same_work_is_reviewed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
        stage: str,
        answer: str,
        status: str,
    ) -> None:
        refusing = _RouterDouble(session_factory)
        refusing.content[stage] = answer
        refused_id = await _walk(
            session_factory,
            task_world,
            _UTF8_LF,
            tmp_path,
            router=refusing,
            source=_ListSource(task_world["in_force"]),
        )
        refused = await _row(session_factory, refused_id)
        assert refused.status == status
        router = _RouterDouble(session_factory)

        again_id = await _walk(
            session_factory,
            task_world,
            _UTF8_LF,
            tmp_path,
            router=router,
            source=_ListSource(task_world["in_force"]),
        )

        again = await _row(session_factory, again_id)
        assert again.fingerprint == refused.fingerprint is not None
        assert router.calls == [
            SAFETY,
            ATTEMPT_CLASSIFIER,
            CRITERIA_EVALUATION,
            REVIEW_EXPLANATION,
        ]
        _assert_reviewed_anew(again)

    @pytest.mark.parametrize("not_an_attempt", ["rejected", "mismatch", "failed"])
    async def test_a_review_on_a_row_that_is_no_attempt_is_not_copied(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
        not_an_attempt: str,
    ) -> None:
        """Every other key equal — review, verdicts on the list in force,
        fingerprint, version, number: the status alone says it is no attempt."""
        first = await _first_review(session_factory, task_world, tmp_path)
        await _set_status(session_factory, first.id, not_an_attempt)
        router = _RouterDouble(session_factory)

        again_id = await _walk(
            session_factory,
            task_world,
            _UTF8_LF,
            tmp_path,
            router=router,
            source=_ListSource(task_world["in_force"]),
        )

        assert router.calls[0] == SAFETY
        _assert_reviewed_anew(await _row(session_factory, again_id))


# ── ґ. A test, and today's Mentor ───────────────────────────────────────────


class TestLockGgATestIsScoredAnew:
    """ґ. Every attempt of a test goes on to its own scoring."""

    async def test_the_same_answers_after_a_reviewed_attempt(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        quiz_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """The second walk asks the funds port (and is held there) — the road
        of a review scored anew; a repeat would never have asked it."""
        answers = stored_answers(uuid.uuid4(), {"1": ["а"], "2": ["б", "в"]}).encode()
        first_id = await _through_the_doors(
            session_factory, quiz_world, answers, tmp_path
        )
        review = ReviewStructureV1(
            schema_version=REVIEW_SCHEMA_VERSION,
            language="ukr",
            verdict=Verdict(passed=True),
            test=review_structure.TestSection(
                score=100,
                questions=[
                    review_structure.TestQuestionResult(number="1", correct=True),
                    review_structure.TestQuestionResult(number="2", correct=True),
                ],
            ),
        )
        async with session_factory() as session:
            await session.execute(
                update(HomeworkSubmission)
                .where(HomeworkSubmission.id == first_id)
                .values(
                    status="delivered",
                    review_result=review.model_dump(mode="json"),
                    review_markdown=assemble_review(review),
                    score=100,
                )
            )
            await session.commit()

        second_id = await _through_the_doors(
            session_factory, quiz_world, answers, tmp_path
        )

        first, second = (
            await _row(session_factory, first_id),
            await _row(session_factory, second_id),
        )
        assert second.fingerprint == first.fingerprint is not None
        assert second.task_version == first.task_version is not None
        assert (second.attempt_no, second.previous_submission_id) == (2, first.id)
        assert second.status == "awaiting_funds"
        assert second.review_result is None
        assert second.attempt_receipt is not None
        assert DEDUP_RECEIPT_KEY not in second.attempt_receipt


@pytest.mark.requires_redis
class TestLockGgTodaysMentorRepeatsNothing:
    """ґ. Today's Mentor reviews the same work again — even after the new path."""

    async def test_after_a_new_path_review_today_reviews_again(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        first = await _first_review(session_factory, task_world, tmp_path)
        # ``task`` handed back to today's Mentor; the same work again.
        submission_id, job_id = await _submit(session_factory, task_world)
        router, sanity, review = _TodaysRouter(), _RecordingSanity(), _RecordingReview()
        ctx = {
            "session_factory": session_factory,
            "stage_router": router,
            "s3_client": _s3(_UTF8_LF, tmp_path / "today.py"),
            "job_try": 1,
        }
        with (
            patch(
                "course_supporter.homework.path_runner.get_path_config",
                return_value=shipped_with_todays_mentor_for(AssignmentType.TASK),
            ),
            patch(
                "course_supporter.homework.sanity_gate.build_sanity_gate_service",
                new=MagicMock(return_value=sanity),
            ),
            patch(
                "course_supporter.homework.review_graph.build_mentor_review_service",
                new=MagicMock(return_value=review),
            ),
            patch("course_supporter.homework.webhook.deliver_webhook", new=_webhook()),
        ):
            await arq_process_homework(ctx, str(job_id), str(submission_id))

        today = await _row(session_factory, submission_id)
        assert today.fingerprint == first.fingerprint is not None
        assert today.attempt_no == 2
        # Today's stages ran on it: its safety, its sanity gate, its review.
        assert "safety_check" in router.rendered
        assert len(sanity.seen) == 1
        assert len(review.rendered) == 1
        assert today.status in {"completed", "delivered"}
        assert today.attempt_receipt is not None
        assert DEDUP_RECEIPT_KEY not in today.attempt_receipt
        assert today.review_result is not None
        assert "unchanged_from_attempt" not in today.review_result
        assert today.review_markdown == "ok"


# ── The road back to an ordinary review: changed work ───────────────────────


class TestChangedWorkIsReviewed:
    """The control of а: one line more, and the stages run as before."""

    async def test_changed_work_walks_the_repeat_path(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        await _first_review(session_factory, task_world, tmp_path)
        router = _RouterDouble(session_factory)

        second_id = await _walk(
            session_factory,
            task_world,
            _CHANGED,
            tmp_path,
            router=router,
            source=_ListSource(task_world["in_force"]),
        )

        assert router.calls == [SAFETY, CRITERIA_EVALUATION, REVIEW_EXPLANATION]
        _assert_reviewed_anew(await _row(session_factory, second_id))


# ── A lookup that breaks: the session stays usable, the review is ordinary ──


async def _broken_lookup(self: HomeworkRepository, **_: Any) -> None:
    """The lookup's query fails in the database, in the body's own session.

    A real error of Postgres, not a mock that raises: the statement aborts the
    session's transaction, as a broken lookup in production would.
    """
    await self._session.execute(text("SELECT * FROM k3_no_such_table"))


class TestALookupThatFailsIsAnOrdinaryReview:
    """The dedup's reads run in a savepoint: a failed one costs only the repeat."""

    async def test_the_submission_walks_its_stages_to_the_end(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """A match exists, the lookup breaks: everything after it still runs
        through the same session — the funds port, every stage, the review."""
        await _first_review(session_factory, task_world, tmp_path)
        router, port = _RouterDouble(session_factory), _PortDouble()

        with (
            patch.object(
                HomeworkRepository, "latest_unchanged_attempt", new=_broken_lookup
            ),
            capture_logs() as logs,
        ):
            second_id = await _walk(
                session_factory,
                task_world,
                _UTF8_LF,
                tmp_path,
                router=router,
                source=_ListSource(task_world["in_force"]),
                port=port,
            )

        assert router.calls == [SAFETY, CRITERIA_EVALUATION, REVIEW_EXPLANATION]
        assert len(port.reserved) == 1
        assert port.released == [SubmissionOutcome.COMPLETED]
        second = await _row(session_factory, second_id)
        _assert_reviewed_anew(second)
        assert second.attempt_no == 2
        [failed] = [e for e in logs if e["event"] == "attempt_dedup_lookup_failed"]
        assert failed["log_level"] == "warning"
        assert failed["submission_id"] == str(second_id)


# ── The evaluator's version in the key (task 13) ────────────────────────────


async def _name_the_evaluator(
    session_factory: async_sessionmaker[AsyncSession],
    submission_id: uuid.UUID,
    evaluator: str | None,
) -> None:
    """Rewrite the evaluator a receipt names; ``None`` drops the key."""
    async with session_factory() as session:
        receipt = dict(
            await session.scalar(
                select(HomeworkSubmission.attempt_receipt).where(
                    HomeworkSubmission.id == submission_id
                )
            )
            or {}
        )
        if evaluator is None:
            del receipt[EVALUATOR_VERSION_RECEIPT_KEY]
        else:
            receipt[EVALUATOR_VERSION_RECEIPT_KEY] = evaluator
        await session.execute(
            update(HomeworkSubmission)
            .where(HomeworkSubmission.id == submission_id)
            .values(attempt_receipt=receipt)
        )
        await session.commit()


async def _the_same_work_again(
    session_factory: async_sessionmaker[AsyncSession],
    world: dict[str, Any],
    tmp_path: Path,
) -> tuple[HomeworkSubmission, list[str]]:
    """The same work as the first review's, walked with a router that answers."""
    router = _RouterDouble(session_factory)
    again_id = await _walk(
        session_factory,
        world,
        _UTF8_LF,
        tmp_path,
        router=router,
        source=_ListSource(world["in_force"]),
    )
    return await _row(session_factory, again_id), router.calls


class TestEvaluatorLockBReviewedThenCopied:
    """б. The review names its evaluator; the copy, made for nothing, the same."""

    async def test_a_task_reviewed_then_the_same_work_copied_with_its_evaluator(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        first = await _first_review(session_factory, task_world, tmp_path)
        assert first.attempt_receipt is not None
        written_by = first.attempt_receipt[EVALUATOR_VERSION_RECEIPT_KEY]
        # The version of the path the review walked: the first attempt's.
        assert written_by == _version_of(SubmissionState.FIRST)
        register_before = await _register_rows(session_factory)

        router = _NoModelRouter()
        with _providers_watched() as providers:
            second_id = await _walk(
                session_factory,
                task_world,
                _UTF8_LF,
                tmp_path,
                router=router,
                source=_ListSource(task_world["in_force"]),
            )

        # ── A copy: no model asked, no row in the register ──
        assert router.calls == []
        assert not any(mock.await_count for mock in providers)
        assert await _register_rows(session_factory) == register_before
        second = await _row(session_factory, second_id)
        assert second.status == "delivered"
        assert second.review_result == {
            **(first.review_result or {}),
            "unchanged_from_attempt": 1,
        }
        # ── Its receipt: the attempt repeated, and the evaluator that wrote it ──
        assert second.attempt_receipt is not None
        assert second.attempt_receipt[DEDUP_RECEIPT_KEY] == 1
        assert second.attempt_receipt[EVALUATOR_VERSION_RECEIPT_KEY] == written_by
        # Inherited from the source, not the version of the path the copy walked.
        assert written_by != _version_of(SubmissionState.REPEAT_WITHOUT_REPLIES)


class TestEvaluatorLockVChangedEvaluatorReviewsAnew:
    """в. Another evaluator than the source's: the same work is reviewed anew."""

    async def test_after_the_verdict_rules_were_raised(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        first = await _first_review(session_factory, task_world, tmp_path)
        assert first.attempt_receipt is not None
        monkeypatch.setattr(
            criteria_verdicts,
            "VERDICT_RULES_VERSION",
            criteria_verdicts.VERDICT_RULES_VERSION + 1,
        )

        again, calls = await _the_same_work_again(session_factory, task_world, tmp_path)

        assert calls == [SAFETY, CRITERIA_EVALUATION, REVIEW_EXPLANATION]
        _assert_reviewed_anew(again)
        assert again.fingerprint == first.fingerprint is not None
        assert again.attempt_receipt is not None
        # Written by today's evaluator, under the rules raised.
        written_by = again.attempt_receipt[EVALUATOR_VERSION_RECEIPT_KEY]
        assert written_by == _version_of(SubmissionState.REPEAT_WITHOUT_REPLIES)
        assert written_by != first.attempt_receipt[EVALUATOR_VERSION_RECEIPT_KEY]

    async def test_the_source_written_by_another_evaluator(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        first = await _first_review(session_factory, task_world, tmp_path)
        await _name_the_evaluator(session_factory, first.id, "ev1:" + "0" * 16)

        again, calls = await _the_same_work_again(session_factory, task_world, tmp_path)

        assert calls == [SAFETY, CRITERIA_EVALUATION, REVIEW_EXPLANATION]
        _assert_reviewed_anew(again)


class TestEvaluatorLockGNoEvaluatorNoSource:
    """г. A receipt that names no evaluator — every one before — is no source."""

    async def test_an_attempt_reviewed_before_the_evaluator_s_version(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_world: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        first = await _first_review(session_factory, task_world, tmp_path)
        await _name_the_evaluator(session_factory, first.id, None)
        source = await _row(session_factory, first.id)
        assert source.attempt_receipt is not None
        assert EVALUATOR_VERSION_RECEIPT_KEY not in source.attempt_receipt

        again, calls = await _the_same_work_again(session_factory, task_world, tmp_path)

        assert calls == [SAFETY, CRITERIA_EVALUATION, REVIEW_EXPLANATION]
        _assert_reviewed_anew(again)
        assert again.fingerprint == source.fingerprint is not None
