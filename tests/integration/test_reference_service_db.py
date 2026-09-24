"""What a key costs and when it carries over (mentor-rebuild task 06, block B2).

The claims here are about money and about the author's work, and both need a
real database: "the same key costs zero generations" is enforced by a partial
unique index, and "the answers carry onto a new task version" is a write that a
read performs. A test with a mocked repository would prove neither.

The generation seam is the one double, and it counts rather than pretends: every
assertion about cost is a number of requests recorded by
:class:`_CountingQueue`, not an absence of a call to a mock.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.homework.reference_key import answers_digest
from course_supporter.homework.reference_service import (
    ReferenceRefusedError,
    ReferenceService,
    ReferenceStatus,
    RefusalCode,
)
from course_supporter.reference_kinds import ReferenceKind
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
    TaskReference,
    TaskReferenceOverride,
)
from course_supporter.storage.task_reference_repository import TaskReferenceRepository

pytestmark = pytest.mark.requires_db

_KEY: dict[str, list[str]] = {"1": ["б"], "2": ["в"], "3": ["а"]}
_TEXT_V1 = "Тест\n\n1. Перше?\nа) так\nб) ні\n\n2. Друге?\nв) так\n\n3. Третє?\nа) так"
_TEXT_V2_SAME_NUMBERS = _TEXT_V1.replace("Перше?", "Перше питання, уточнене?")
_TEXT_V3_FEWER_NUMBERS = "Тест\n\n1. Перше?\nа) так\n\n2. Друге?\nв) так"


@dataclass
class _CountingQueue:
    """The generation seam, counting what it was asked to do.

    Not a mock: it satisfies the protocol and records the pairs it was handed,
    so a test asserts a NUMBER of requested generations rather than the absence
    of a call on an object that would have accepted anything.
    """

    requests: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)

    async def request(
        self, *, authored_document_id: uuid.UUID, reference_id: uuid.UUID
    ) -> None:
        self.requests.append((authored_document_id, reference_id))


async def _test_task(
    session: AsyncSession,
    root: CourseNode,
    *,
    text: str,
    language: str | None = "ukr",
    task_type: str | None = "test",
    content_hash: str | None = "v1" + "0" * 62,
) -> AuthoredDocument:
    """A task whose text the mentor pipeline would assemble from one segment."""
    document = AuthoredDocument(
        course_node_id=root.id,
        course_root_id=root.id,
        source_type="text",
        source_url="file:///tmp/test.md",
        task_type=task_type,
        language=language,
        content_hash=content_hash,
    )
    session.add(document)
    await session.flush()

    summary = DocumentSummary(
        authored_document_id=document.id,
        course_root_id=root.id,
        title="Тест",
        status="ready",
    )
    session.add(summary)
    await session.flush()
    session.add(
        DocumentSegment(
            document_summary_id=summary.id,
            course_root_id=root.id,
            order=0,
            content=text,
            description="the whole test",
            start_pos=0,
            end_pos=len(text),
        )
    )
    await session.flush()
    return document


def _service(session: AsyncSession) -> tuple[ReferenceService, _CountingQueue]:
    queue = _CountingQueue()
    return ReferenceService(session, queue), queue


class TestWhatAKeyCosts:
    async def test_the_same_key_twice_costs_one_generation(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Invariant 4, end to end: the second replacement requests nothing."""
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)

        first = await service.replace_key(document.id, _KEY)
        assert first.status is ReferenceStatus.GENERATING
        assert len(queue.requests) == 1

        second = await service.replace_key(document.id, dict(_KEY))
        assert second.version == first.version
        assert len(queue.requests) == 1, "the same key must not be generated twice"

    async def test_changed_answers_cost_exactly_one_more_generation(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """The answers are an axis of the version key — change one, pay once."""
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)

        await service.replace_key(document.id, _KEY)
        changed = dict(_KEY) | {"3": ["б"]}
        after = await service.replace_key(document.id, changed)

        assert len(queue.requests) == 2, "a changed answer is a new version"
        assert after.answers == changed

    async def test_reordered_answers_cost_nothing(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """The digest is canonical, so a shuffled key is the same key."""
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)

        await service.replace_key(document.id, _KEY)
        await service.replace_key(document.id, {"3": ["а"], "1": ["б"], "2": ["в"]})

        assert len(queue.requests) == 1

    async def test_an_author_explanation_alone_costs_nothing(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Editing the author's own explanation is not a new generation."""
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)

        await service.replace_key(document.id, _KEY)
        view = await service.replace_key(
            document.id, dict(_KEY), author_explanations={"1": "бо так"}
        )

        assert len(queue.requests) == 1
        assert view.explanations["1"] == "бо так"


class TestCarryingTheKeyToANewTaskVersion:
    async def test_the_same_question_numbers_carry_the_answers_over(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """A text edit that keeps the numbering keeps the key — marked as carried."""
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)
        await service.replace_key(document.id, _KEY)

        await _revise_text(db_session, document, _TEXT_V2_SAME_NUMBERS, "v2" + "0" * 62)

        view = await service.read(document.id)
        assert view.status is ReferenceStatus.GENERATING
        assert view.carried_over is True, "the author did not send this key for v2"
        assert view.answers == _KEY
        assert len(queue.requests) == 2, "the new version needs its own explanations"

    async def test_a_changed_question_set_leaves_the_task_awaiting_a_key(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Different numbers: the key is about other questions — no carry, no cost."""
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)
        await service.replace_key(document.id, _KEY)

        await _revise_text(
            db_session, document, _TEXT_V3_FEWER_NUMBERS, "v3" + "0" * 62
        )

        view = await service.read(document.id)
        assert view.status is ReferenceStatus.AWAITING_KEY
        assert len(queue.requests) == 1, "an unusable key must not be generated for"

        override = await TaskReferenceRepository(db_session).get_override(
            document.id, ReferenceKind.TEST_KEY
        )
        assert override is not None, "the author's work is not thrown away on an edit"

    async def test_replacing_the_key_clears_the_carried_over_mark(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Once the author sends a key FOR this version, it is no longer carried."""
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, _ = _service(db_session)
        await service.replace_key(document.id, _KEY)
        await _revise_text(db_session, document, _TEXT_V2_SAME_NUMBERS, "v2" + "0" * 62)
        assert (await service.read(document.id)).carried_over is True

        after = await service.replace_key(document.id, dict(_KEY))
        assert after.carried_over is False


class TestReadingAndClearing:
    async def test_a_task_without_a_key_is_awaiting_one(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)

        view = await service.read(document.id)
        assert view.status is ReferenceStatus.AWAITING_KEY
        assert view.answers == {}
        assert not queue.requests

    async def test_the_author_explanation_wins_over_the_generated_one(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Per question, not per set: the author overrides the ones they wrote."""
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, _ = _service(db_session)
        await service.replace_key(document.id, _KEY, author_explanations={"2": "автор"})

        repo = TaskReferenceRepository(db_session)
        version = await repo.get_live_version(
            authored_document_id=document.id,
            kind=ReferenceKind.TEST_KEY,
            source_content_hash=document.content_hash or "",
            source_task_type="test",
            answers_hash=answers_digest(_KEY),
            language="ukr",
        )
        assert version is not None
        await repo.mark_ready(version.id, {"1": "машина", "2": "машина", "3": "машина"})

        view = await service.read(document.id)
        assert view.status is ReferenceStatus.READY
        assert view.explanations == {"1": "машина", "2": "автор", "3": "машина"}

    async def test_a_failed_version_is_reported_with_its_reason(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, _ = _service(db_session)
        await service.replace_key(document.id, _KEY)

        repo = TaskReferenceRepository(db_session)
        version = await repo.get_live_version(
            authored_document_id=document.id,
            kind=ReferenceKind.TEST_KEY,
            source_content_hash=document.content_hash or "",
            source_task_type="test",
            answers_hash=answers_digest(_KEY),
            language="ukr",
        )
        assert version is not None
        await repo.mark_failed(version.id, "ladder out")

        view = await service.read(document.id)
        assert view.status is ReferenceStatus.FAILED
        assert view.failure_reason == "ladder out"

    async def test_clearing_returns_the_task_to_awaiting_a_key(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)
        await service.replace_key(document.id, _KEY)

        assert await service.clear_key(document.id) is True
        assert (await service.read(document.id)).status is ReferenceStatus.AWAITING_KEY
        assert await service.clear_key(document.id) is False

        again = await service.replace_key(document.id, dict(_KEY))
        assert again.status is ReferenceStatus.GENERATING
        assert len(queue.requests) == 1, "the generated version outlived the key"

    async def test_the_same_key_after_a_failure_buys_a_retry(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """A failed generation must be retryable, and the retry is the same key.

        This is the author's ONLY way back from a failure: they send the key
        they already sent. If the service treated the failed version as an
        existing one, that request would cost nothing and change nothing, and
        the task would sit in "failed" forever with no way out of it. The
        storage test proves the index allows the row; this one proves the
        service actually asks for it.
        """
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)
        await service.replace_key(document.id, _KEY)

        repo = TaskReferenceRepository(db_session)
        failed = await repo.get_live_version(
            authored_document_id=document.id,
            kind=ReferenceKind.TEST_KEY,
            source_content_hash=document.content_hash or "",
            source_task_type="test",
            answers_hash=answers_digest(_KEY),
            language="ukr",
        )
        assert failed is not None
        await repo.mark_failed(failed.id, "ladder exhausted")
        assert (await service.read(document.id)).status is ReferenceStatus.FAILED

        retried = await service.replace_key(document.id, dict(_KEY))

        assert len(queue.requests) == 2, "the retry must request a generation"
        assert retried.status is ReferenceStatus.GENERATING
        assert retried.version == failed.version + 1
        assert queue.requests[-1][1] != failed.id, "and it must be a NEW version"


class TestEachRefusalHasItsOwnCode:
    async def test_not_a_test_task(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _test_task(
            db_session, seed_root_node, text=_TEXT_V1, task_type="project"
        )
        service, _ = _service(db_session)
        with pytest.raises(ReferenceRefusedError) as exc:
            await service.replace_key(document.id, _KEY)
        assert exc.value.code is RefusalCode.NOT_A_TEST_TASK

    async def test_task_not_ready(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _test_task(
            db_session, seed_root_node, text=_TEXT_V1, content_hash=None
        )
        service, _ = _service(db_session)
        with pytest.raises(ReferenceRefusedError) as exc:
            await service.replace_key(document.id, _KEY)
        assert exc.value.code is RefusalCode.TASK_NOT_READY

    async def test_task_language_unset_is_not_task_not_ready(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Its own code: the task IS processed, so "wait" would be the wrong advice."""
        document = await _test_task(
            db_session, seed_root_node, text=_TEXT_V1, language=None
        )
        service, _ = _service(db_session)
        with pytest.raises(ReferenceRefusedError) as exc:
            await service.replace_key(document.id, _KEY)
        assert exc.value.code is RefusalCode.TASK_LANGUAGE_UNSET

    async def test_no_question_numbers(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _test_task(
            db_session, seed_root_node, text="Тест\n\nа) так\nб) ні"
        )
        service, _ = _service(db_session)
        with pytest.raises(ReferenceRefusedError) as exc:
            await service.replace_key(document.id, _KEY)
        assert exc.value.code is RefusalCode.NO_QUESTION_NUMBERS

    async def test_task_text_truncated(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """An over-budget source text is refused, read through the real loader.

        The tail alone is past the 512 KiB budget, so the refusal is about the
        length of the text the service reads, not about a marker in it (task 07
        reads the source text, which carries none).
        """
        long_first = "1. Перше?" + "x" * 600
        document = await _test_task(db_session, seed_root_node, text=long_first)
        summary_id = await db_session.scalar(
            DocumentSummary.__table__.select()
            .with_only_columns(DocumentSummary.id)
            .where(DocumentSummary.authored_document_id == document.id)
        )
        db_session.add(
            DocumentSegment(
                document_summary_id=summary_id,
                course_root_id=seed_root_node.id,
                order=1,
                content="2. Друге?" + "y" * 600_000,
                description="oversize tail",
                start_pos=0,
                end_pos=10,
            )
        )
        await db_session.flush()

        service, _ = _service(db_session)
        with pytest.raises(ReferenceRefusedError) as exc:
            await service.replace_key(document.id, _KEY)
        assert exc.value.code is RefusalCode.TASK_TEXT_TRUNCATED

    async def test_key_does_not_match_questions(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)
        with pytest.raises(ReferenceRefusedError) as exc:
            await service.replace_key(document.id, {"1": ["б"], "9": ["а"]})
        assert exc.value.code is RefusalCode.KEY_DOES_NOT_MATCH_QUESTIONS
        assert "missing" in exc.value.details and "unknown" in exc.value.details
        assert not queue.requests, "a refused key costs nothing"


class TestThePassMark:
    """Task 07: the author's pass mark travels with the layer, and costs nothing."""

    async def test_the_pass_mark_is_stored_with_the_key_and_read_back(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, _ = _service(db_session)

        written = await service.replace_key(document.id, _KEY, pass_threshold=80)

        assert written.pass_threshold == 80
        assert (await service.read(document.id)).pass_threshold == 80

    async def test_a_new_pass_mark_alone_asks_for_nothing_and_omitting_it_clears_it(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Outside the version key: the mark changes, the generation does not."""
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)
        await service.replace_key(document.id, _KEY, pass_threshold=80)

        changed = await service.replace_key(document.id, dict(_KEY), pass_threshold=60)
        cleared = await service.replace_key(document.id, dict(_KEY))

        assert changed.pass_threshold == 60
        assert cleared.pass_threshold is None, "the layer is replaced whole"
        assert len(queue.requests) == 1, "the mark is not a new generation"

    async def test_the_carry_over_keeps_the_pass_mark(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """The layer moves whole onto a new text — its pass mark included."""
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, _ = _service(db_session)
        await service.replace_key(document.id, _KEY, pass_threshold=80)
        await _revise_text(db_session, document, _TEXT_V2_SAME_NUMBERS, "v2" + "0" * 62)

        view = await service.read(document.id)

        assert view.carried_over is True, "the premise: carried, not sent again"
        assert view.pass_threshold == 80
        stored = await TaskReferenceRepository(db_session).get_override(
            document.id, ReferenceKind.TEST_KEY
        )
        assert stored is not None
        assert stored.pass_threshold == 80


class TestWhetherTheKeyApplies:
    """The doors' question (task 07, decision 12) — answered without writing."""

    async def test_no_key_does_not_apply(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, _ = _service(db_session)

        assert await service.key_applies(document.id) is False

    async def test_a_key_written_for_the_current_text_applies(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, _ = _service(db_session)
        await service.replace_key(document.id, _KEY)

        assert await service.key_applies(document.id) is True

    async def test_a_key_that_would_carry_over_applies_and_nothing_is_written(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Rows, the layer and the queue are the same before and after.

        The premise is asserted as well: the same state, READ, does write — the
        layer moves and one more version is asked for — so the footprint below
        is one that sees a write when there is one.
        """
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)
        await service.replace_key(document.id, _KEY)
        await _revise_text(db_session, document, _TEXT_V2_SAME_NUMBERS, "v2" + "0" * 62)
        before = await _footprint(db_session, document.id)
        requests_before = len(queue.requests)

        assert await service.key_applies(document.id) is True
        assert await _footprint(db_session, document.id) == before
        assert len(queue.requests) == requests_before

        await service.read(document.id)
        assert await _footprint(db_session, document.id) != before
        assert len(queue.requests) == requests_before + 1

    async def test_a_key_for_other_questions_does_not_apply_and_nothing_is_written(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)
        await service.replace_key(document.id, _KEY)
        await _revise_text(
            db_session, document, _TEXT_V3_FEWER_NUMBERS, "v3" + "0" * 62
        )
        before = await _footprint(db_session, document.id)
        requests_before = len(queue.requests)

        assert await service.key_applies(document.id) is False
        assert await _footprint(db_session, document.id) == before
        assert len(queue.requests) == requests_before


class TestExplanationsInOneLanguage:
    """What a review reads (task 07): the model's, the author's and the doubts apart."""

    async def test_the_model_the_author_and_the_doubts_stay_apart(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """The author's view lets the author win; a review is shown both sides."""
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, _ = _service(db_session)
        await service.replace_key(
            document.id, _KEY, author_explanations={"2": "автор"}, pass_threshold=80
        )
        version = await _live_version(db_session, document, "ukr")
        await TaskReferenceRepository(db_session).mark_ready(
            version.id,
            {"1": "машина", "2": "машина", "3": "машина"},
            doubts={"2": True},
        )

        seen = await service.explanations_for(document.id, "ukr")

        assert seen.answers == _KEY
        assert seen.pass_threshold == 80
        assert seen.model == {"1": "машина", "2": "машина", "3": "машина"}
        assert seen.author == {"2": "автор"}
        assert seen.doubts == {"2": True}

        view = await service.read(document.id)
        assert view.explanations == {"1": "машина", "2": "автор", "3": "машина"}
        assert view.doubts == {"2": True}

    async def test_each_language_is_read_from_its_own_version(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, _ = _service(db_session)
        await service.replace_key(document.id, _KEY)
        repo = TaskReferenceRepository(db_session)
        await repo.mark_ready(
            (await _live_version(db_session, document, "ukr")).id,
            {"1": "укр", "2": "укр", "3": "укр"},
            doubts={"1": True},
        )
        english, _ = await repo.create_version(
            authored_document_id=document.id,
            kind=ReferenceKind.TEST_KEY,
            source_content_hash=document.content_hash or "",
            source_task_type="test",
            answers_hash=answers_digest(_KEY),
            language="eng",
        )
        await repo.mark_ready(
            english.id, {"1": "eng", "2": "eng", "3": "eng"}, doubts={}
        )

        in_english = await service.explanations_for(document.id, "eng")
        in_ukrainian = await service.explanations_for(document.id, "ukr")

        assert in_english.model == {"1": "eng", "2": "eng", "3": "eng"}
        assert in_english.doubts == {}
        assert in_ukrainian.model == {"1": "укр", "2": "укр", "3": "укр"}
        assert in_ukrainian.doubts == {"1": True}

    async def test_a_language_without_a_version_has_the_key_but_no_model_words(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        """Reading creates no version in that language and asks for nothing."""
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)
        await service.replace_key(document.id, _KEY, author_explanations={"2": "автор"})
        before = await _footprint(db_session, document.id)

        seen = await service.explanations_for(document.id, "eng")

        assert seen.answers == _KEY
        assert seen.author == {"2": "автор"}
        assert (seen.model, seen.doubts) == ({}, {})
        assert await _footprint(db_session, document.id) == before
        assert len(queue.requests) == 1, "only the course language was asked for"

    async def test_no_key_reads_as_no_answers(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, _ = _service(db_session)

        seen = await service.explanations_for(document.id, "ukr")

        assert seen.language == "ukr"
        assert (seen.answers, seen.model, seen.author) == ({}, {}, {})

    async def test_it_carries_the_key_over_as_the_authors_read_does(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        document = await _test_task(db_session, seed_root_node, text=_TEXT_V1)
        service, queue = _service(db_session)
        await service.replace_key(document.id, _KEY)
        await _revise_text(db_session, document, _TEXT_V2_SAME_NUMBERS, "v2" + "0" * 62)

        seen = await service.explanations_for(document.id, "ukr")

        assert seen.answers == _KEY
        assert len(queue.requests) == 2, "the new version asked for its explanations"


async def _live_version(
    session: AsyncSession, document: AuthoredDocument, language: str
) -> TaskReference:
    """The live version of ``_KEY`` for the task's current text, in ``language``."""
    version = await TaskReferenceRepository(session).get_live_version(
        authored_document_id=document.id,
        kind=ReferenceKind.TEST_KEY,
        source_content_hash=document.content_hash or "",
        source_task_type="test",
        answers_hash=answers_digest(_KEY),
        language=language,
    )
    assert version is not None
    return version


async def _footprint(
    session: AsyncSession, authored_document_id: uuid.UUID
) -> tuple[int, tuple[str, bool] | None]:
    """What a write to a task's reference would change: its versions and its layer.

    Read with plain queries, not through loaded objects, so it reports the rows
    as the database holds them.
    """
    versions = await session.scalar(
        select(func.count())
        .select_from(TaskReference)
        .where(TaskReference.authored_document_id == authored_document_id)
    )
    layer = (
        await session.execute(
            select(
                TaskReferenceOverride.source_content_hash,
                TaskReferenceOverride.carried_over,
            ).where(TaskReferenceOverride.authored_document_id == authored_document_id)
        )
    ).one_or_none()
    return int(versions or 0), (layer[0], layer[1]) if layer is not None else None


async def _revise_text(
    session: AsyncSession,
    document: AuthoredDocument,
    text: str,
    content_hash: str,
) -> None:
    """Re-author the task: new segment content and a new content hash.

    What ingestion does, reduced to what this module can see — the text changes
    and the hash moves. Nothing here needs the pipeline itself, which is the
    point of the lazy path: the service notices on the next read.
    """
    summary_id = await session.scalar(
        DocumentSummary.__table__.select()
        .with_only_columns(DocumentSummary.id)
        .where(DocumentSummary.authored_document_id == document.id)
    )
    await session.execute(
        DocumentSegment.__table__.update()
        .where(DocumentSegment.document_summary_id == summary_id)
        .values(content=text)
    )
    document.content_hash = content_hash
    await session.flush()
