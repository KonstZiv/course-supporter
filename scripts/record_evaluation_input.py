"""Record the input of one ``criteria_evaluation`` request from production.

Reads, never writes (2026-11-13). For one submission it assembles the prompt
variables of the stage's FIRST request (not the repeat about the verdicts that
could not stand) from the same sources, through the same code, as the stage:

* the criteria list in force — only a STORED one
  (``criteria_list_service.load_in_force``). None stored: the script stops; it
  composes nothing and calls no model;
* the task's title, description and text (``task_context.load_task_context``);
* the work as the doors read it — the same functions the body calls
  (``path_runner.door_languages``, ``doors.screen_student_note``,
  ``path_runner.read_text_work``), on the file read from S3;
* the course's language (``criteria_evaluation.course_language``);
* rendered into the prompt's variables by ``criteria_evaluator.render_context``.

The database is read in one ``READ ONLY`` transaction; S3 is only read (the
download is a temporary file, deleted at once). Serves the types the stage
serves — ``task`` and ``short_task``; a project or a test is refused.

Usage::

    uv run python scripts/record_evaluation_input.py --submission <uuid> \\
        --out recorded.json
    uv run python scripts/record_evaluation_input.py --submission <uuid> \\
        --out recorded.json --check-hash

``--out`` is the JSON ``scripts/measure_determinism.py --input`` takes.

``--check-hash`` also builds the request the router would send on the stage's
first rung WITHOUT the seed (``deterministic: false`` — what production sent
before 2026-11-13), takes its ``input_hash`` and compares it with the register
rows of that rung for this submission (``external_service_calls`` of the
submission's jobs, ``action = criteria_evaluation``). ``matched: true`` — the
recorded input is the input production sent. The result is printed and
written beside ``--out`` as ``<name>.check.json``, with each row's hash and
prompt hash, so a mismatch shows whether the prompt or the input moved.

Exit codes: 0 — written; 2 — refused (the reason is printed), nothing written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.agents.criteria_evaluator import (
    RESPONSE_SCHEMA,
    EvaluationInput,
    render_context,
)
from course_supporter.homework.criteria_evaluation import course_language
from course_supporter.homework.criteria_list_service import load_in_force
from course_supporter.homework.criteria_verdicts import judged_items
from course_supporter.homework.doors import screen_student_note
from course_supporter.homework.path_config import ModelStage
from course_supporter.homework.path_runner import door_languages, read_text_work
from course_supporter.homework.path_stages import router_stage_config
from course_supporter.homework.task_context import load_task_context
from course_supporter.llm.error_categories import ErrorCategory
from course_supporter.llm.factory import PROVIDER_CONFIGS
from course_supporter.llm.input_hash import hash_attempt_input
from course_supporter.llm.ladder_config import LadderConfig
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.providers import PROVIDER_REGISTRY
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.registry import ModelRegistryConfig
from course_supporter.llm.schemas import LLMRequest, LLMResponse
from course_supporter.llm.stage_router import StageRouter
from course_supporter.models.source import AssignmentType
from course_supporter.security.exceptions import SecurityRejectedError
from course_supporter.storage.orm import ExternalServiceCall, HomeworkSubmission, Job
from course_supporter.storage.s3 import S3Client

STAGE = "criteria_evaluation"
SERVED_TYPES = frozenset({AssignmentType.TASK.value, AssignmentType.SHORT_TASK.value})
# The subject type of a submission's jobs (``jobs/job_type.py``).
SUBMISSION_SUBJECT = "homework_submission"


class RecordRefusedError(Exception):
    """The input cannot be recorded; the message says why. Nothing is written."""


@dataclass(frozen=True)
class Recorded:
    """One request's prompt variables, and where its parts came from."""

    variables: dict[str, Any]
    criteria_layer: str
    criteria_source_id: str
    # The doors name the work by the student's file name; without one, by the
    # name of the downloaded file — a temporary name that differs per download,
    # so the text (and the hash) cannot match production's then.
    filename_was_temporary: bool


@dataclass(frozen=True)
class RegisterRow:
    """An attempt row of the first rung, as the check reads it."""

    id: str
    created_at: str
    outcome: str | None
    input_hash: str | None
    prompt_hash: str | None
    matched: bool = False


@dataclass
class HashCheck:
    """``input_hash`` of the recorded request against the register."""

    stage: str
    provider: str
    model: str
    input_hash: str
    prompt_hash: str
    matched: bool
    rows: list[RegisterRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


async def record(
    session: AsyncSession, s3: S3Client, submission_id: uuid.UUID
) -> Recorded:
    """The prompt variables of the stage's first request for one submission.

    Raises:
        RecordRefusedError: No such submission, a type the stage does not
            serve, no stored criteria list, or the doors refuse the work.
    """
    submission = await session.get(HomeworkSubmission, submission_id)
    if submission is None:
        msg = f"no submission {submission_id}"
        raise RecordRefusedError(msg)
    languages = await door_languages(session, submission)
    task = languages.task
    if task is None or task.task_type not in SERVED_TYPES:
        task_type = task.task_type if task is not None else None
        msg = (
            f"the task's type is {task_type!r}; {STAGE} serves "
            f"{sorted(SERVED_TYPES)} only"
        )
        raise RecordRefusedError(msg)

    # Before anything is downloaded: a stored list or nothing. The stage would
    # compose one on a miss (a model call); this script never does.
    listed = await load_in_force(session, submission.authored_document_id)
    if listed is None:
        msg = (
            "no stored criteria list in force for the task's current version; "
            "nothing is composed and no model is called"
        )
        raise RecordRefusedError(msg)

    try:
        note_flags = screen_student_note(submission.student_note)
    except SecurityRejectedError as exc:
        msg = f"the doors refuse the student's note: {exc.category.value}"
        raise RecordRefusedError(msg) from exc
    key = s3.extract_key(submission.file_url)
    if key is None:
        msg = f"cannot extract an S3 key from {submission.file_url}"
        raise RecordRefusedError(msg)
    path = await s3.download_file(key)
    try:
        reading = read_text_work(
            filename=submission.original_filename or path.name,
            file_bytes=path.read_bytes(),
            languages=languages.verify,
            note_flags=note_flags,
        )
    except SecurityRejectedError as exc:
        msg = f"the doors refuse the work: {exc.category.value}"
        raise RecordRefusedError(msg) from exc
    finally:
        path.unlink(missing_ok=True)

    title, description, task_text = await load_task_context(
        session, submission.authored_document_id
    )
    shown = EvaluationInput(
        task_title=title,
        task_description=description,
        task_text=task_text,
        criteria=listed.criteria,
        submission_text=reading.text,
        language=await course_language(session, submission),
    )
    variables = render_context(shown, expected=judged_items(listed.criteria), repeat=())
    return Recorded(
        # Through JSON once, so the variables are exactly what the file holds
        # and what ``measure_determinism.py`` will render.
        variables=json.loads(json.dumps(variables, ensure_ascii=False)),
        criteria_layer=listed.layer.value,
        criteria_source_id=str(listed.source_id),
        filename_was_temporary=not submission.original_filename,
    )


def _connector(name: str, model: str) -> LLMProvider:
    """The production connector class of a rung, built without a key.

    Only asked what it can send (``seed_supported``, ``check_request``);
    never called, so the key is a placeholder and nothing leaves the machine.
    """
    kwargs: dict[str, Any] = {"api_keys": ["unused"], "default_model": model}
    config = PROVIDER_CONFIGS.get(name)
    if config is not None:
        kwargs.update(config.extra_kwargs)
    return PROVIDER_REGISTRY[name](**kwargs)


class _Capture(LLMProvider):
    """The rung's connector for every question; keeps the request, sends nothing."""

    def __init__(self, inner: LLMProvider) -> None:
        super().__init__()
        self._inner = inner
        self.provider_name = inner.provider_name
        self.requests: list[LLMRequest] = []

    def seed_supported(self, request: LLMRequest) -> bool:
        return self._inner.seed_supported(request)

    def check_request(self, request: LLMRequest) -> None:
        self._inner.check_request(request)

    def classify_error(self, exc: Exception) -> ErrorCategory:
        return self._inner.classify_error(exc)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            content="{}", provider=self.provider_name, model_id=request.model
        )


async def first_rung_request(
    variables: dict[str, Any],
    stage: ModelStage,
    registry: ModelRegistryConfig,
) -> tuple[str, LLMRequest]:
    """``(provider, request)`` the router builds on the first rung, no seed.

    The real router builds it (prompt, schema mode, output pin) and the rung's
    real connector class answers what it can send — the seed it would take is
    left out only by ``deterministic: false``. The request is kept, not sent.
    """
    config = router_stage_config(stage)
    first = config.ladder[0]
    capture = _Capture(_connector(first.provider, first.model))
    router = StageRouter(LadderConfig(), {first.provider: capture}, registry=registry)
    await router.execute_stage(
        config.model_copy(update={"ladder": [first], "deterministic": False}),
        STAGE,
        expects_json=True,
        response_schema=RESPONSE_SCHEMA,
        stop_on_output_ceiling=True,
        **variables,
    )
    return first.provider, capture.requests[0]


async def register_rows(
    session: AsyncSession, submission_id: uuid.UUID, *, provider: str, model: str
) -> list[RegisterRow]:
    """The attempt rows of ``provider``/``model`` on the stage, oldest first.

    Attempt rows only: a trace row (skipped or abandoned rung) has
    ``success`` NULL and carries no input hash.
    """
    jobs = select(Job.id).where(
        Job.subject_type == SUBMISSION_SUBJECT, Job.subject_id == submission_id
    )
    esc = ExternalServiceCall
    result = await session.execute(
        select(esc)
        .where(
            esc.job_id.in_(jobs),
            esc.action == STAGE,
            esc.provider == provider,
            esc.model_id == model,
            esc.success.is_not(None),
        )
        .order_by(esc.created_at)
    )
    return [
        RegisterRow(
            id=str(row.id),
            created_at=row.created_at.isoformat(),
            outcome=row.outcome,
            input_hash=row.input_hash,
            prompt_hash=row.prompt_hash,
        )
        for row in result.scalars()
    ]


def compare(
    request: LLMRequest,
    *,
    provider: str,
    prompt_hash: str,
    rows: Sequence[RegisterRow],
    recorded: Recorded,
) -> HashCheck:
    """Mark each row whose ``input_hash`` is the recorded request's."""
    input_hash = hash_attempt_input(request, provider=provider)
    marked = [replace(row, matched=row.input_hash == input_hash) for row in rows]
    notes: list[str] = []
    if not marked:
        notes.append("no attempt row of the first rung for this submission")
    if any(row.prompt_hash != prompt_hash for row in marked):
        notes.append("a row's prompt_hash is not today's: the prompt moved")
    if recorded.filename_was_temporary:
        notes.append(
            "the submission has no original file name: the doors named the work "
            "by a temporary name, so its text cannot match production's"
        )
    return HashCheck(
        stage=STAGE,
        provider=provider,
        model=request.model,
        input_hash=input_hash,
        prompt_hash=prompt_hash,
        matched=any(row.matched for row in marked),
        rows=marked,
        notes=notes,
    )


def write_input(recorded: Recorded, out: Path) -> None:
    """The JSON ``scripts/measure_determinism.py --input`` takes."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(recorded.variables, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def check_path(out: Path) -> Path:
    """Where ``--check-hash`` writes its result: ``<name>.check.json``."""
    return out.with_name(f"{out.stem}.check.json")


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--submission", type=uuid.UUID, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--check-hash", action="store_true")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    from course_supporter.config import get_settings
    from course_supporter.homework.path_config import load_path_config, model_stages
    from course_supporter.llm.registry import load_registry

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        async with AsyncSession(engine) as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            async with S3Client(
                endpoint_url=settings.s3_endpoint,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key.get_secret_value(),
                bucket=settings.s3_bucket,
            ) as s3:
                try:
                    recorded = await record(session, s3, args.submission)
                except RecordRefusedError as refusal:
                    sys.stderr.write(f"refused: {refusal}\n")
                    return 2
            write_input(recorded, args.out)
            sys.stdout.write(
                f"written {args.out} (criteria: {recorded.criteria_layer} "
                f"{recorded.criteria_source_id})\n"
            )
            if not args.check_hash:
                return 0
            stage = model_stages(
                load_path_config(settings.submission_paths_config_path)
            )[STAGE]
            provider, request = await first_rung_request(
                recorded.variables,
                stage,
                load_registry(settings.external_services_path),
            )
            rows = await register_rows(
                session, args.submission, provider=provider, model=request.model
            )
            check = compare(
                request,
                provider=provider,
                prompt_hash=load_prompt(stage.prompt_ref).content_hash(),
                rows=rows,
                recorded=recorded,
            )
    finally:
        await engine.dispose()
    document = json.dumps(asdict(check), ensure_ascii=False, indent=2) + "\n"
    check_path(args.out).write_text(document, encoding="utf-8")
    sys.stdout.write(document)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Record one submission's evaluation input (reads production, writes a file)."""
    return asyncio.run(_run(_parse(argv)))


if __name__ == "__main__":
    sys.exit(main())
