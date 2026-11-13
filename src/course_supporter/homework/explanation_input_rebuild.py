"""The explanation stage's input, rebuilt from what a submission left.

Purpose:
    The stage does not keep its input whole: it builds it on every run from
    rows that stay — the verdict rows, the list they were judged against, the
    course links kept for the submission, the task's texts — and from the work
    as the doors read it, which is read again from the stored file. This
    module repeats those reads for one submission, writes nothing, calls no
    model, and says whether the rebuilt input is the one the stage was sent
    (``scripts/record_explanation_input.py``; the measurement that uses it is
    ``homework/explanation_measure.py``).

What is rebuilt, and from where:
    * the facts, the course links, the task's texts and the language — by the
      stage's own reads (``review_explanation.explanation_input``);
    * the language the path resolved — by the path runner's rule: the language
      named on the submission, the student's preference, the task's language;
    * the work's text — the stored file through Stage 1 and the text
      assembler with the arguments the path runner's door gives them, for a
      ``task`` or a ``short_task`` alone (a project has its own stages).

    What can make it differ from the input of the run: the task's summary
    re-ingested since, a student's preference changed since, the door's code
    changed since; and a submission stored without its original file name,
    which the door framed by the name of a temporary file. Hence the check.

The check:
    The call register keeps the hash of every request it sent
    (``external_service_calls.input_hash``, ``llm/input_hash.py``). For each
    prompt the register names on the stage's first-rung rows of this
    submission's jobs, the request is rebuilt from the rebuilt input — the
    same router, nothing sent (``explanation_measure.send_once``) — and
    hashed. A hash that is in the register means the rebuilt input is
    byte for byte the one the stage was sent. A retry's row carries the
    retry's feedback in its request and never matches; the first attempt's
    does.
"""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from course_supporter.homework.doors import (
    assemble_submission_work,
    extract_document_text,
)
from course_supporter.homework.explanation_measure import STAGE_NAME, send_once
from course_supporter.homework.review_explanation import explanation_input
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.language import resolve_review_language
from course_supporter.models.source import AssignmentType
from course_supporter.normalizer.classify import denylist_prefix
from course_supporter.security.exceptions import SecurityRejectedError
from course_supporter.security.stage1 import run_stage1
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.orm import ExternalServiceCall, HomeworkSubmission, Job
from course_supporter.storage.student_repository import StudentRepository
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.agents.review_explainer import ExplanationInput
    from course_supporter.homework.path_config import ModelStage
    from course_supporter.llm.registry import ModelRegistryConfig
    from course_supporter.storage.s3 import S3Client

_TEXT_TYPES = frozenset({AssignmentType.TASK.value, AssignmentType.SHORT_TASK.value})


@dataclass(frozen=True, slots=True)
class RegisterRow:
    """One row of the call register for the stage of this submission."""

    job_id: uuid.UUID
    prompt_ref: str | None
    provider: str | None
    model_id: str | None
    input_hash: str | None
    call_started_at: datetime | None


@dataclass(frozen=True, slots=True)
class Rebuilt:
    """The rebuilt input, and what was found beside it.

    Attributes:
        shown: The agent's input, as the stage would build it now.
        language_code: The language of the explanation (ISO 639-3).
        task_type: ``task`` or ``short_task``.
        stored_explanation: The explanation the stage kept, if any — the
            answer the run gave, for reference.
        register: The stage's rows in the call register for this submission's
            jobs, oldest first.
    """

    submission_id: uuid.UUID
    shown: ExplanationInput
    language_code: str
    task_type: str
    stored_explanation: dict[str, Any] | None
    register: tuple[RegisterRow, ...]


async def rebuild_input(
    session: AsyncSession, s3: S3Client, submission_id: uuid.UUID
) -> Rebuilt:
    """Rebuild the explanation stage's input for one submission. Reads only.

    Raises:
        LookupError: No such submission, or no task document.
        ValueError: The task is not a ``task`` or a ``short_task``; the doors
            refuse the stored file now; or the stage's own reads refuse (no
            verdict rows, rows the list does not match, no language).
    """
    submission = await session.get(HomeworkSubmission, submission_id)
    if submission is None:
        msg = f"no submission {submission_id}"
        raise LookupError(msg)
    task_doc = await AuthoredDocumentRepository(session).get_by_id(
        submission.authored_document_id
    )
    if task_doc is None:
        msg = f"submission {submission_id} has no task document"
        raise LookupError(msg)
    if task_doc.task_type not in _TEXT_TYPES:
        msg = (
            f"submission {submission_id} is of type {task_doc.task_type!r}: "
            "the explanation stage explains a task or a short task"
        )
        raise ValueError(msg)
    student = await StudentRepository(session).get_by_id(submission.student_id)
    # The path runner's rule (homework/path_runner.py::_open_the_doors).
    review_language = resolve_review_language(
        explicit=submission.response_language,
        preferred=student.preferred_language if student else None,
        course=task_doc.language,
    )
    languages = [
        code
        for code in dict.fromkeys([task_doc.language, review_language.code])
        if code
    ]
    work = await _read_the_work(s3, submission, languages)

    shown, language_code = await explanation_input(
        session,
        submission,
        submission_text=work,
        language=review_language.code,
    )
    stored = await SubmissionVerdictRepository(session).get_explanation(
        tenant_id=submission.tenant_id, submission_id=submission.id
    )
    return Rebuilt(
        submission_id=submission.id,
        shown=shown,
        language_code=language_code,
        task_type=task_doc.task_type,
        stored_explanation=None if stored is None else dict(stored.body),
        register=await _register_rows(session, submission.id),
    )


async def _read_the_work(
    s3: S3Client, submission: HomeworkSubmission, languages: list[str]
) -> str:
    """The work's text, as the path runner's door reads a text task's file.

    The same calls with the same arguments as
    ``homework/path_runner.py::_open_the_doors`` for a type that is neither a
    test nor a project — and none of its writes: a refusal is raised here,
    never persisted.
    """
    key = s3.extract_key(submission.file_url)
    if key is None:
        msg = f"cannot extract an S3 key from {submission.file_url}"
        raise ValueError(msg)
    with tempfile.TemporaryDirectory() as tmp:
        file_path = await s3.download_file(key, Path(tmp) / Path(key).name)
        file_bytes = file_path.read_bytes()
        filename = submission.original_filename or file_path.name
    try:
        result = run_stage1(
            filename=filename,
            content=file_bytes,
            context="homework",
            languages=languages,
            archive_skip_matcher=denylist_prefix,
            document_extractor=extract_document_text,
        )
        text, _, _ = assemble_submission_work(
            result, file_bytes=file_bytes, filename=filename
        )
    except SecurityRejectedError as refused:
        msg = f"the doors refuse the stored file now: {refused.category.value}"
        raise ValueError(msg) from refused
    return text


async def _register_rows(
    session: AsyncSession, submission_id: uuid.UUID
) -> tuple[RegisterRow, ...]:
    """The stage's call rows of every job of the submission, oldest first."""
    esc = ExternalServiceCall
    rows = await session.execute(
        select(esc)
        .join(Job, Job.id == esc.job_id)
        .where(
            Job.subject_type == JOB_SUBJECT_TYPE[JobType.HOMEWORK_PROCESSING],
            Job.subject_id == submission_id,
            esc.action == STAGE_NAME,
            esc.provider.is_not(None),
        )
        .order_by(esc.call_started_at, esc.id)
    )
    return tuple(
        RegisterRow(
            job_id=row.job_id,
            prompt_ref=row.prompt_ref,
            provider=row.provider,
            model_id=row.model_id,
            input_hash=row.input_hash,
            call_started_at=row.call_started_at,
        )
        for row in rows.scalars()
    )


async def verify_against_register(
    rebuilt: Rebuilt,
    *,
    stage: ModelStage,
    registry: ModelRegistryConfig,
    prompt_base_path: Path | None = None,
) -> dict[str, Any]:
    """Whether the rebuilt input is the one the stage was sent (see the module).

    Returns a JSON-ready mapping: for each prompt the register names on the
    first rung's rows, the rebuilt request's hash and whether a row holds it;
    and ``matched`` — true when one of them does.
    """
    rung = stage.ladder[0]
    first_rung_rows = [
        row
        for row in rebuilt.register
        if row.provider == rung.provider and row.model_id == rung.model
    ]
    by_prompt: dict[str, Any] = {}
    for prompt_ref in dict.fromkeys(row.prompt_ref for row in first_rung_rows):
        if prompt_ref is None:
            continue
        sent = await send_once(
            rebuilt.shown,
            prompt_ref,
            stage=stage,
            registry=registry,
            provider=None,
            prompt_base_path=prompt_base_path,
        )
        hashes = {
            row.input_hash for row in first_rung_rows if row.prompt_ref == prompt_ref
        }
        by_prompt[prompt_ref] = {
            "rebuilt_hash": sent.request_hash,
            "matched": sent.request_hash in hashes,
            "rows": sum(1 for row in first_rung_rows if row.prompt_ref == prompt_ref),
        }
    return {
        "first_rung": f"{rung.provider}/{rung.model}",
        "register_rows": len(rebuilt.register),
        "first_rung_rows": len(first_rung_rows),
        "prompts": by_prompt,
        "matched": any(entry["matched"] for entry in by_prompt.values()),
    }
