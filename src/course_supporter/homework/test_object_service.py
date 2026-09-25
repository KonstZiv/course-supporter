"""A written test: its draft, its publication, the version in force (task 07b).

Purpose:
    The author writes a test as a draft and publishes it; a student answers
    the version that was published. Between the two stand the decisions of
    PRE-FLIGHT section 8, taken here once: in which language the test is
    lettered, when a publication is a new version, and when it asks for
    explanations and costs money.

Interface:
    :class:`TestObjectService` — save the draft, publish it, read the version
        in force, and ask for a version's explanations as a submission does.
    :class:`Publication` — the version a publication gave, and whether it is
        new.
    :class:`NotATestObjectError` — the document is not a test written in the
        system, or it is gone.

Publishing (section 8.1):
    1. The language is the course root's ``default_language``, read at the
       moment of publishing — not the copy on the document, which is made when
       the test is created and stays behind a course whose language was changed
       since. When the two differ, the document's follows the course in the
       same transaction (operator's decision, 2026-09-25). A root without a
       language cannot exist — see :meth:`TestObjectService._course_language`.
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

Transactions:
    The service flushes and never commits. When a job is asked for, the queue
    commits — ``enqueue_key_explanation`` owns its commit — so the version, the
    language and the job land together. When another job of the task is in
    flight the queue raises
    :class:`~course_supporter.homework.reference_service.GenerationInProgressError`
    and the caller rolls back: nothing of the publication survives, and the
    author publishes again (decision 12).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.homework.reference_service import (
    ExplanationQueue,
    ReferenceService,
)
from course_supporter.homework.test_object import (
    DraftBody,
    published_form,
    version_digests,
)
from course_supporter.models.source import SourceType
from course_supporter.reference_kinds import ReferenceKind
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    TestDraft,
    TestVersion,
)
from course_supporter.storage.test_object_repository import TestObjectRepository


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

    async def save_draft(
        self,
        authored_document_id: uuid.UUID,
        body: DraftBody,
        *,
        title: str | None = None,
    ) -> TestDraft:
        """Replace the draft whole; a title, when given, renames the test at once.

        The body arrives checked. The title is a column of the document, so it
        changes now and no publication carries it (answer 2 of section 13).
        """
        document = await self._require_test_object(authored_document_id)
        if title is not None:
            document.title = title
        return await self._repo.replace_draft(document.id, body.to_jsonb())

    async def publish(self, authored_document_id: uuid.UUID) -> Publication:
        """Publish the draft (section 8.1) — see the module docstring.

        Raises:
            GenerationInProgressError: another job of the task is in flight; the
                caller rolls back.
            RuntimeError: a broken invariant of the database — the course root
                has no language, or the test has no draft.
        """
        document = await self._require_test_object(authored_document_id)
        language = await self._course_language(document)
        draft = await self._repo.get_draft(document.id)
        if draft is None:
            msg = f"test {document.id} has no draft; a test is created with one"
            raise RuntimeError(msg)

        published = published_form(DraftBody.from_jsonb(draft.body), language)
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
            document, version, language, retry_failed=True
        )
        return Publication(version=version, created=created)

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
            document, published, language, retry_failed=False
        )

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

    async def _course_language(self, document: AuthoredDocument) -> str:
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
        root = await self._session.get(CourseNode, document.course_root_id)
        language = root.default_language if root is not None else None
        if not language:
            msg = (
                f"the course root {document.course_root_id} of test {document.id} "
                "is missing or has no language; the database forbids both "
                "(its foreign key, course_nodes_root_language_required)"
            )
            raise RuntimeError(msg)
        return language
