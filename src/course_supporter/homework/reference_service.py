"""What the author's key means for the task it belongs to (mentor-rebuild task 06).

Purpose:
    Between the author's answers and the explanations a model writes there is a
    set of decisions nobody else should repeat: whether this task can carry a
    key at all, whether the key still fits the text, whether the answers moved,
    and whether any of that is worth a paid generation. They live here, once,
    and the route, the job and task 07's submission path all read the same
    verdict.

Interface:
    :class:`ReferenceStatus` — how far the current version got.
    :class:`ReferenceView` — what a reader sees: status, answers, explanations.
    :class:`ReferenceRefusedError` — a refusal with the code the author reads.
    :class:`ExplanationQueue` — the seam a generation request goes through.
    :class:`ReferenceService` — read, replace, clear.

The lazy path, and why the reader writes:
    There is no event that says "this task has a new version". The content hash
    is recomputed where the summary is written
    (``storage/document_summary_repository.py``), and nothing downstream is
    told. So the state of a reference is computed by its first READER — this
    service, called from the author's routes today and from task 07's
    submission path tomorrow — exactly as the criteria cache computes its own
    (``homework/criteria_cache.py``: read the document, compare the version
    keys, decide). The ingestion pipeline stays untouched (ratified
    2026-09-19), and the price is that a read can write: carrying the author's
    answers onto a new task version happens when someone looks, not when the
    version appears.

Replacing the generation seam:
    :class:`ExplanationQueue` is the whole contract: one method, one pair of
    identifiers, no return value. The shipped implementation (block G) enqueues
    an ARQ job; a test passes a counter; a future implementation could run it
    inline. Nothing in this module names ``JobType``, ARQ or Redis — a service
    that knew how the work is scheduled would have to change when that changes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.homework.reference_key import (
    AnswerKey,
    QuestionNumbers,
    answers_digest,
    compare_to_questions,
    parse_question_numbers,
)
from course_supporter.homework.task_context import load_task_source_text
from course_supporter.homework.task_text import MENTOR_TASK_TEXT_MAX_BYTES
from course_supporter.models.source import AssignmentType
from course_supporter.reference_kinds import ReferenceKind, ReferenceState
from course_supporter.storage.orm import (
    AuthoredDocument,
    TaskReference,
    TaskReferenceOverride,
)
from course_supporter.storage.task_reference_repository import TaskReferenceRepository

logger = structlog.get_logger(__name__)


class ReferenceStatus(StrEnum):
    """How far the reference of the task's CURRENT version got.

    Derived on read, never stored: the stored ``state`` belongs to one version,
    and what the author asks is about the task as it stands now.

    * ``AWAITING_KEY`` — no author layer, or one that no longer fits the text.
    * ``GENERATING`` — the key is in, its explanations are not written yet.
    * ``READY`` — answers and explanations are both available.
    * ``FAILED`` — generation gave up; ``failure_reason`` says why.
    """

    AWAITING_KEY = "awaiting_key"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"


class RefusalCode(StrEnum):
    """Why a key could not be accepted — one code per reason, never one for all.

    Four of these are about the TASK and one is about the key. Keeping them
    apart is the point: "your key is wrong" and "this task cannot carry a key
    yet" send the author to different places, and a single code would send both
    to the wrong one.
    """

    NOT_A_TEST_TASK = "NOT_A_TEST_TASK"
    TASK_NOT_READY = "TASK_NOT_READY"
    TASK_LANGUAGE_UNSET = "TASK_LANGUAGE_UNSET"
    TASK_TEXT_TRUNCATED = "TASK_TEXT_TRUNCATED"
    NO_QUESTION_NUMBERS = "NO_QUESTION_NUMBERS"
    KEY_DOES_NOT_MATCH_QUESTIONS = "KEY_DOES_NOT_MATCH_QUESTIONS"


class ReferenceRefusedError(Exception):
    """A refusal the author can act on, carrying its code and its details.

    An exception rather than a returned value because every caller of
    :meth:`ReferenceService.replace_key` has exactly one thing to do with a
    refusal — surface it — and a returned union would let one of them forget.
    The route turns it into a 422 body; nothing else catches it.
    """

    def __init__(self, code: RefusalCode, details: str) -> None:
        super().__init__(f"{code.value}: {details}")
        self.code = code
        self.details = details


@dataclass(frozen=True, slots=True)
class ReferenceView:
    """What a reader sees about the current version of a task's reference."""

    status: ReferenceStatus
    version: int | None = None
    answers: AnswerKey = field(default_factory=dict)
    explanations: dict[str, str] = field(default_factory=dict)
    carried_over: bool = False
    language: str | None = None
    failure_reason: str | None = None


class ExplanationQueue(Protocol):
    """The seam a request for generation goes through.

    One method, and deliberately no return value: the caller must not be able
    to wait for the work, because the work costs money and takes a minute, and
    the author's request must not.
    """

    async def request(
        self, *, authored_document_id: uuid.UUID, reference_id: uuid.UUID
    ) -> None: ...


class ReferenceService:
    """Read, replace and clear the reference of one task."""

    def __init__(
        self,
        session: AsyncSession,
        queue: ExplanationQueue,
        *,
        kind: ReferenceKind = ReferenceKind.TEST_KEY,
    ) -> None:
        self._session = session
        self._repo = TaskReferenceRepository(session)
        self._queue = queue
        self._kind = kind

    async def read(self, authored_document_id: uuid.UUID) -> ReferenceView:
        """The state of this task's reference as it stands now.

        Carries the author's answers onto a new task version when they still
        fit it — the lazy path described in the module docstring — so a read
        can both write a row and request a generation. When the task is not in
        a shape to carry a key (no language yet, truncated text, no numbered
        questions) the carry is skipped rather than refused: the author asked
        what the state is, and "awaiting key" IS the answer.
        """
        document = await self._require_test_task(authored_document_id)
        override = await self._repo.get_override(authored_document_id, self._kind)
        if override is None:
            return ReferenceView(
                status=ReferenceStatus.AWAITING_KEY, language=document.language
            )

        carried = await self._carry_over_if_it_still_fits(document, override)
        if not carried:
            return ReferenceView(
                status=ReferenceStatus.AWAITING_KEY, language=document.language
            )

        version = await self._repo.latest_for_key(
            authored_document_id=authored_document_id,
            kind=self._kind,
            source_content_hash=document.content_hash or "",
            source_task_type=document.task_type or "",
            answers_hash=answers_digest(override.answers),
            language=document.language or "",
        )
        return self._view(document, override, version)

    async def replace_key(
        self,
        authored_document_id: uuid.UUID,
        answers: AnswerKey,
        author_explanations: dict[str, str] | None = None,
    ) -> ReferenceView:
        """Replace the author's key whole, and generate its explanations once.

        The checks run in a fixed order, from the task to the key, because that
        is the order in which a cause makes sense: a key cannot be wrong for a
        task that cannot carry one. Each refusal carries its own code
        (ratified 2026-09-19).

        A generation is requested ONLY when the version was just created. The
        same answers sent twice find the version already there and cost
        nothing — the database decides that, not a branch here
        (``TASK.md`` invariant 4).
        """
        document = await self._require_test_task(authored_document_id)
        language = self._require_language(document)
        questions = self._require_questions(await self._task_text(document))

        mismatch = compare_to_questions(answers, questions)
        if mismatch:
            raise ReferenceRefusedError(
                RefusalCode.KEY_DOES_NOT_MATCH_QUESTIONS,
                f"key does not match the questions of the current task version; "
                f"missing: {list(mismatch.missing)}, unknown: {list(mismatch.unknown)}",
            )

        override = await self._repo.replace_override(
            authored_document_id=authored_document_id,
            kind=self._kind,
            answers=answers,
            author_explanations=author_explanations,
            source_content_hash=document.content_hash or "",
            carried_over=False,
        )
        version = await self._ensure_version(document, override, language)
        return self._view(document, override, version)

    async def clear_key(self, authored_document_id: uuid.UUID) -> bool:
        """Drop the author's layer; the task goes back to awaiting a key.

        The generated versions stay. They cost money, they belong to the keys
        that produced them, and a key sent again finds its version waiting
        rather than paying for it twice.
        """
        await self._require_test_task(authored_document_id)
        return await self._repo.clear_override(authored_document_id, self._kind)

    # ── internals ────────────────────────────────────────────────────────

    async def _require_test_task(
        self, authored_document_id: uuid.UUID
    ) -> AuthoredDocument:
        """The task, if it is a live test task that has settled into a version."""
        document = await self._session.get(AuthoredDocument, authored_document_id)
        if document is None or document.deleted_at is not None:
            raise ReferenceRefusedError(
                RefusalCode.TASK_NOT_READY, "the task does not exist any more"
            )
        if document.task_type != AssignmentType.TEST.value:
            raise ReferenceRefusedError(
                RefusalCode.NOT_A_TEST_TASK,
                "answer keys attach only to task_type='test' documents",
            )
        if not document.content_hash:
            raise ReferenceRefusedError(
                RefusalCode.TASK_NOT_READY,
                "the task has not finished processing — it has no version yet",
            )
        return document

    def _require_language(self, document: AuthoredDocument) -> str:
        """The course language, which the explanations are written in.

        Its own refusal rather than "not ready" (ratified 2026-09-19): the task
        IS processed, and telling the author to wait for something that has
        already happened sends them looking in the wrong place.
        """
        if not document.language:
            raise ReferenceRefusedError(
                RefusalCode.TASK_LANGUAGE_UNSET,
                "the task has no language, so explanations have none to be written in",
            )
        return document.language

    def _require_questions(self, task_text: str) -> QuestionNumbers:
        """The question numbers, or the reason there are none to check against."""
        questions = parse_question_numbers(task_text)
        if questions.truncated:
            raise ReferenceRefusedError(
                RefusalCode.TASK_TEXT_TRUNCATED,
                f"the task text is longer than the "
                f"{MENTOR_TASK_TEXT_MAX_BYTES // 1024} KiB every reader of it takes "
                "whole, so a key checked against it would be explained against a part",
            )
        if not questions.numbers:
            raise ReferenceRefusedError(
                RefusalCode.NO_QUESTION_NUMBERS,
                "no question numbers found; a question must start its line with "
                "its number and a full stop, as in '1.'",
            )
        return questions

    async def _task_text(self, document: AuthoredDocument) -> str:
        """The task's source text, which its question numbers are read from.

        Segments joined without a separator, not the stitched text the mentor
        pipeline assembles: a stitch boundary falls mid-line and could split a
        question's number (task 07, decision 8).
        """
        return await load_task_source_text(self._session, document.id)

    async def _carry_over_if_it_still_fits(
        self, document: AuthoredDocument, override: TaskReferenceOverride
    ) -> bool:
        """Move the author's answers onto the current task version, if they fit.

        Fit means one thing only: the set of question numbers is the same. The
        answers are then still about the same questions, whatever else the
        author edited in the text. A different set means the key is about
        questions that are gone or silent about questions that appeared, so it
        stays where it is — NOT deleted (the author's work is not thrown away
        on a text edit) — and the task waits for a new key.

        Returns whether the layer applies to the current version.
        """
        if override.source_content_hash == document.content_hash:
            return True
        if not document.language:
            return False

        questions = parse_question_numbers(await self._task_text(document))
        if not questions:
            return False
        if compare_to_questions(override.answers, questions):
            logger.info(
                "reference_key_no_longer_fits",
                authored_document_id=str(document.id),
                kind=self._kind.value,
            )
            return False

        carried = await self._repo.replace_override(
            authored_document_id=document.id,
            kind=self._kind,
            answers=override.answers,
            author_explanations=override.author_explanations,
            source_content_hash=document.content_hash or "",
            carried_over=True,
        )
        await self._ensure_version(document, carried, document.language)
        return True

    async def _ensure_version(
        self,
        document: AuthoredDocument,
        override: TaskReferenceOverride,
        language: str,
    ) -> TaskReference:
        """The version for this key, requesting a generation only if it is new."""
        version, created = await self._repo.create_version(
            authored_document_id=document.id,
            kind=self._kind,
            source_content_hash=document.content_hash or "",
            source_task_type=document.task_type or "",
            answers_hash=answers_digest(override.answers),
            language=language,
        )
        if created:
            await self._queue.request(
                authored_document_id=document.id, reference_id=version.id
            )
            logger.info(
                "reference_generation_requested",
                authored_document_id=str(document.id),
                reference_id=str(version.id),
                version=version.version,
            )
        return version

    def _view(
        self,
        document: AuthoredDocument,
        override: TaskReferenceOverride,
        version: TaskReference | None,
    ) -> ReferenceView:
        """Assemble what the reader sees, the author's words winning per question."""
        if version is None:
            return ReferenceView(
                status=ReferenceStatus.AWAITING_KEY,
                answers=dict(override.answers),
                carried_over=override.carried_over,
                language=document.language,
            )

        status = {
            ReferenceState.PENDING.value: ReferenceStatus.GENERATING,
            ReferenceState.READY.value: ReferenceStatus.READY,
            ReferenceState.FAILED.value: ReferenceStatus.FAILED,
        }[version.state]
        explanations = dict(version.explanations or {})
        explanations.update(override.author_explanations or {})
        return ReferenceView(
            status=status,
            version=version.version,
            answers=dict(override.answers),
            explanations=explanations,
            carried_over=override.carried_over,
            language=version.language,
            failure_reason=version.failure_reason,
        )
