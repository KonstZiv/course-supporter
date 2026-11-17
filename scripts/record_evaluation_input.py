"""Record the input of one ``criteria_evaluation`` request from production.

Reads, never writes (2026-11-13). For one submission it assembles the prompt
variables of the stage's FIRST request (not the repeat about the verdicts that
could not stand) from the same sources, through the same code, as the stage:

* the criteria list the submission's verdicts were judged against — by the
  address its verdict rows carry (``criteria_layer`` + ``criteria_source_id``,
  ``judged_list.read_judged_list``), as the explanation's rebuild reads it
  (``homework/explanation_input_rebuild.py``): an author's edit since the run
  does not move it. A submission without verdict rows (the stage never wrote
  them) takes the list in force now, as before — only a STORED one
  (``criteria_list_service.load_in_force``); the output says which it was.
  Neither: the script stops; it composes nothing and calls no model;
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

``--check-hash`` also builds the request the router sends on the stage's
first rung and compares its ``input_hash`` with the register rows of that rung
for this submission (``external_service_calls`` of the submission's jobs,
``action = criteria_evaluation``). The seed and the temperature are part of
that hash, so each row is compared with the request built with the seed and
the temperature production sent for THAT row (:func:`sent_with`):

* a row written since revision ``esc_request_traces`` (its ``request_kind`` is
  set) says them itself — its ``seed`` and ``temperature`` columns;
* an older row is dated. Before :data:`SEED_SENT_SINCE` — the merge of the
  router's seed (``llm/reproducibility.py``) into main, 2026-11-13 20:23:21
  UTC — no seed; from then on the stage's fixed seed, where the rung's
  connector takes one (``LLMProvider.seed_supported``). The temperature is 0
  on both sides: the request's own default before, set explicitly after. The
  deploy was by hand, after the merge: a row of the hours between can have
  gone without a seed, and its rule is printed beside it.

``matched: true`` — the recorded input is the input production sent. The
result is printed and written beside ``--out`` as ``<name>.check.json``, with
each row's hash, the rule its seed came from and its prompt hash, so a
mismatch shows whether the prompt, the input or the seed moved.

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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.agents.criteria_evaluator import (
    RESPONSE_SCHEMA,
    EvaluationInput,
    render_context,
)
from course_supporter.homework.criteria_evaluation import course_language
from course_supporter.homework.criteria_list_service import (
    CriteriaInForce,
    load_in_force,
)
from course_supporter.homework.criteria_verdicts import judged_items
from course_supporter.homework.doors import screen_student_note
from course_supporter.homework.judged_list import judged_layer, read_judged_list
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
from course_supporter.llm.reproducibility import (
    DETERMINISTIC_TEMPERATURE,
    REPRODUCIBILITY_SEED,
)
from course_supporter.llm.schemas import LLMRequest, LLMResponse
from course_supporter.llm.stage_router import StageRouter
from course_supporter.models.source import AssignmentType
from course_supporter.security.exceptions import SecurityRejectedError
from course_supporter.storage.orm import (
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    SubmissionCriterionVerdict,
)
from course_supporter.storage.s3 import S3Client
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)

STAGE = "criteria_evaluation"
SERVED_TYPES = frozenset({AssignmentType.TASK.value, AssignmentType.SHORT_TASK.value})
# The subject type of a submission's jobs (``jobs/job_type.py``).
SUBMISSION_SUBJECT = "homework_submission"
# When the router began to send the stage's seed: the merge of
# ``llm/reproducibility.py`` into main (PR 661). See the module docstring.
SEED_SENT_SINCE: Final = datetime(2026, 11, 13, 20, 23, 21, tzinfo=UTC)
# Where a list came from (``Recorded.criteria_from``).
FROM_VERDICT_ROWS = "verdict_rows"
FROM_IN_FORCE = "in_force"
# The rule a row's seed and temperature came from (``RegisterRow.sent_rule``).
RULE_COLUMNS = "columns"
RULE_BEFORE_SEED = "dated_before_seed"
RULE_SINCE_SEED = "dated_since_seed"


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
    # FROM_VERDICT_ROWS — the list the verdicts were judged against;
    # FROM_IN_FORCE — the submission has no verdict rows, the list in force now.
    criteria_from: str = FROM_VERDICT_ROWS


@dataclass(frozen=True)
class RegisterRow:
    """An attempt row of the first rung, as the check reads it.

    ``request_kind``, ``seed`` and ``temperature`` are the row's own columns
    (``None`` on a row written before they existed); ``sent_rule``,
    ``rebuilt_hash`` and ``matched`` are filled by :func:`compare`.
    """

    id: str
    created_at: str
    outcome: str | None
    input_hash: str | None
    prompt_hash: str | None
    request_kind: str | None = None
    seed: int | None = None
    temperature: float | None = None
    sent_rule: str = ""
    rebuilt_hash: str = ""
    matched: bool = False


@dataclass
class HashCheck:
    """``input_hash`` of the recorded request against the register."""

    stage: str
    provider: str
    model: str
    prompt_hash: str
    matched: bool
    rows: list[RegisterRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SentWith:
    """The seed and the temperature one row's request was sent with."""

    seed: int | None
    temperature: float
    rule: str


async def record(
    session: AsyncSession, s3: S3Client, submission_id: uuid.UUID
) -> Recorded:
    """The prompt variables of the stage's first request for one submission.

    Raises:
        RecordRefusedError: No such submission, a type the stage does not
            serve, no list (neither by the verdict rows' address nor stored in
            force), or the doors refuse the work.
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

    # Before anything is downloaded: the list the verdicts were judged
    # against, else a stored list in force, else nothing. The stage would
    # compose one on a miss (a model call); this script never does.
    listed = await judged_criteria(session, submission)
    criteria_from = FROM_VERDICT_ROWS
    if listed is None:
        listed = await load_in_force(session, submission.authored_document_id)
        criteria_from = FROM_IN_FORCE
    if listed is None:
        msg = (
            "no verdict rows and no stored criteria list in force for the "
            "task's current version; nothing is composed and no model is called"
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
        criteria_from=criteria_from,
    )


async def verdict_rows(
    session: AsyncSession, submission: HomeworkSubmission
) -> list[SubmissionCriterionVerdict]:
    """The submission's verdict rows, as the stage wrote them."""
    return await SubmissionVerdictRepository(session).list_for_submission(
        tenant_id=submission.tenant_id, submission_id=submission.id
    )


async def judged_criteria(
    session: AsyncSession, submission: HomeworkSubmission
) -> CriteriaInForce | None:
    """The list the submission's verdicts were judged against, by their address.

    ``None`` — the submission has no verdict rows. The same read the
    explanation's rebuild makes (``judged_list.read_judged_list``), so the
    list is the one the stage was handed, whatever is in force now.

    Raises:
        RecordRefusedError: The rows name more than one list, or a list that
            is not this task's.
    """
    rows = await verdict_rows(session, submission)
    if not rows:
        return None
    try:
        criteria = await read_judged_list(session, submission, rows)
        layer = judged_layer(rows)
    except ValueError as exc:
        raise RecordRefusedError(str(exc)) from exc
    return CriteriaInForce(
        criteria=criteria, layer=layer, source_id=rows[0].criteria_source_id
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
    left out only by ``deterministic: false``; :func:`compare` puts each row's
    own seed and temperature back. The request is kept, not sent.
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
    ``success`` NULL and carries no input hash. Each with the seed and the
    temperature it records, when it records them.
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
            request_kind=row.request_kind,
            seed=row.seed,
            temperature=row.temperature,
        )
        for row in result.scalars()
    ]


def seed_supported(provider: str, request: LLMRequest) -> bool:
    """Whether the rung's production connector sends a seed with ``request``."""
    return _connector(provider, request.model).seed_supported(request)


def sent_with(row: RegisterRow, *, seed_supported: bool) -> SentWith:
    """The seed and the temperature production sent with ``row``'s request.

    The rule of the module docstring: the row's own columns when it has them
    (``request_kind`` set: written since revision ``esc_request_traces``),
    otherwise by the row's date against :data:`SEED_SENT_SINCE`.
    """
    if row.request_kind is not None:
        # Never NULL on an attempt row of the router; the request's default
        # is what a request without one would carry.
        temperature = (
            row.temperature
            if row.temperature is not None
            else float(LLMRequest.model_fields["temperature"].default)
        )
        return SentWith(row.seed, temperature, RULE_COLUMNS)
    if datetime.fromisoformat(row.created_at) < SEED_SENT_SINCE:
        return SentWith(None, DETERMINISTIC_TEMPERATURE, RULE_BEFORE_SEED)
    return SentWith(
        REPRODUCIBILITY_SEED if seed_supported else None,
        DETERMINISTIC_TEMPERATURE,
        RULE_SINCE_SEED,
    )


def compare(
    request: LLMRequest,
    *,
    provider: str,
    prompt_hash: str,
    rows: Sequence[RegisterRow],
    recorded: Recorded,
    seed_supported: bool,
) -> HashCheck:
    """Mark each row whose ``input_hash`` is the recorded request's.

    The request is hashed once per row, with the seed and the temperature that
    row was sent with (:func:`sent_with`): the same input under another seed
    is another hash.
    """
    marked: list[RegisterRow] = []
    for row in rows:
        sent = sent_with(row, seed_supported=seed_supported)
        rebuilt = hash_attempt_input(
            request.model_copy(
                update={"seed": sent.seed, "temperature": sent.temperature}
            ),
            provider=provider,
        )
        marked.append(
            replace(
                row,
                sent_rule=sent.rule,
                rebuilt_hash=rebuilt,
                matched=row.input_hash == rebuilt,
            )
        )
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
    if recorded.criteria_from == FROM_IN_FORCE:
        notes.append(
            "the submission has no verdict rows: the list is the one in force "
            "now, which may not be the one the stage was handed"
        )
    return HashCheck(
        stage=STAGE,
        provider=provider,
        model=request.model,
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
            source = (
                "the list the submission's verdicts were judged against"
                if recorded.criteria_from == FROM_VERDICT_ROWS
                else "the list in force now: the submission has no verdict rows"
            )
            sys.stdout.write(
                f"written {args.out} (criteria: {recorded.criteria_layer} "
                f"{recorded.criteria_source_id}, {source})\n"
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
                seed_supported=seed_supported(provider, request),
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
