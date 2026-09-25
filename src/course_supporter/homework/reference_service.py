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
    :class:`ReferenceView` — what the author sees: status, answers, explanations.
    :class:`ExplanationsView` — what a review reads: the key and one language's
    explanations, the model's and the author's kept apart.
    :class:`ReferenceRefusedError` — a refusal with the code the author reads.
    :class:`GenerationInProgressError` — no generation could be asked for,
    because another job of the task is in flight.
    :class:`ExplanationQueue` — the seam a generation request goes through;
    :class:`ReadOnlyQueue` — the one to hand a service that must only read.
    :class:`ReferenceService` — read, replace, clear; and for a submission,
    whether the key applies, its explanations in a given language, and the
    request for them in the student's language once the review is out.

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
    version appears. Two readers never write: :meth:`ReferenceService.key_applies`
    and :meth:`ReferenceService.explanations_for`, what a submission asks before
    its review is delivered. They answer from the same test the carry-over runs
    and leave the carrying to the next read that may write — for a submission,
    :meth:`ReferenceService.request_explanations`, after delivery.

Replacing the generation seam:
    :class:`ExplanationQueue` is the whole contract: a request, with one pair of
    identifiers, no return value, and one error — :class:`GenerationInProgressError`
    when another job of the task is in flight — and the question whether one
    is, asked before a stuck version is asked for again. The shipped
    implementation (block G) enqueues an ARQ job; a test passes a counter; a
    future implementation could run it inline. Nothing in this module names
    ``JobType``, ARQ or Redis — a service that knew how the work is scheduled
    would have to change when that changes.

A test written in the system (task 07b):
    Its key is the marked options of a published version, not an author's
    layer, so :meth:`ReferenceService.replace_key` and
    :meth:`ReferenceService.clear_key` refuse it (``KEY_LIVES_IN_TEST``). Its
    explanations are asked for by the axes of a published version —
    :meth:`ReferenceService.order_explanations` — by a publication in the
    course language and by a submission in the student's
    (:meth:`ReferenceService.request_explanations`). A version left ``pending``
    with no job of the task in flight is stuck, and both ask for it again; a
    ``failed`` one only a publication does (task 07b, decision 13). The author
    reads its state (:meth:`ReferenceService.read`) from its version in force,
    and that read writes nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Final, Protocol

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
from course_supporter.homework.test_object import PublishedBody
from course_supporter.models.source import AssignmentType, SourceType
from course_supporter.reference_kinds import ReferenceKind, ReferenceState
from course_supporter.storage.orm import (
    AuthoredDocument,
    TaskReference,
    TaskReferenceOverride,
    TestVersion,
)
from course_supporter.storage.task_reference_repository import TaskReferenceRepository
from course_supporter.storage.test_object_repository import TestObjectRepository

logger = structlog.get_logger(__name__)

_STUCK_REASON: Final[str] = (
    "stuck: pending with no job of the task in flight, so it was asked for again"
)
"""The reason a stuck version is closed with before it is asked for again."""


def _is_test_object(document: AuthoredDocument) -> bool:
    """Whether the task is a test written in the system (task 07b)."""
    return document.source_type == SourceType.TEST_OBJECT.value


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


_STATUS_OF_STATE: Final[dict[str, ReferenceStatus]] = {
    ReferenceState.PENDING.value: ReferenceStatus.GENERATING,
    ReferenceState.READY.value: ReferenceStatus.READY,
    ReferenceState.FAILED.value: ReferenceStatus.FAILED,
}
"""What the author reads for the stored state of a version."""


class RefusalCode(StrEnum):
    """Why a key could not be accepted — one code per reason, never one for all.

    Most of these are about the TASK, one is about the key, and one about
    where the key lives. Keeping them apart is the point: "your key is wrong"
    and "this task cannot carry a key yet" send the author to different places,
    and a single code would send both to the wrong one.
    """

    NOT_A_TEST_TASK = "NOT_A_TEST_TASK"
    TASK_NOT_READY = "TASK_NOT_READY"
    TASK_LANGUAGE_UNSET = "TASK_LANGUAGE_UNSET"
    TASK_TEXT_TRUNCATED = "TASK_TEXT_TRUNCATED"
    NO_QUESTION_NUMBERS = "NO_QUESTION_NUMBERS"
    KEY_DOES_NOT_MATCH_QUESTIONS = "KEY_DOES_NOT_MATCH_QUESTIONS"
    KEY_LIVES_IN_TEST = "KEY_LIVES_IN_TEST"


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


class GenerationInProgressError(Exception):
    """Another job of this task is in flight, so no generation could be asked for.

    The database keeps one job in flight per task: the explanation job takes the
    task as its subject, and so does the task's own processing
    (``uq_jobs_subject_in_flight``). Not a refusal of the key — the request is
    not wrong, only early — which is why it is not a :class:`RefusalCode`.

    A caller that meets it rolls its session back. The version created for the
    request must not outlive the job it was created for: only a NEW version asks
    for work, so a version left without one would stay ``generating`` for good,
    and the next request would find it and ask for nothing (task 07,
    decision 9). With the shipped queue the refused insert has already voided
    the transaction, and the rollback only makes the session usable again; a
    queue that looks before it inserts has voided nothing, and then the
    rollback is what removes the version.
    """

    code: ClassVar[str] = "GENERATION_IN_PROGRESS"

    def __init__(self, authored_document_id: uuid.UUID) -> None:
        super().__init__(
            f"{self.code}: another job of task {authored_document_id} is in flight"
        )
        self.authored_document_id = authored_document_id


@dataclass(frozen=True, slots=True)
class ReferenceView:
    """What the author sees about the current version of a task's reference."""

    status: ReferenceStatus
    version: int | None = None
    answers: AnswerKey = field(default_factory=dict)
    explanations: dict[str, str] = field(default_factory=dict)
    carried_over: bool = False
    language: str | None = None
    failure_reason: str | None = None
    pass_threshold: int | None = None
    doubts: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExplanationsView:
    """What a review reads: the key, and its explanations in one language.

    :class:`ReferenceView` merges the two sets of explanations and lets the
    author's words win, because that is what the author asks to see. A review
    cannot merge them: a doubt hides only the MODEL's words
    (``homework/test_scoring.py``, ``choose_explanation``), so here the model's,
    the author's and the model's doubts stay apart.

    ``answers`` is empty when no key applies to the task as it stands.
    ``model`` and ``doubts`` are empty until the version in ``language`` is
    ready, and when that language has no version at all.
    """

    language: str
    answers: AnswerKey = field(default_factory=dict)
    pass_threshold: int | None = None
    author: dict[str, str] = field(default_factory=dict)
    model: dict[str, str] = field(default_factory=dict)
    doubts: dict[str, bool] = field(default_factory=dict)


class ExplanationQueue(Protocol):
    """The seam a request for generation goes through.

    One method, and deliberately no return value: the caller must not be able
    to wait for the work, because the work costs money and takes a minute, and
    the author's request must not.

    An implementation that cannot ask because another job of the task is in
    flight raises :class:`GenerationInProgressError`, whatever told it so — a
    refused insert, or a look before one.

    :meth:`in_flight` is the question behind that refusal, asked before a
    version left ``pending`` is asked for again: while no job of its task is
    in flight, nothing will ever write it (task 07b, decision 13).
    """

    async def request(
        self, *, authored_document_id: uuid.UUID, reference_id: uuid.UUID
    ) -> None: ...

    async def in_flight(self, *, authored_document_id: uuid.UUID) -> bool: ...


class ReadOnlyQueue:
    """The queue behind a service that must only read.

    :meth:`ReferenceService.key_applies` and
    :meth:`ReferenceService.explanations_for` never ask for work. A caller that
    must not pay — a submission's doors, its review — hands them this, so a
    change that started to ask fails loudly instead of paying from there.
    """

    async def request(
        self, *, authored_document_id: uuid.UUID, reference_id: uuid.UUID
    ) -> None:
        msg = (
            f"a read-only use of the reference asked for work on task "
            f"{authored_document_id}"
        )
        raise RuntimeError(msg)

    async def in_flight(self, *, authored_document_id: uuid.UUID) -> bool:
        """Never asked by a read: only asking for work looks for a job in flight."""
        msg = (
            f"a read-only use of the reference looked for work in flight on task "
            f"{authored_document_id}"
        )
        raise RuntimeError(msg)


class ReferenceService:
    """Read, replace and clear the reference of one task.

    A submission asks two more questions: whether the key applies to the test
    as the student sees it (:meth:`key_applies`), and what the key's
    explanations are in a given language (:meth:`explanations_for`). Once its
    review is delivered it asks for them in the student's language
    (:meth:`request_explanations`).
    """

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

        A test written in the system is read from its version in force and
        nothing is written: its explanations are asked for when it is
        published, not when anyone looks (task 07b).
        """
        document = await self._require_test_task(authored_document_id)
        if _is_test_object(document):
            return await self._read_a_written_test(document)
        override = await self._applicable_override(document)
        if override is None:
            return ReferenceView(
                status=ReferenceStatus.AWAITING_KEY, language=document.language
            )

        version = await self._latest_version(
            document, override, document.language or ""
        )
        return self._view(document, override, version)

    async def explanations_for(
        self, authored_document_id: uuid.UUID, language: str
    ) -> ExplanationsView:
        """The current key and its explanations in ``language``, kept apart.

        Read-only. A review reads this before it is delivered, and a write here
        would ride on the submission's own transaction and could meet another
        job of the task (task 07, decision 9). So the key is the one
        :meth:`key_applies` sees — written for the current text, or one that
        would be carried onto it — and the carrying, like the asking for work,
        waits for :meth:`request_explanations`. Nothing is lost by waiting: a
        version made now would still be ``pending`` when this review is sent.
        A language without a version comes back with the model's side empty.
        """
        document = await self._require_test_task(authored_document_id)
        override = await self._repo.get_override(authored_document_id, self._kind)
        if override is None or not await self._applies(document, override):
            return ExplanationsView(language=language)

        version = await self._latest_version(document, override, language)
        return ExplanationsView(
            language=language,
            answers=dict(override.answers),
            pass_threshold=override.pass_threshold,
            author=dict(override.author_explanations or {}),
            model=dict(version.explanations or {}) if version is not None else {},
            doubts=dict(version.doubts or {}) if version is not None else {},
        )

    async def request_explanations(
        self, authored_document_id: uuid.UUID, language: str
    ) -> None:
        """Ask for the key's explanations in ``language``, once per version.

        A submission's step after its review is delivered (task 07, decision 9).
        The key is carried onto the current text as :meth:`read` carries it —
        which asks for its explanations in the course language when the text
        moved — and then, if ``language`` has no version of this key yet, one is
        made and its generation asked for. A version that exists asks for
        nothing, whatever its state: a failed one is not retried from here, so
        a generation that keeps failing is not paid for once per submission.

        A test written in the system has no layer to carry: its explanations
        are asked for by the axes of its current published version, through
        :meth:`order_explanations` — a stuck version asked for again, a failed
        one left as it is (task 07b, decision 13). Nothing published, nothing
        asked.

        Raises:
            GenerationInProgressError: another job of the task is in flight. The
                caller rolls its session back; the next submission asks again.
        """
        document = await self._require_test_task(authored_document_id)
        if _is_test_object(document):
            published = await TestObjectRepository(self._session).latest_version(
                document.id
            )
            if published is not None:
                await self.order_explanations(
                    document, published, language, retry_failed=False
                )
            return
        override = await self._applicable_override(document)
        if override is None:
            return
        if await self._latest_version(document, override, language) is not None:
            return
        await self._ensure_version(document, override, language)

    async def key_applies(self, authored_document_id: uuid.UUID) -> bool:
        """Whether the author's key applies to the task as it stands — read-only.

        The doors' question (task 07, decision 12): a submission is taken only
        when there is a key for the version of the test the student sees. The
        key applies when it was written for the current text or would be
        carried onto it, and "would be carried" is the carry-over's own test,
        so the doors and the carry-over cannot disagree. Nothing is written and
        nothing is asked for: the carrying, and the generation it asks for,
        wait for the next read that may write.
        """
        document = await self._require_test_task(authored_document_id)
        override = await self._repo.get_override(authored_document_id, self._kind)
        return override is not None and await self._applies(document, override)

    async def replace_key(
        self,
        authored_document_id: uuid.UUID,
        answers: AnswerKey,
        author_explanations: dict[str, str] | None = None,
        *,
        pass_threshold: int | None = None,
    ) -> ReferenceView:
        """Replace the author's key whole, and generate its explanations once.

        The checks run in a fixed order, from the task to the key, because that
        is the order in which a cause makes sense: a key cannot be wrong for a
        task that cannot carry one. Each refusal carries its own code
        (ratified 2026-09-19).

        A generation is requested ONLY when the version was just created. The
        same answers sent twice find the version already there and cost
        nothing — the database decides that, not a branch here
        (``TASK.md`` invariant 4). The pass mark is replaced with the rest of
        the layer, so a replacement without one clears it; it is outside the
        version key, and changing it alone asks for nothing.

        A test written in the system is refused before anything is checked: its
        key is the marked options of its published version (task 07b).
        """
        document = await self._require_test_task(authored_document_id)
        self._refuse_a_test_object(document)
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
            pass_threshold=pass_threshold,
        )
        version = await self._ensure_version(document, override, language)
        return self._view(document, override, version)

    async def clear_key(self, authored_document_id: uuid.UUID) -> bool:
        """Drop the author's layer; the task goes back to awaiting a key.

        The generated versions stay. They cost money, they belong to the keys
        that produced them, and a key sent again finds its version waiting
        rather than paying for it twice.

        A test written in the system has no layer to drop, and is refused.
        """
        self._refuse_a_test_object(await self._require_test_task(authored_document_id))
        return await self._repo.clear_override(authored_document_id, self._kind)

    async def order_explanations(
        self,
        document: AuthoredDocument,
        published: TestVersion,
        language: str,
        *,
        retry_failed: bool,
    ) -> TaskReference:
        """Ask for the explanations of a published test in ``language``, when due.

        The axes are the version's visible digest and its key's digest, never
        its number: a publication that moves neither — a new pass mark, the
        author's own explanations, a return to an earlier state — finds its
        explanations already there (task 07b, decision 9).

        The asking is task 06's step: a generation is asked for only when a
        version is created, so a ``ready`` version, or a ``pending`` one whose
        job is in flight, comes back as it is. Two rules of decision 13
        (PRE-FLIGHT section 8.5) come first:

        * ``pending`` with no job of the task in flight is stuck — nothing will
          ever write it. It is marked ``failed`` with that reason, and the step
          then creates its successor and asks for it.
        * ``failed`` is asked for again only when ``retry_failed``: a
          publication in the course language retries it, a submission does
          not, so a generation that keeps failing is not paid for once per
          submission.

        Raises:
            GenerationInProgressError: another job of the task is in flight.
        """
        axes = {
            "source_content_hash": published.content_digest,
            "source_task_type": document.task_type or "",
            "answers_hash": published.answers_digest,
            "language": language,
        }
        latest = await self._repo.latest_for_key(
            authored_document_id=document.id, kind=self._kind, **axes
        )
        if latest is not None:
            if latest.state == ReferenceState.PENDING.value:
                if not await self._queue.in_flight(authored_document_id=document.id):
                    await self._repo.mark_failed(latest.id, _STUCK_REASON)
            elif latest.state == ReferenceState.FAILED.value and not retry_failed:
                return latest
        return await self._order(document.id, **axes)

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

    async def _applicable_override(
        self, document: AuthoredDocument
    ) -> TaskReferenceOverride | None:
        """The author's layer, carried onto the current version — or ``None``.

        ``None`` both when there is no layer and when it no longer fits: to a
        reader the two are the same state, a task waiting for a key.
        """
        override = await self._repo.get_override(document.id, self._kind)
        if override is None:
            return None
        if not await self._carry_over_if_it_still_fits(document, override):
            return None
        return override

    async def _applies(
        self, document: AuthoredDocument, override: TaskReferenceOverride
    ) -> bool:
        """Whether the author's layer applies to the current version — read-only.

        It applies when it was written for the current text, or when it would
        be carried onto it. Carrying needs a language to explain in and a fit,
        and fit means one thing only: the set of question numbers is the same.
        The answers are then still about the same questions, whatever else the
        author edited in the text. A different set means the key is about
        questions that are gone or silent about questions that appeared, so it
        stays where it is — NOT deleted (the author's work is not thrown away
        on a text edit) — and the task waits for a new key.
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
        return True

    async def _carry_over_if_it_still_fits(
        self, document: AuthoredDocument, override: TaskReferenceOverride
    ) -> bool:
        """Move the author's answers onto the current task version, if they fit.

        Whether they fit is :meth:`_applies`, the same test :meth:`key_applies`
        answers from. The layer moves whole, pass mark included: the carry-over
        is the author's layer on a new text, not a new layer.

        Returns whether the layer applies to the current version.
        """
        if not await self._applies(document, override):
            return False
        if override.source_content_hash == document.content_hash:
            return True

        carried = await self._repo.replace_override(
            authored_document_id=document.id,
            kind=self._kind,
            answers=override.answers,
            author_explanations=override.author_explanations,
            source_content_hash=document.content_hash or "",
            carried_over=True,
            pass_threshold=override.pass_threshold,
        )
        await self._ensure_version(document, carried, self._require_language(document))
        return True

    async def _latest_version(
        self,
        document: AuthoredDocument,
        override: TaskReferenceOverride,
        language: str,
    ) -> TaskReference | None:
        """The newest version of this key in ``language``, a failed one included."""
        return await self._repo.latest_for_key(
            authored_document_id=document.id,
            kind=self._kind,
            source_content_hash=document.content_hash or "",
            source_task_type=document.task_type or "",
            answers_hash=answers_digest(override.answers),
            language=language,
        )

    async def _ensure_version(
        self,
        document: AuthoredDocument,
        override: TaskReferenceOverride,
        language: str,
    ) -> TaskReference:
        """The version for this key, requesting a generation only if it is new."""
        return await self._order(
            document.id,
            source_content_hash=document.content_hash or "",
            source_task_type=document.task_type or "",
            answers_hash=answers_digest(override.answers),
            language=language,
        )

    async def _order(
        self,
        authored_document_id: uuid.UUID,
        *,
        source_content_hash: str,
        source_task_type: str,
        answers_hash: str,
        language: str,
    ) -> TaskReference:
        """The live version for these axes, requesting a generation only if new."""
        version, created = await self._repo.create_version(
            authored_document_id=authored_document_id,
            kind=self._kind,
            source_content_hash=source_content_hash,
            source_task_type=source_task_type,
            answers_hash=answers_hash,
            language=language,
        )
        if created:
            await self._queue.request(
                authored_document_id=authored_document_id, reference_id=version.id
            )
            logger.info(
                "reference_generation_requested",
                authored_document_id=str(authored_document_id),
                reference_id=str(version.id),
                version=version.version,
            )
        return version

    def _refuse_a_test_object(self, document: AuthoredDocument) -> None:
        """A test written in the system keeps its key in its published version."""
        if _is_test_object(document):
            raise ReferenceRefusedError(
                RefusalCode.KEY_LIVES_IN_TEST,
                "the key of a test written in the system is the options its "
                "published version marks; change the draft and publish it",
            )

    async def _read_a_written_test(self, document: AuthoredDocument) -> ReferenceView:
        """What the author sees of a written test, from its version in force.

        The key, the pass mark and the author's own explanations are the
        version's; the model's explanations, its doubts and the state are the
        machine layer's, for the version's axes in the version's language — and
        the author's words win per question, as for a key typed in (task 07b,
        PRE-FLIGHT section 7.3).
        """
        published = await TestObjectRepository(self._session).latest_version(
            document.id
        )
        if published is None:
            return ReferenceView(
                status=ReferenceStatus.AWAITING_KEY, language=document.language
            )
        body = PublishedBody.from_jsonb(published.body)
        explanation = await self._repo.latest_for_key(
            authored_document_id=document.id,
            kind=self._kind,
            source_content_hash=published.content_digest,
            source_task_type=document.task_type or "",
            answers_hash=published.answers_digest,
            language=published.language,
        )
        if explanation is None:
            # A publication asks for these before it commits, so a version
            # without them is not a state the service leaves behind.
            return ReferenceView(
                status=ReferenceStatus.AWAITING_KEY,
                answers=body.answer_key(),
                language=published.language,
                pass_threshold=body.pass_threshold,
            )
        explanations = dict(explanation.explanations or {})
        explanations.update(body.explanations())
        return ReferenceView(
            status=_STATUS_OF_STATE[explanation.state],
            version=explanation.version,
            answers=body.answer_key(),
            explanations=explanations,
            language=explanation.language,
            failure_reason=explanation.failure_reason,
            pass_threshold=body.pass_threshold,
            doubts=dict(explanation.doubts or {}),
        )

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
                pass_threshold=override.pass_threshold,
            )

        explanations = dict(version.explanations or {})
        explanations.update(override.author_explanations or {})
        return ReferenceView(
            status=_STATUS_OF_STATE[version.state],
            version=version.version,
            answers=dict(override.answers),
            explanations=explanations,
            carried_over=override.carried_over,
            language=version.language,
            failure_reason=version.failure_reason,
            pass_threshold=override.pass_threshold,
            doubts=dict(version.doubts or {}),
        )
