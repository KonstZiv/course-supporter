"""A written test: its draft, its publication, the version in force (task 07b).

Purpose:
    The author writes a test as a draft and publishes it; a student answers
    the version that was published. Between the two stand the decisions of
    PRE-FLIGHT section 8, taken here once: in which language the test is
    lettered, when a publication is a new version, and when it asks for
    explanations and costs money.

Interface:
    :class:`TestObjectService` — create a test with its draft, save the draft,
        check it and read what its check found (task 07c), publish it, read
        the version in force, ask for a version's explanations as a submission
        does, and tell the explanation work which body a version of
        explanations was asked for (task 07c).
    :data:`WRITTEN_TEST_URL` — the source URL every test written in the
        system carries.
    :class:`Publication` — the version a publication gave, and whether it is
        new.
    :class:`DraftCheck` and :class:`DraftCheckState` — what the check of a
        draft found, and how far it got.
    :class:`NotATestObjectError` — the document is not a test written in the
        system, or it is gone.

Publishing (section 8.1):
    A draft is saved unfinished (task 07c, decision 11); a publication refuses
    one before it writes anything —
    :class:`~course_supporter.homework.test_completeness.DraftIncompleteError`
    with every unfinished place. A finished draft is published so:

    1. The language is the course root's ``default_language``, read at the
       moment of publishing — not the copy on the document, which is made when
       the test is created and stays behind a course whose language was changed
       since. When the two differ, the document's follows the course in the
       same transaction (operator's decision, 2026-09-25). A root without a
       language cannot exist — see :meth:`TestObjectService.course_language`.
    2. The draft is numbered, lettered and digested
       (:mod:`course_supporter.homework.test_object`).
    3. A version is created unless the draft equals the latest one
       (:meth:`~course_supporter.storage.test_object_repository.TestObjectRepository.publish`).
    4. The explanations in the course language are asked for by the axes of
       the version, through
       :meth:`~course_supporter.homework.reference_service.ReferenceService.order_explanations`:
       a pass mark, the author's own explanations or a return to an earlier
       state move no axis and cost nothing; a stuck or failed version is asked
       for again (decision 13).

Checking (task 07c):
    The author may have the draft's explanations and doubts written before it
    is published — :meth:`TestObjectService.check`. A check refuses an
    unfinished draft as a publication does, and then asks exactly as step 4
    would: by the axes the draft would be published with, in the course
    language, a stuck or failed version asked for again. So a check of an
    unchanged draft asks for nothing, and a publication of the checked draft
    finds its explanations there and pays nothing more. A check publishes
    nothing — no version of the test — and leaves the document's language as
    it is, so a student sees nothing of it. What it found is read with the
    draft (:meth:`TestObjectService.draft_check`), and that read asks for
    nothing.

Transactions:
    The service flushes and never commits. When a job is asked for, the queue
    commits — ``enqueue_key_explanation`` owns its commit — so what the step
    wrote lands with its job: a publication's version and language, a check's
    version of the explanations. When another job of the task is in flight the
    queue raises
    :class:`~course_supporter.homework.reference_service.GenerationInProgressError`
    and the caller rolls back: nothing of the publication or the check
    survives, and the author tries again (decision 12).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.homework.reference_service import (
    ExplanationQueue,
    ReferenceService,
)
from course_supporter.homework.test_completeness import require_complete
from course_supporter.homework.test_object import (
    DraftBody,
    PublishedBody,
    published_form,
    version_digests,
)
from course_supporter.models.source import AssignmentType, SourceType
from course_supporter.reference_kinds import ReferenceKind, ReferenceState
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    TaskReference,
    TestDraft,
    TestVersion,
)
from course_supporter.storage.test_object_repository import TestObjectRepository

WRITTEN_TEST_URL: Final[str] = "test-object:"
"""The source URL of a test written in the system: a fixed placeholder.

There is no file behind it (PRE-FLIGHT section 4.3). Storage clean-up reads no
key out of it and passes it by, and the portal's material route refuses the
document before anything could hand it out as a link.
"""


class NotATestObjectError(Exception):
    """The document is not a test written in the system, or it is gone."""

    def __init__(self, authored_document_id: uuid.UUID) -> None:
        super().__init__(
            f"document {authored_document_id} is not a test written in the system"
        )
        self.authored_document_id = authored_document_id


@dataclass(frozen=True, slots=True)
class Publication:
    """What a publication gave: the version in force, and whether it is new."""

    version: TestVersion
    created: bool


class DraftCheckState(StrEnum):
    """How far the check of the draft as it stands got (task 07c).

    * ``NOT_CHECKED`` — nothing was asked for this draft: never checked, or
      changed since. An unfinished draft is never checked.
    * ``IN_PROGRESS`` — its explanations are being written.
    * ``READY`` — its explanations and doubts are written.
    * ``FAILED`` — the writing gave up; a check asks for it again.
    """

    NOT_CHECKED = "not_checked"
    IN_PROGRESS = "in_progress"
    READY = "ready"
    FAILED = "failed"


_CHECK_STATE_OF: Final[dict[str, DraftCheckState]] = {
    ReferenceState.PENDING.value: DraftCheckState.IN_PROGRESS,
    ReferenceState.READY.value: DraftCheckState.READY,
    ReferenceState.FAILED.value: DraftCheckState.FAILED,
}


@dataclass(frozen=True, slots=True)
class DraftCheck:
    """What the check of a draft found: its state, and the model's words.

    ``explanations`` and ``doubts`` are the model's, by question number, and
    empty until ``READY``. The author's own explanations are the draft's and
    stay apart: a doubt hides only the model's words (KD20).
    """

    state: DraftCheckState
    explanations: dict[str, str] = field(default_factory=dict)
    doubts: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def of(cls, explanation: TaskReference | None) -> DraftCheck:
        """The check a version of the explanations stands for; none — not checked."""
        if explanation is None:
            return cls(state=DraftCheckState.NOT_CHECKED)
        return cls(
            state=_CHECK_STATE_OF[explanation.state],
            explanations=dict(explanation.explanations or {}),
            doubts=dict(explanation.doubts or {}),
        )


class TestObjectService:
    """The draft, the publication and the version in force of a written test."""

    __test__ = False  # a service, not a pytest class, whatever its name says

    def __init__(
        self,
        session: AsyncSession,
        queue: ExplanationQueue,
        *,
        kind: ReferenceKind = ReferenceKind.TEST_KEY,
    ) -> None:
        self._session = session
        self._repo = TestObjectRepository(session)
        self._references = ReferenceService(session, queue, kind=kind)

    async def create(
        self,
        course_node_id: uuid.UUID,
        body: DraftBody,
        *,
        title: str,
        language: str,
    ) -> AuthoredDocument:
        """Create a test written in the system in a node: its document and draft.

        The document is the row of PRE-FLIGHT section 4.3: no file, never
        processed, and so ready from its first moment — whether a student sees
        it is its publication's to decide (decision 8). Its language is the
        course's, read by the caller now (decision 7), and it is created with
        its title — the name both trees show — never without one. Nothing is
        asked for: a draft costs nothing until it is checked or published.
        """
        document = await AuthoredDocumentRepository(self._session).create(
            node_id=course_node_id,
            source_type=SourceType.TEST_OBJECT.value,
            source_url=WRITTEN_TEST_URL,
            task_type=AssignmentType.TEST,
            language=language,
            title=title,
        )
        await self._repo.replace_draft(document.id, body.to_jsonb())
        return document

    async def save_draft(
        self,
        authored_document_id: uuid.UUID,
        body: DraftBody,
        *,
        title: str | None = None,
    ) -> TestDraft:
        """Replace the draft whole; a title, when given, renames the test at once.

        The body arrives read by the format's rules — finished or not (task
        07c): an unfinished one is a publication's to refuse. The title is a
        column of the document, so it changes now and no publication carries it
        (answer 2 of section 13).
        """
        document = await self._require_test_object(authored_document_id)
        if title is not None:
            document.title = title
        return await self._repo.replace_draft(document.id, body.to_jsonb())

    async def publish(self, authored_document_id: uuid.UUID) -> Publication:
        """Publish the draft (section 8.1) — see the module docstring.

        Raises:
            DraftIncompleteError: the draft is not finished; nothing is written.
            GenerationInProgressError: another job of the task is in flight; the
                caller rolls back.
            RuntimeError: a broken invariant of the database — the course root
                has no language, or the test has no draft.
        """
        document = await self._require_test_object(authored_document_id)
        language = await self.course_language(document.course_root_id)
        body = await self._draft(document)
        # Before the first write, the language the course moved to included:
        # a refused publication leaves nothing a caller could commit by mistake.
        require_complete(body)
        published = published_form(body, language)
        digests = version_digests(published, language)
        if document.language != language:
            document.language = language
            await self._session.flush()
        version, created = await self._repo.publish(
            document.id,
            language=language,
            body=published.to_jsonb(),
            content_digest=digests.content_digest,
            answers_digest=digests.answers_digest,
            publication_digest=digests.publication_digest,
        )
        await self._references.order_explanations(
            document,
            content_digest=version.content_digest,
            answers_digest=version.answers_digest,
            language=language,
            retry_failed=True,
        )
        return Publication(version=version, created=created)

    async def check(self, authored_document_id: uuid.UUID) -> DraftCheck:
        """Ask for the draft's explanations and doubts — see the module docstring.

        The language is the course root's as it stands now, as for a
        publication; the document's own copy is left as it is, since only a
        publication moves it.

        Returns:
            The check after the request: ``IN_PROGRESS`` when its explanations
            are asked for — now or earlier — and not written yet; ``READY`` for
            an unchanged draft checked before.

        Raises:
            DraftIncompleteError: the draft is not finished; nothing is asked
                for.
            GenerationInProgressError: another job of the task is in flight; the
                caller rolls back.
            RuntimeError: a broken invariant of the database, as for
                :meth:`publish`.
        """
        document = await self._require_test_object(authored_document_id)
        language = await self.course_language(document.course_root_id)
        body = await self._draft(document)
        require_complete(body)
        digests = version_digests(published_form(body, language), language)
        explanation = await self._references.order_explanations(
            document,
            content_digest=digests.content_digest,
            answers_digest=digests.answers_digest,
            language=language,
            retry_failed=True,
        )
        return DraftCheck.of(explanation)

    async def draft_check(
        self, document: AuthoredDocument, draft: DraftBody, language: str
    ) -> DraftCheck:
        """What the check of ``draft`` found — read, nothing asked for (task 07c).

        By the axes :meth:`check` asks by: the draft lettered and digested in
        ``language``, which the caller reads as the course's now. A draft
        changed since its check, or read in a language the course has moved
        to, has other axes and reads ``NOT_CHECKED``; so does an unfinished
        one, whose axes nothing asks for — a check and a publication refuse it.
        A pass mark and the author's own explanations move no axis.
        """
        digests = version_digests(published_form(draft, language), language)
        explanation = await self._references.explanation_version(
            document,
            content_digest=digests.content_digest,
            answers_digest=digests.answers_digest,
            language=language,
        )
        return DraftCheck.of(explanation)

    async def current_version(
        self, authored_document_id: uuid.UUID
    ) -> TestVersion | None:
        """The version in force — the latest published — or ``None`` before any."""
        document = await self._require_test_object(authored_document_id)
        return await self._repo.latest_version(document.id)

    async def request_explanations(self, version_id: uuid.UUID, language: str) -> None:
        """Ask for a version's explanations in ``language``, as a submission does.

        A submission's step once its review is out: for the version it was taken
        for — not the newest — in the review's language (task 07b, decisions 13
        and 14). A stuck version is asked for again; a failed one is not, so a
        generation that keeps failing is not paid for once per submission.

        Raises:
            GenerationInProgressError: another job of the task is in flight; the
                caller rolls back, and the next submission asks again.
            RuntimeError: the version is not there — versions go only with their
                test, so this is a broken invariant.
        """
        published = await self._repo.get_version(version_id)
        if published is None:
            msg = f"published version {version_id} is not there"
            raise RuntimeError(msg)
        document = await self._require_test_object(published.authored_document_id)
        await self._references.order_explanations(
            document,
            content_digest=published.content_digest,
            answers_digest=published.answers_digest,
            language=language,
            retry_failed=False,
        )

    async def body_with_axes(
        self,
        document: AuthoredDocument,
        *,
        content_digest: str,
        answers_digest: str,
        language: str | None,
    ) -> PublishedBody | None:
        """The test a version of explanations was asked for, found by its axes.

        The explanation work's question: which body of this test has this
        visible digest and this key's digest (task 07b), asked in ``language``?

        1. A published version with both digests — the newest; every version
           with the same two has the same text and the same key.
        2. Else the draft, which a check asks explanations for before it is
           published (task 07c): lettered in the course's language as it
           stands now, with both digests, and only when the explanations were
           asked for in that very language. A draft changed since, or a course
           whose language changed since, has no body here — explanations nobody
           would read are not paid for.

        Returns:
            The body — its text and its key — or ``None`` when the axes
            describe nothing current.
        """
        published = await self._repo.version_with_digests(
            document.id, content_digest=content_digest, answers_digest=answers_digest
        )
        if published is not None:
            return PublishedBody.from_jsonb(published.body)
        course_language = await self.course_language(document.course_root_id)
        if language != course_language:
            return None
        draft = await self._repo.get_draft(document.id)
        if draft is None:
            return None
        body = published_form(DraftBody.from_jsonb(draft.body), course_language)
        digests = version_digests(body, course_language)
        if (digests.content_digest, digests.answers_digest) != (
            content_digest,
            answers_digest,
        ):
            return None
        return body

    async def _draft(self, document: AuthoredDocument) -> DraftBody:
        draft = await self._repo.get_draft(document.id)
        if draft is None:
            msg = f"test {document.id} has no draft; a test is created with one"
            raise RuntimeError(msg)
        return DraftBody.from_jsonb(draft.body)

    async def _require_test_object(
        self, authored_document_id: uuid.UUID
    ) -> AuthoredDocument:
        document = await self._session.get(AuthoredDocument, authored_document_id)
        if (
            document is None
            or document.deleted_at is not None
            or document.source_type != SourceType.TEST_OBJECT.value
        ):
            raise NotATestObjectError(authored_document_id)
        return document

    async def course_language(self, course_root_id: uuid.UUID) -> str:
        """The course root's language as it stands now.

        A root without a language is a state no author can reach, so its
        absence is not a refusal: the database refuses such a root
        (``course_nodes_root_language_required``, ``parent_id IS NOT NULL OR
        default_language IS NOT NULL``), creating a root requires a language,
        and clearing it on a root is refused with a 422. The column is nullable
        only for the nodes below the root. What remains is a broken invariant,
        raised as one (operator's decision at the stop of commit V1,
        2026-09-25) — and no test pins it, because no test can build it.
        """
        root = await self._session.get(CourseNode, course_root_id)
        language = root.default_language if root is not None else None
        if not language:
            msg = (
                f"the course root {course_root_id} is missing or has no "
                "language; the database forbids both (its foreign key, "
                "course_nodes_root_language_required)"
            )
            raise RuntimeError(msg)
        return language
