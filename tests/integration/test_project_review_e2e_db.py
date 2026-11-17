"""A project reviewed end to end on the new path (project chain K7).

``vision-rules#25``: what the chain's steps prove one by one — the doors' two
corpora (K1, K3), the project's own stages and prompts (K4), the place of a
problem and the marks of the author's lines (K5), the builder and the refusals
(K6, #677) — is joined here by one walk. The project is on the new path in
test mode, with the path the next step (K8) will give it, in this test's own
configuration; ``config/submission_paths.yaml`` is read for the stages' own
descriptions and is not touched. Everything below the model layer is the real
thing: the ARQ task (``arq_process_homework``), the seam, the real body, the
project's door against a real base in the S3-compatible storage (``moto_server``
in this run; SeaweedFS in the compose file), the attempt ledger, the dedup, the
five stages of the path with their prompts, the verdict repository, the course
search over a real course, the builder, the assembler, the webhook payload.

The doors, and why each:

* the ROUTER — the alternative is a paid call. It renders each stage's prompt
  from the stage's own file exactly as the real router does (so what a request
  carries is what a model would read), answers from a recorded queue — a stage
  asked more often than it has answers fails the test — runs the stage's own
  check on every answer, and leaves the register row a real call would, so each
  stage's price is summed from real rows;
* the CRITERIA SOURCE — the production service composes a list on a miss
  (another paid call). The author's list itself is a real row of the author's
  edits: the stages read it back by the address the verdict rows carry;
* the FUNDS PORT and the webhook's transport — nothing to spend, nowhere to
  send.

The runs:

1. a first submission — the author's base, one file of it changed and one added
   (``TestAFirstSubmissionOfAProject``): what the safety stage, the evaluation
   and the explanation are each shown; the verdicts (a quote of the new file
   stands, a quote of the base is asked about again and then not met, a "not
   met" keeps the line of the student's file); the course link found for the
   unmet criterion and its place in the review; the review's structure and
   markdown; the attempt, its fingerprint and the evaluator's version in the
   receipt; the webhook; the register's rows and their money against the
   stages' ceilings;
2. the same work again by the same student (``TestTheSameWorkAgain``): the
   first attempt's review is given again, no stage is called and nothing is
   paid for (task 13, K3);
3. a changed work again (``TestAChangedWorkAgain``): a new, full review, and
   the second attempt.

Requires ``docker compose up -d`` (or Postgres, Redis and an S3-compatible
storage on the settings' addresses); run with ``--run-db``.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.tasks import arq_process_homework
from course_supporter.config import get_settings
from course_supporter.criteria_kinds import CriteriaLayer, RepeatReason, VerdictValue
from course_supporter.funds_port import (
    FUNDS_PORT_ACTION,
    FundsAnswer,
    SubmissionContext,
    SubmissionOutcome,
    VersionWorkContext,
)
from course_supporter.homework.attempt_dedup import (
    DEDUP_RECEIPT_KEY,
    same_work_reaches_the_new_path,
)
from course_supporter.homework.criteria_form import Criterion, criteria_to_document
from course_supporter.homework.criteria_list_service import CriteriaInForce
from course_supporter.homework.criteria_verdicts import RepeatItem
from course_supporter.homework.evaluator_version import (
    EVALUATOR_VERSION_RECEIPT_KEY,
    evaluator_version,
)
from course_supporter.homework.path_config import (
    ModelStage,
    PathConfig,
    ServedBy,
    SubmissionState,
    in_test_mode,
    load_path_config,
)
from course_supporter.homework.path_stages import (
    ATTEMPT_CLASSIFIER,
    COURSE_LINK_LOOKUP,
    PROJECT_CRITERIA_EVALUATION,
    PROJECT_REVIEW_EXPLANATION,
    SAFETY,
)
from course_supporter.homework.project_submission import _submission_snapshot_key
from course_supporter.homework.review_assembler import assemble_review
from course_supporter.homework.text_budget import path_context_budget_chars
from course_supporter.llm.ladder_config import StageConfig
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.registry import load_registry
from course_supporter.llm.stage_router import StageResult
from course_supporter.models.review_structure import (
    PassRule,
    Position,
    Reference,
    ReviewStructureV1,
)
from course_supporter.models.source import AssignmentType
from course_supporter.models.webhook import WebhookReviewedPayload
from course_supporter.normalizer import manifest_to_jsonb, normalize_archive
from course_supporter.review_metrics import REVIEW_METRICS_ACTION
from course_supporter.service_logging import _persist
from course_supporter.storage.orm import (
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    ProjectBase,
    Student,
    SubmissionCriterionVerdict,
    Tenant,
)
from course_supporter.storage.s3 import S3Client
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)
from tests._helpers.course_node_factory import make_root_course_node
from tests._helpers.priced import priced_exact
from tests._helpers.recorded_project import (
    BASE,
    PROJECT,
    QUOTE_OF_CHANGED,
    QUOTE_OF_NEIGHBOR,
    QUOTE_OF_NEW,
    SECRET,
    STATS,
    STUDENTS_APP,
    UTILS,
)
from tests.integration.test_document_segment_repository import (
    _seed_child,
    _seed_material,
    _seed_segment,
    _seed_summary,
)

pytestmark = pytest.mark.requires_db

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WEBHOOK_URL = "https://example.com/hook"
_TASK_HASH = "h" * 64

# The path K8 gives the project, the same on every state of a submission.
_PATH = (
    SAFETY,
    ATTEMPT_CLASSIFIER,
    PROJECT_CRITERIA_EVALUATION,
    COURSE_LINK_LOOKUP,
    PROJECT_REVIEW_EXPLANATION,
)


def _shipped() -> PathConfig:
    """The shipped file, read from the repository (never cached)."""
    return load_path_config(_REPO_ROOT / "config" / "submission_paths.yaml")


def _config() -> PathConfig:
    """The shipped stages with ``project`` on the new path, in test mode."""
    raw = _shipped().model_dump(mode="json")
    raw["task_types"]["project"] = {
        "served_by": ServedBy.NEW_PATH.value,
        "test_mode": True,
        "paths": {state.value: list(_PATH) for state in SubmissionState},
    }
    return PathConfig.model_validate(raw)


# ── The author's list ───────────────────────────────────────────────────────


def _criterion(
    cid: str, weight: str, *, points: int = 0, concepts: tuple[str, ...] = ()
) -> Criterion:
    return Criterion.model_validate(
        {
            "id": cid,
            "text": f"Criterion {cid}",
            "evidence": f"What shows {cid}",
            "weight": weight,
            "check_method": "mandatory_points" if points else "model_verdict",
            "soft_descent": False,
            "concepts": list(concepts),
            "mandatory_points": [
                {"id": f"{cid}.p{n}", "text": f"Point {n} of {cid}"}
                for n in range(1, points + 1)
            ],
        }
    )


# Weights 3 / 2 / 1. c2 is checked by two points: reading the lines and
# printing the statistics. c3 names a concept the course teaches.
# The author's edit: c1 a "must" the author made mandatory themselves
# (``must_by_author``, 2026-11-17).
_CRITERIA = (
    _criterion("c1", "must").model_copy(update={"must_by_author": True}),
    _criterion("c2", "should", points=2).model_copy(update={"must_by_author": False}),
    _criterion("c3", "may", concepts=("empty-input",)).model_copy(
        update={"must_by_author": False}
    ),
)

# ── The recorded answers ────────────────────────────────────────────────────

_QUOTE_OF_THE_PLACE = "return 0.0"
"""``stats.py`` line 3: the student's own, in a NEW file — where c3 falls short."""
_PLACE = Position(kind="file", value="stats.py:3")
_MISSING = "Порожній вхід мовчки дає нуль, а користувач про це не дізнається."


def _verdicts(*verdicts: dict[str, Any]) -> str:
    return json.dumps({"verdicts": list(verdicts)}, ensure_ascii=False)


def _met(item: str, quote: str) -> dict[str, Any]:
    return {"id": item, "verdict": "met", "quote": quote, "missing": None}


def _not_met(item: str, quote: str) -> dict[str, Any]:
    return {"id": item, "verdict": "not_met", "quote": quote, "missing": _MISSING}


# c1 met on a line of the NEW file; c2.p1 "met" on the author's line of a
# neighbor; c2.p2 met on the student's line of the changed file; c3 not met on
# the line of the new file where the problem stands.
_FIRST_ANSWER = _verdicts(
    _met("c1", QUOTE_OF_NEW),
    _met("c2.p1", QUOTE_OF_NEIGHBOR),
    _met("c2.p2", QUOTE_OF_CHANGED),
    _not_met("c3", _QUOTE_OF_THE_PLACE),
)
# Asked again about c2.p1 alone, the model gives the author's line once more.
_REPEAT_ANSWER = _verdicts(_met("c2.p1", QUOTE_OF_NEIGHBOR))

_WHY = (
    "Модуль статистики охайний, але частину роботи лишено на коді стартового проєкту."
)
_EXPLANATION = json.dumps(
    {
        "passed": False,
        "why": _WHY,
        # The lightest first: the builder puts the heaviest first.
        "remarks": [
            {
                "id": "c3",
                "what": "Порожній файл дає нуль без жодного повідомлення.",
                "why": "Користувач не побачить, що даних не було.",
                "todo": "Повідомте про порожній вхід замість нуля.",
                "links": ["c3.l1"],
            },
            {
                "id": "c2",
                "what": (
                    "Читання рядків з файлу є лише в коді стартового проєкту, "
                    "а не у вашій роботі."
                ),
                "why": "Завдання просило, щоб цю частину написали саме ви.",
                "todo": "Напишіть читання файлу у власному модулі.",
                "links": [],
            },
        ],
        "mentor_voice": None,
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
    PROJECT_CRITERIA_EVALUATION: 0.02,
    PROJECT_REVIEW_EXPLANATION: 0.008,
}

_TAUGHT = "Порожній вхід — перший край, який варто перевірити."
_TITLE = "Краї входу"


def _answers() -> dict[str, list[str]]:
    """The queue of every model stage of one full review, in order."""
    return {
        SAFETY: [_SAFETY],
        ATTEMPT_CLASSIFIER: [_ATTEMPT],
        PROJECT_CRITERIA_EVALUATION: [_FIRST_ANSWER, _REPEAT_ANSWER],
        PROJECT_REVIEW_EXPLANATION: [_EXPLANATION],
    }


# ── The doubles ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Request:
    """What one call of the router was asked: the stage, the variables its
    prompt was rendered from, and the prompt the model would have read."""

    stage: str
    variables: dict[str, Any]
    system: str
    user: str


class _RouterDouble:
    """Answers each stage from a recorded queue and leaves its register row.

    The prompt is rendered from the stage's own file as the router renders it;
    the stage's own check runs on every answer, as the real router runs it;
    the answer is credited to the stage's first rung, as if it answered.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.requests: list[_Request] = []
        self.queue: dict[str, list[str]] = {}
        self._session_factory = session_factory

    @property
    def calls(self) -> list[str]:
        return [request.stage for request in self.requests]

    def asked(self, stage_name: str) -> list[_Request]:
        return [r for r in self.requests if r.stage == stage_name]

    def answer_with(self, queue: dict[str, list[str]]) -> None:
        self.requests.clear()
        self.queue = {stage: list(answers) for stage, answers in queue.items()}

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
        repeat_on_rung: int | None = None,
        **render_context: Any,
    ) -> StageResult:
        rendered = load_prompt(stage.prompt_ref).render(**render_context)
        self.requests.append(
            _Request(
                stage_name,
                render_context,
                rendered.system or "",
                rendered.user or "",
            )
        )
        if not self.queue.get(stage_name):
            msg = f"{stage_name} was asked more often than it has recorded answers"
            raise AssertionError(msg)
        content = self.queue[stage_name].pop(0)
        rung = stage.ladder[0]
        await _persist(
            self._session_factory,
            action=stage_name,
            strategy="default",
            provider=rung.provider,
            model_id=rung.model,
            unit_type="tokens",
            priced=priced_exact(_PRICE[stage_name]),
        )
        if response_validator is not None:
            response_validator(content)
        return StageResult(
            content=content,
            provider_used=rung.provider,
            model_used=rung.model,
            attempt_count=1,
        )


class _ListSource:
    """The criteria source: the author's list as the real row stores it."""

    def __init__(self, in_force: CriteriaInForce) -> None:
        self._in_force = in_force

    async def get_or_compose(self, authored_document_id: uuid.UUID) -> CriteriaInForce:
        return self._in_force


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
        self,
        context: VersionWorkContext,
        actual_usd: float,
        outcome: SubmissionOutcome = SubmissionOutcome.COMPLETED,
    ) -> None:
        raise AssertionError("the submission path must not use the version operations")


# ── The world: a course, a project with its base and list, a student ────────


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.fixture()
async def s3_client() -> AsyncGenerator[S3Client]:
    s = get_settings()
    client = S3Client(
        endpoint_url=s.s3_endpoint,
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key.get_secret_value(),
        bucket=s.s3_bucket,
    )
    await client.open()
    try:
        await client.ensure_bucket()
        yield client
    finally:
        await client.close()


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession], s3_client: S3Client
) -> AsyncGenerator[dict[str, Any]]:
    """A course teaching a concept, its project with a base and an author's
    list, and one student; no submission yet."""
    base_key = f"base/k7/{uuid.uuid4().hex}/snapshot.zip"
    base = normalize_archive(_zip(BASE), archive_kind="zip")
    await s3_client.upload_file(base_key, base.canonical_zip, "application/zip")
    async with session_factory() as session:
        tenant = Tenant(name=f"k7-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        root = make_root_course_node(tenant_id=tenant.id, title="Course", order=0)
        session.add(root)
        await session.flush()
        lesson = await _seed_child(session, tenant, root, title="Лекція", order=0)
        material = await _seed_material(session, lesson, root, order=0)
        summary = await _seed_summary(session, material, root, title=_TITLE)
        segment = await _seed_segment(
            session,
            summary,
            root,
            main=["empty-input"],
            content=_TAUGHT,
            start_paragraph=2,
            end_paragraph=3,
        )
        project = await _seed_material(
            session, lesson, root, task_type="project", order=5, filename=None
        )
        project.content_hash = _TASK_HASH
        project.language = "ukr"
        session.add(
            ProjectBase(
                authored_document_id=project.id,
                version=1,
                archive_key="k/v1/original.zip",
                snapshot_key=base_key,
                snapshot_hash=base.snapshot_hash,
                manifest=manifest_to_jsonb(base.manifest),
                state="ready",
            )
        )
        await session.flush()
        base_row_id = await session.scalar(
            select(ProjectBase.id).where(ProjectBase.authored_document_id == project.id)
        )
        edit = await TaskCriteriaOverrideRepository(session).replace(
            authored_document_id=project.id,
            source_content_hash=_TASK_HASH,
            source_task_type="project",
            criteria=criteria_to_document(_CRITERIA),
        )
        student = Student(
            tenant_id=tenant.id, external_id=f"stu-{uuid.uuid4().hex[:6]}"
        )
        session.add(student)
        await session.commit()
        ids: dict[str, Any] = {
            "tenant_id": tenant.id,
            "root_id": root.id,
            "lesson_id": lesson.id,
            "doc_id": project.id,
            "base_id": base_row_id,
            "student_id": student.id,
            "segment_id": segment.id,
            "in_force": CriteriaInForce(
                criteria=_CRITERIA, layer=CriteriaLayer.AUTHOR, source_id=edit.id
            ),
            "submissions": [],
            "s3_keys": [base_key],
        }

    yield ids

    for key in ids["s3_keys"]:
        try:
            await s3_client.delete_object(key)
        except Exception:  # noqa: S112 - best-effort cleanup
            continue
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


@dataclass(frozen=True)
class _Submitted:
    submission_id: uuid.UUID
    job_id: uuid.UUID


async def _submit(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    world: dict[str, Any],
    files: dict[str, bytes],
) -> _Submitted:
    """A submission of ``files`` (an archive in the storage) with its first job."""
    s = get_settings()
    raw_key = f"homework/k7/{uuid.uuid4().hex}/proj.zip"
    await s3_client.upload_file(raw_key, _zip(files), "application/zip")
    world["s3_keys"] += [raw_key, _submission_snapshot_key(raw_key)]
    async with session_factory() as session:
        submission = HomeworkSubmission(
            tenant_id=world["tenant_id"],
            student_id=world["student_id"],
            course_node_id=world["root_id"],
            node_id=world["lesson_id"],
            authored_document_id=world["doc_id"],
            file_url=f"{s.s3_endpoint}/{s.s3_bucket}/{raw_key}",
            file_type="application/zip",
            original_filename="proj.zip",
            webhook_url=_WEBHOOK_URL,
            response_language="ukr",
            status="received",
            base_id=world["base_id"],
        )
        session.add(submission)
        await session.flush()
        job = Job(
            tenant_id=world["tenant_id"],
            course_node_id=world["root_id"],
            job_type="homework_processing",
            subject_type="homework_submission",
            subject_id=submission.id,
            input_params={"submission_id": str(submission.id)},
            status="queued",
        )
        session.add(job)
        await session.commit()
        world["submissions"].append(submission.id)
        return _Submitted(submission.id, job.id)


class _Walk:
    """One run of the ARQ task with its doubles, and what it left behind."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3_client: S3Client,
        world: dict[str, Any],
    ) -> None:
        self.session_factory = session_factory
        self.s3_client = s3_client
        self.world = world
        self.router = _RouterDouble(session_factory)
        self.config = _config()

    async def run(self, files: dict[str, bytes]) -> _Outcome:
        """Submit ``files`` and run the task on the router's recorded answers."""
        self.router.answer_with(_answers())
        port = _PortDouble()
        webhook = AsyncMock(return_value=True)
        submitted = await _submit(
            self.session_factory, self.s3_client, self.world, files
        )
        ctx = {
            "session_factory": self.session_factory,
            "stage_router": self.router,
            "s3_client": self.s3_client,
            "job_try": 1,
        }
        with (
            patch(
                "course_supporter.homework.path_runner.get_path_config",
                return_value=self.config,
            ),
            patch(
                "course_supporter.homework.path_runner.build_funds_port",
                return_value=port,
            ),
            patch(
                "course_supporter.homework.criteria_evaluation."
                "build_criteria_list_service",
                return_value=_ListSource(self.world["in_force"]),
            ),
            patch("course_supporter.homework.webhook.deliver_webhook", new=webhook),
        ):
            await arq_process_homework(
                ctx, str(submitted.job_id), str(submitted.submission_id)
            )
        async with self.session_factory() as session:
            submission = await session.get(HomeworkSubmission, submitted.submission_id)
        assert submission is not None
        payloads = [call.kwargs["payload"] for call in webhook.await_args_list]
        return _Outcome(
            submission=submission,
            job_id=submitted.job_id,
            port=port,
            reviewed=[p for p in payloads if isinstance(p, WebhookReviewedPayload)],
            deliveries=len(payloads),
        )


@dataclass(frozen=True)
class _Outcome:
    submission: HomeworkSubmission
    job_id: uuid.UUID
    port: _PortDouble
    reviewed: list[WebhookReviewedPayload]
    deliveries: int

    @property
    def structure(self) -> ReviewStructureV1:
        return ReviewStructureV1.model_validate(self.submission.review_result)


@pytest.fixture()
def walk(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    world: dict[str, Any],
) -> _Walk:
    return _Walk(session_factory, s3_client, world)


# ── What a run left ─────────────────────────────────────────────────────────


async def _rows(
    session_factory: async_sessionmaker[AsyncSession], submission_id: uuid.UUID
) -> dict[str, SubmissionCriterionVerdict]:
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(SubmissionCriterionVerdict).where(
                    SubmissionCriterionVerdict.submission_id == submission_id
                )
            )
        ).scalars()
        return {row.item_id: row for row in rows}


async def _register(
    session_factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> list[ExternalServiceCall]:
    """The register's rows of a job, in order — the stages' calls, and no row
    of the funds port or of the review's metrics, which record no call."""
    async with session_factory() as session:
        return list(
            (
                await session.execute(
                    select(ExternalServiceCall)
                    .where(
                        ExternalServiceCall.job_id == job_id,
                        ExternalServiceCall.action.not_in(
                            (FUNDS_PORT_ACTION, REVIEW_METRICS_ACTION)
                        ),
                    )
                    .order_by(ExternalServiceCall.created_at.asc())
                )
            ).scalars()
        )


def _section(prompt: str, tag: str) -> str:
    """What a prompt carries between ``<tag>`` and ``</tag>``."""
    return prompt.split(f"\n<{tag}>\n", 1)[1].split(f"\n</{tag}>\n", 1)[0]


_FROM_BASE = RepeatItem("c2.p1", RepeatReason.QUOTE_FROM_BASE).feedback


# ── The runs ────────────────────────────────────────────────────────────────


class TestAFirstSubmissionOfAProject:
    """Run 1: from the student's archive to the delivered review."""

    async def test_the_safety_stage_reads_the_students_files_alone(
        self, walk: _Walk
    ) -> None:
        await walk.run(PROJECT)

        [request] = walk.router.asked(SAFETY)
        read = request.variables["submission_text"]
        # Two files, each under the frame of an archive's member: the changed
        # file and the new one, whole — and nothing of ours or of the author's.
        assert read == f"--- app.py ---\n{STUDENTS_APP}\n--- stats.py ---\n{STATS}"
        assert read in request.user
        assert "@@MENTOR_CTX@@" not in request.user
        assert "TRUSTED" not in request.user
        assert UTILS not in request.user  # the author's neighbor
        assert "README" not in request.user  # in the trusted tree only
        assert SECRET not in request.user  # a file the doors never open

    async def test_the_evaluation_reads_the_projects_prompt_over_its_frames(
        self, walk: _Walk
    ) -> None:
        await walk.run(PROJECT)

        first, again = walk.router.asked(PROJECT_CRITERIA_EVALUATION)
        # The project's own prompt (K4), not the text types'.
        assert "`NEIGHBOR` — a file of the base" in first.system
        assert "`NEIGHBOR`" in again.system
        # The delta context in its frames (K1): the trusted block, the changed
        # file whole, the new file, the author's neighbor.
        work = _section(first.user, "student_submission")
        assert "@@MENTOR_CTX@@ TRUSTED" in work
        assert "type=CHANGED-FULL path=app.py" in work
        assert "type=NEW path=stats.py" in work
        assert "type=NEIGHBOR path=utils.py" in work
        assert SECRET not in work
        # The marks of the author's lines in the whole changed file (K5): the
        # student's line is "+", the author's line is left as it was.
        assert "\n+    print(len(lines), mean_length(lines))\n" in work
        assert "\n     lines = read_lines(path)\n" in work
        assert work == first.variables["submission_text"]
        # The list in force reached the prompt.
        assert [c["id"] for c in first.variables["criteria"]] == ["c1", "c2", "c3"]

    async def test_the_verdicts_the_quote_of_the_base_and_the_place(
        self, walk: _Walk, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        outcome = await walk.run(PROJECT)

        # A quote of the base: asked about once more, alone, with its reason.
        first, again = walk.router.asked(PROJECT_CRITERIA_EVALUATION)
        assert '["c2.p1"]' in again.user
        [note] = json.loads(_section(again.user, "previous_problems"))
        assert note["problem"] == _FROM_BASE
        assert '["c2.p1"]' not in first.user

        rows = await _rows(session_factory, outcome.submission.id)
        assert {item: row.verdict for item, row in rows.items()} == {
            "c1": VerdictValue.MET,
            "c2": VerdictValue.NOT_MET,
            "c2.p1": VerdictValue.NOT_MET,
            "c2.p2": VerdictValue.MET,
            "c3": VerdictValue.NOT_MET,
        }
        # A quote of the student's NEW file stands: the file and the line.
        c1 = rows["c1"]
        assert (c1.quote_file, c1.quote_line_start, c1.quote_line_end) == (
            "stats.py",
            4,
            4,
        )
        assert (c1.retried, c1.quote_from_base) == (False, False)
        assert (rows["c2.p2"].quote_file, rows["c2.p2"].quote_line_start) == (
            "app.py",
            8,
        )
        # The same quote of the base twice: not met, flagged for the author.
        p1 = rows["c2.p1"]
        assert (p1.model_verdict, p1.quote_from_base, p1.retried) == (
            VerdictValue.MET,
            True,
            True,
        )
        assert rows["c2"].quote_from_base is True
        # A "not met" on a line of the student's file keeps its place.
        c3 = rows["c3"]
        assert (c3.quote_file, c3.quote_line_start) == ("stats.py", 3)
        assert c3.missing == _MISSING
        assert not any(row.safeguard_fired for row in rows.values())

    async def test_the_course_link_of_the_unmet_criterion_stands_in_the_review(
        self, walk: _Walk, world: dict[str, Any]
    ) -> None:
        outcome = await walk.run(PROJECT)

        # The search found the place the course teaches c3's concept, and the
        # explanation was shown it under its local name (K5 of task 10).
        [explanation] = walk.router.asked(PROJECT_REVIEW_EXPLANATION)
        shown = {
            c["criterion"]: c["links"] for c in explanation.variables["course_links"]
        }
        assert shown["c2"] == []
        [place] = shown["c3"]
        assert (place["id"], place["material"], place["excerpt"]) == (
            "c3.l1",
            _TITLE,
            _TAUGHT,
        )
        # The flag of the base is shown to the explanation where it stands.
        c2 = next(
            c
            for c in json.loads(_section(explanation.user, "verdicts"))
            if c["id"] == "c2"
        )
        assert c2["points"][0]["quote_from_base"] is True

        # The review links it: the remark of c3, the lightest, last.
        c2_remark, c3_remark = outcome.structure.new_remarks
        assert c2_remark.read == []
        assert c3_remark.read == [
            Reference(
                title=_TITLE,
                url=None,
                position=Position(kind="paragraph", value="2"),
                excerpt=_TAUGHT,
                segment_id=str(world["segment_id"]),
                later_in_course=False,
            )
        ]
        assert (
            f"- **Де почитати:** {_TITLE} (абзац 2)"
            in (outcome.submission.review_markdown or "").splitlines()
        )

    async def test_the_review_the_verdict_the_rule_the_place_and_the_score(
        self, walk: _Walk
    ) -> None:
        outcome = await walk.run(PROJECT)

        submission = outcome.submission
        # The score by hand: c1 (3) of c1 + c2 + c3 (3 + 2 + 1) = 50 %.
        assert submission.score == 50
        structure = outcome.structure
        assert structure.language == "ukr"
        assert structure.unchanged_from_attempt is None
        assert structure.verdict is not None
        assert structure.verdict.passed is False
        assert structure.verdict.why == _WHY
        # The author's list: its own "must" is met, and 50 is under the
        # author's threshold of 70 (the pass rule of 2026-11-12 and 2026-11-17).
        assert structure.verdict.rule == PassRule(
            basis="author_musts_below_threshold", score=50, threshold=70
        )
        # The remarks, heaviest first; the place of the problem above its
        # remark when the student's own line is where it stands.
        c2_remark, c3_remark = structure.new_remarks
        assert c2_remark.position is None
        assert c3_remark.position == _PLACE

        markdown = submission.review_markdown or ""
        assert markdown == assemble_review(structure)
        assert "Не зараховано" in markdown
        assert (
            "*файл stats.py:3*\n\n- **Що не так:** Порожній файл дає нуль" in markdown
        )
        # Only c3 stands on a line of the student's file.
        assert markdown.count("*файл ") == 1

    async def test_the_attempt_its_fingerprint_and_the_evaluators_version(
        self, walk: _Walk
    ) -> None:
        outcome = await walk.run(PROJECT)

        submission = outcome.submission
        assert submission.attempt_no == 1
        assert submission.fingerprint is not None
        assert submission.fingerprint == submission.snapshot_hash
        receipt = submission.attempt_receipt
        assert receipt is not None
        assert receipt["fingerprint"] == submission.fingerprint
        assert receipt["path"] == ServedBy.NEW_PATH.value
        assert receipt["test_mode"] is True
        assert DEDUP_RECEIPT_KEY not in receipt
        # The evaluator that wrote the review is the project's, not the text
        # types' (their stages are others').
        version = evaluator_version(AssignmentType.PROJECT, walk.config)
        assert version is not None
        assert receipt[EVALUATOR_VERSION_RECEIPT_KEY] == version
        assert version != evaluator_version(AssignmentType.TASK, walk.config)

    async def test_delivered_through_the_webhook_with_the_stored_review(
        self, walk: _Walk
    ) -> None:
        outcome = await walk.run(PROJECT)

        assert outcome.submission.status == "delivered", (
            outcome.submission.error_message
        )
        [payload] = outcome.reviewed
        assert payload.structure == outcome.structure
        assert payload.review.score == 50
        assert payload.review.passed is False
        assert payload.review.review_text == outcome.submission.review_markdown
        assert outcome.deliveries == 1
        assert outcome.port.released == [SubmissionOutcome.COMPLETED]

    async def test_every_model_stage_left_its_row_within_its_ceiling(
        self, walk: _Walk, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        outcome = await walk.run(PROJECT)

        # Each stage of the path asked its router, the code stage did not, and
        # the register holds one row per call — the evaluation's repeat too.
        assert walk.router.calls == [
            SAFETY,
            ATTEMPT_CLASSIFIER,
            PROJECT_CRITERIA_EVALUATION,
            PROJECT_CRITERIA_EVALUATION,
            PROJECT_REVIEW_EXPLANATION,
        ]
        rows = await _register(session_factory, outcome.job_id)
        assert [row.action for row in rows] == walk.router.calls
        assert all(row.cost_usd is not None and row.cost_usd > 0 for row in rows)

        spent: dict[str, float] = {}
        for row in rows:
            spent[row.action] = spent.get(row.action, 0.0) + float(row.cost_usd or 0)
        stages = walk.config.stages
        for name, cost in spent.items():
            stage = stages[name]
            assert isinstance(stage, ModelStage)
            assert cost <= stage.ceilings.money_usd, name
        assert sum(spent.values()) <= sum(
            stage.ceilings.money_usd
            for name in _PATH
            if isinstance(stage := stages[name], ModelStage)
        )
        # What the funds port was told, stage by stage, from the same rows.
        assert outcome.port.accounted == [
            pytest.approx(spent.get(name, 0.0)) for name in _PATH
        ]


class TestTheSameWorkAgain:
    """Run 2: the same archive by the same student — the review is given again."""

    async def test_the_earlier_review_is_repeated_and_nothing_is_paid_for(
        self, walk: _Walk, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        first = await walk.run(PROJECT)
        assert first.submission.status == "delivered"
        assert same_work_reaches_the_new_path("project", walk.config) is True

        second = await walk.run(PROJECT)

        # No stage, no funds question, no register row, no verdict row.
        assert walk.router.calls == []
        assert second.port.reserved == []
        assert second.port.accounted == []
        async with session_factory() as session:
            written = (
                await session.execute(
                    select(ExternalServiceCall).where(
                        ExternalServiceCall.job_id == second.job_id
                    )
                )
            ).all()
        assert written == []
        assert await _rows(session_factory, second.submission.id) == {}

        # The copy: the first review, marked as unchanged from attempt 1.
        assert second.submission.attempt_no == 2
        assert second.structure.unchanged_from_attempt == 1
        assert second.structure == first.structure.model_copy(
            update={"unchanged_from_attempt": 1}
        )
        assert second.submission.score == first.submission.score == 50
        assert second.submission.review_markdown == assemble_review(second.structure)
        assert second.submission.review_markdown != first.submission.review_markdown
        receipt = second.submission.attempt_receipt
        assert receipt is not None
        assert receipt[DEDUP_RECEIPT_KEY] == 1
        assert (
            receipt[EVALUATOR_VERSION_RECEIPT_KEY]
            == (first.submission.attempt_receipt or {})[EVALUATOR_VERSION_RECEIPT_KEY]
        )
        assert (
            receipt["fingerprint"]
            == (first.submission.attempt_receipt or {})["fingerprint"]
        )
        assert receipt["previous_submission_id"] == str(first.submission.id)

        # It reached the student as any review does.
        assert second.submission.status == "delivered"
        [payload] = second.reviewed
        assert payload.structure == second.structure
        assert payload.review.score == 50


class TestAChangedWorkAgain:
    """Run 3: another line of the changed file — a new, full review."""

    async def test_a_new_full_review_and_the_second_attempt(
        self, walk: _Walk, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        first = await walk.run(PROJECT)
        reworded = STUDENTS_APP.replace("as the starting project does", "like the base")
        assert reworded != STUDENTS_APP

        second = await walk.run({**PROJECT, "app.py": reworded.encode()})

        # Every stage of the path was called, the work in its request is the
        # changed one, and the register holds the rows of a whole review.
        assert walk.router.calls == [
            SAFETY,
            ATTEMPT_CLASSIFIER,
            PROJECT_CRITERIA_EVALUATION,
            PROJECT_CRITERIA_EVALUATION,
            PROJECT_REVIEW_EXPLANATION,
        ]
        assert "like the base" in walk.router.asked(SAFETY)[0].user
        assert "like the base" in walk.router.asked(PROJECT_CRITERIA_EVALUATION)[0].user
        assert [
            row.action for row in await _register(session_factory, second.job_id)
        ] == (walk.router.calls)
        assert second.port.reserved != []

        submission = second.submission
        assert submission.attempt_no == 2
        assert submission.status == "delivered"
        assert submission.fingerprint != first.submission.fingerprint
        receipt = submission.attempt_receipt
        assert receipt is not None
        assert DEDUP_RECEIPT_KEY not in receipt
        assert receipt["previous_submission_id"] == str(first.submission.id)
        assert receipt["task_version_changed"] is False
        assert (
            receipt[EVALUATOR_VERSION_RECEIPT_KEY]
            == (first.submission.attempt_receipt or {})[EVALUATOR_VERSION_RECEIPT_KEY]
        )
        # A full review, written again: not a copy, with its own rows.
        assert second.structure.unchanged_from_attempt is None
        assert submission.score == 50
        assert set(await _rows(session_factory, submission.id)) == {
            "c1",
            "c2",
            "c2.p1",
            "c2.p2",
            "c3",
        }
        # A second attempt at the same version of the task (task 12a): the
        # remarks of the first that still stand are open, none is new — the
        # copy between the two has no rows and is passed over.
        assert second.structure.new_remarks == []
        assert second.structure.open[1].position == _PLACE
        # Each remark names its criterion: a reply is tied to it by that.
        assert [remark.item_id for remark in second.structure.open] == ["c2", "c3"]
        [payload] = second.reviewed
        assert payload.structure == second.structure


class TestTheConfigurationOfTheWalk:
    """What the runs above stand on: the shipped stages, the project switched."""

    def test_it_differs_from_the_shipped_file_in_the_projects_entry_alone(
        self,
    ) -> None:
        shipped, config = _shipped(), _config()

        assert config.stages == shipped.stages
        others = {t for t in config.task_types if t is not AssignmentType.PROJECT}
        assert {t: config.task_types[t] for t in others} == {
            t: shipped.task_types[t] for t in others
        }
        project = config.task_types[AssignmentType.PROJECT]
        assert project.served_by is ServedBy.NEW_PATH
        assert in_test_mode(AssignmentType.PROJECT, config) is True
        assert shipped.task_types[AssignmentType.PROJECT].served_by is (
            ServedBy.TODAYS_MENTOR
        )

    def test_the_input_budget_of_the_marks_is_the_same_on_the_new_path(
        self,
    ) -> None:
        """The marks of the author's lines are held to the budget of
        ``project_path_budget_chars``, which reads the file the process cached —
        today the shipped one. Switching the project does not move it."""
        registry = load_registry(get_settings().external_services_path)

        assert path_context_budget_chars(
            AssignmentType.PROJECT, _config(), registry
        ) == path_context_budget_chars(AssignmentType.PROJECT, _shipped(), registry)
