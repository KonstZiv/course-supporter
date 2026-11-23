"""The one definition of "the replies to answer", without a database (task 12a, K3).

``pending_replies`` (``homework/remark_comments.py``) asks the version of the
task in force, the attempt ledger's attempts at that version and the store's
unanswered replies on them. Here the first two are doubles and the store is
the in-memory double of the port, so what is pinned is the composition: what
each is asked, in what order, and that a step that finds nothing ends the
read. The filters of the rows themselves — the version, the student, the
live rows — are pinned on a real database (``test_pending_replies_db.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests._helpers.path_config import (
    shipped_with_replies_for,
    shipped_with_todays_mentor_for,
)
from tests._helpers.remark_comments import FakeRemarkComments, a_reply

from course_supporter.homework import remark_comments
from course_supporter.homework.path_config import PathConfig
from course_supporter.homework.remark_comments import pending_replies
from course_supporter.models.source import AssignmentType
from course_supporter.remark_kinds import CommentAuthor, CommentKind

_REPLIES_ON = shipped_with_replies_for(AssignmentType.TASK)
_VERSION = "the-version-in-force"
_TENANT = uuid.uuid4()
_STUDENT = uuid.uuid4()
_TASK = uuid.uuid4()
_ASKING = uuid.uuid4()
_OLDER = uuid.uuid4()
_PREVIOUS = uuid.uuid4()


class _Ledger:
    """The attempt ledger's double: the attempts at a version, as asked."""

    def __init__(self, attempts: list[uuid.UUID]) -> None:
        self.attempts = attempts
        self.asked: list[dict[str, Any]] = []

    async def attempts_at_version(self, **kwargs: Any) -> list[uuid.UUID]:
        self.asked.append(kwargs)
        return list(self.attempts)


@pytest.fixture()
def version() -> Iterator[AsyncMock]:
    with patch.object(
        remark_comments,
        "current_task_version",
        new=AsyncMock(return_value=_VERSION),
    ) as mock:
        yield mock


def _ledger(attempts: list[uuid.UUID]) -> tuple[_Ledger, Any]:
    ledger = _Ledger(attempts)
    return ledger, patch.object(
        remark_comments, "HomeworkRepository", new=MagicMock(return_value=ledger)
    )


async def _pending(
    store: FakeRemarkComments, *, config: PathConfig = _REPLIES_ON
) -> list[uuid.UUID]:
    replies = await pending_replies(
        MagicMock(spec=AsyncSession),
        tenant_id=_TENANT,
        submission_id=_ASKING,
        student_id=_STUDENT,
        authored_document_id=_TASK,
        task_type=AssignmentType.TASK,
        config=config,
        comments=store,
    )
    return [reply.id for reply in replies]


class TestEveryAttemptAtTheVersionInForce:
    async def test_a_reply_to_an_older_attempt_is_picked_up(
        self, version: AsyncMock
    ) -> None:
        """The finding of №686: the reply to the attempt before the previous."""
        waiting = a_reply(_OLDER, minute=0)
        store = FakeRemarkComments([waiting])
        _, patched = _ledger([_OLDER, _PREVIOUS])

        with patched:
            pending = await _pending(store)

        assert pending == [waiting.id]
        assert store.asked_for == [(_OLDER, _PREVIOUS)]

    async def test_the_ledger_is_asked_for_the_version_in_force(
        self, version: AsyncMock
    ) -> None:
        ledger, patched = _ledger([_PREVIOUS])

        with patched:
            await _pending(FakeRemarkComments())

        assert ledger.asked == [
            {
                "tenant_id": _TENANT,
                "student_id": _STUDENT,
                "authored_document_id": _TASK,
                "task_version": _VERSION,
                "excluding": _ASKING,
            }
        ]

    async def test_the_store_is_asked_once_for_the_whole_set(
        self, version: AsyncMock
    ) -> None:
        store = FakeRemarkComments(
            [a_reply(_OLDER, minute=0), a_reply(_PREVIOUS, minute=1)]
        )
        _, patched = _ledger([_OLDER, _PREVIOUS])

        with patched:
            await _pending(store)

        assert store.asked_for == [(_OLDER, _PREVIOUS)]
        assert store.tenants == [_TENANT]


class TestWhatIsNoReplyToAnswer:
    async def test_answered_ones_and_the_mentors_comments(
        self, version: AsyncMock
    ) -> None:
        question = a_reply(_OLDER, minute=0, answered_in=_PREVIOUS)
        mentor = a_reply(
            _PREVIOUS,
            minute=1,
            author=CommentAuthor.MENTOR,
            kind=CommentKind.ANSWER,
            parent_id=question.id,
        )
        waiting = a_reply(_PREVIOUS, minute=2)
        store = FakeRemarkComments([question, mentor, waiting])
        _, patched = _ledger([_OLDER, _PREVIOUS])

        with patched:
            pending = await _pending(store)

        assert pending == [waiting.id]

    async def test_a_reply_on_a_submission_the_ledger_did_not_name(
        self, version: AsyncMock
    ) -> None:
        """Another student's, another version's: the ledger names neither."""
        elsewhere = a_reply(uuid.uuid4())
        _, patched = _ledger([_PREVIOUS])

        with patched:
            pending = await _pending(FakeRemarkComments([elsewhere]))

        assert pending == []

    async def test_no_version_in_force_asks_no_ledger_and_no_store(
        self, version: AsyncMock
    ) -> None:
        version.return_value = None
        store = FakeRemarkComments([a_reply(_PREVIOUS)])
        ledger, patched = _ledger([_PREVIOUS])

        with patched:
            pending = await _pending(store)

        assert pending == []
        assert ledger.asked == []
        assert store.asked_for == []

    async def test_no_attempt_at_the_version_asks_no_store(
        self, version: AsyncMock
    ) -> None:
        store = FakeRemarkComments([a_reply(_PREVIOUS)])
        _, patched = _ledger([])

        with patched:
            pending = await _pending(store)

        assert pending == []
        assert store.asked_for == []

    async def test_a_type_that_takes_no_replies_asks_nothing(
        self, version: AsyncMock
    ) -> None:
        store = FakeRemarkComments([a_reply(_PREVIOUS)])
        ledger, patched = _ledger([_PREVIOUS])

        with patched:
            pending = await _pending(
                store, config=shipped_with_todays_mentor_for(AssignmentType.TASK)
            )

        assert pending == []
        version.assert_not_awaited()
        assert ledger.asked == []
        assert store.asked_for == []


class TestTheOrder:
    async def test_oldest_first_across_the_attempts_each_once(
        self, version: AsyncMock
    ) -> None:
        on_previous = a_reply(_PREVIOUS, minute=0)
        on_older = a_reply(_OLDER, minute=1)
        later_on_previous = a_reply(_PREVIOUS, item_id="c2", minute=2)
        store = FakeRemarkComments([later_on_previous, on_older, on_previous])
        _, patched = _ledger([_OLDER, _PREVIOUS])

        with patched:
            pending = await _pending(store)

        assert pending == [on_previous.id, on_older.id, later_on_previous.id]
