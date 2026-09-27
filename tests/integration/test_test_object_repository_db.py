"""The storage of a test written in the system (mentor-rebuild task 07b, commit A2).

Requires ``docker compose up -d``; run with ``--run-db``.

The repository is handed digests, not a test: computing them is
``homework.test_object``'s job. So the digests here are plain markers — what
matters is which of them repeat.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    Tenant,
    TestDraft,
    TestVersion,
)
from course_supporter.storage.test_object_repository import TestObjectRepository
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


def _body(answer: str = "б") -> dict[str, Any]:
    return {
        "pass_threshold": None,
        "questions": [
            {
                "number": "1",
                "text": "Що виведе print(2 ** 3)?",
                "options": [
                    {"label": "а", "text": "6", "correct": answer == "а"},
                    {"label": "б", "text": "8", "correct": answer == "б"},
                ],
                "explanation": None,
            }
        ],
    }


def _publication(
    content: str = "c", answers: str = "k", publication: str = "p"
) -> dict[str, Any]:
    return {
        "language": "ukr",
        "body": _body(),
        "content_digest": content * 64,
        "answers_digest": answers * 64,
        "publication_digest": publication * 64,
    }


def _a_test(
    node_id: uuid.UUID, title: str | None = "Тест до лекції 3"
) -> AuthoredDocument:
    """A test written in the system: no file, never processed."""
    return AuthoredDocument(
        course_node_id=node_id,
        course_root_id=node_id,
        source_type="test_object",
        source_url="test-object:",
        task_type="test",
        language="ukr",
        title=title,
    )


@pytest.fixture()
async def written_test(
    db_session: AsyncSession, seed_root_node: CourseNode
) -> AuthoredDocument:
    document = _a_test(seed_root_node.id)
    db_session.add(document)
    await db_session.flush()
    return document


async def _versions(session: AsyncSession, document_id: uuid.UUID) -> list[int]:
    stmt = (
        select(TestVersion.version)
        .where(TestVersion.authored_document_id == document_id)
        .order_by(TestVersion.version)
    )
    return list((await session.execute(stmt)).scalars())


class TestPublication:
    async def test_a_draft_identical_to_the_latest_publishes_nothing(
        self, db_session: AsyncSession, written_test: AuthoredDocument
    ) -> None:
        repo = TestObjectRepository(db_session)

        first, created = await repo.publish(written_test.id, **_publication())
        again, created_again = await repo.publish(written_test.id, **_publication())

        assert (created, created_again) == (True, False)
        assert (again.id, again.version) == (first.id, 1)
        assert await _versions(db_session, written_test.id) == [1]

    async def test_a_return_to_an_earlier_state_is_a_new_current_version(
        self, db_session: AsyncSession, written_test: AuthoredDocument
    ) -> None:
        """A → B → A: three versions, and the third — A again — is current.

        Decision 6 as clarified 2026-09-25: a digest unique among all versions
        would have handed back version 1 and left B current, with no way back.
        """
        repo = TestObjectRepository(db_session)
        a = _publication(publication="a")
        b = {**_publication(answers="m", publication="b"), "body": _body("а")}

        v1, _ = await repo.publish(written_test.id, **a)
        v2, _ = await repo.publish(written_test.id, **b)
        v3, created = await repo.publish(written_test.id, **a)

        assert created is True
        assert (v1.version, v2.version, v3.version) == (1, 2, 3)
        latest = await repo.latest_version(written_test.id)
        assert latest is not None
        assert latest.id == v3.id
        assert v3.body == v1.body
        assert v3.publication_digest == v1.publication_digest
        assert await _versions(db_session, written_test.id) == [1, 2, 3]

    async def test_a_new_pass_mark_is_a_new_version_of_the_same_test(
        self, db_session: AsyncSession, written_test: AuthoredDocument
    ) -> None:
        """Only the whole publication's digest changes — the same test for the
        student, still a version of its own (decision 9)."""
        repo = TestObjectRepository(db_session)

        first, _ = await repo.publish(written_test.id, **_publication())
        second, created = await repo.publish(
            written_test.id, **_publication(publication="q")
        )

        assert created is True
        assert (first.version, second.version) == (1, 2)
        assert second.content_digest == first.content_digest

    async def test_versions_are_numbered_per_test(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        written_test: AuthoredDocument,
    ) -> None:
        other = _a_test(seed_root_node.id, title=None)
        db_session.add(other)
        await db_session.flush()
        repo = TestObjectRepository(db_session)

        mine, _ = await repo.publish(written_test.id, **_publication())
        theirs, created = await repo.publish(other.id, **_publication())

        assert created is True
        assert (mine.version, theirs.version) == (1, 1)
        assert theirs.id != mine.id

    async def test_a_version_is_found_by_its_id(
        self, db_session: AsyncSession, written_test: AuthoredDocument
    ) -> None:
        repo = TestObjectRepository(db_session)
        published, _ = await repo.publish(written_test.id, **_publication())

        found = await repo.get_version(published.id)

        assert found is not None
        assert (found.version, found.language, found.body) == (1, "ukr", _body())

    async def test_nothing_is_published_before_the_first_publication(
        self, db_session: AsyncSession, written_test: AuthoredDocument
    ) -> None:
        repo = TestObjectRepository(db_session)

        assert await repo.latest_version(written_test.id) is None
        assert await repo.get_draft(written_test.id) is None


@pytest.fixture()
async def committed_test(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[uuid.UUID]:
    """A test with version 1 published and committed — a race needs two
    sessions, and two sessions see only what is committed."""
    async with session_factory() as session:
        tenant = Tenant(name=f"test-object-race-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = make_root_course_node(tenant_id=tenant.id, title="Race", order=0)
        session.add(node)
        await session.flush()
        document = _a_test(node.id)
        session.add(document)
        await session.flush()
        await TestObjectRepository(session).publish(document.id, **_publication())
        await session.commit()
        ids = (tenant.id, node.id, document.id)

    yield ids[2]

    async with session_factory() as session:
        # The versions go with their test (FK ON DELETE CASCADE).
        await session.execute(
            delete(AuthoredDocument).where(AuthoredDocument.id == ids[2])
        )
        await session.execute(delete(CourseNode).where(CourseNode.id == ids[1]))
        await session.execute(delete(Tenant).where(Tenant.id == ids[0]))
        await session.commit()


async def _race(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    document_id: uuid.UUID,
    publications: tuple[dict[str, Any], dict[str, Any]],
) -> list[tuple[int, bool]]:
    """Two publications in two sessions, both past their first read of the
    latest version before either inserts — so they meet on one number."""
    barrier = asyncio.Barrier(2)
    read_latest = TestObjectRepository.latest_version
    first_read: set[int] = set()

    async def read_then_meet(
        self: TestObjectRepository, authored_document_id: uuid.UUID
    ) -> TestVersion | None:
        latest = await read_latest(self, authored_document_id)
        if id(self) not in first_read:
            first_read.add(id(self))
            await barrier.wait()
        return latest

    monkeypatch.setattr(TestObjectRepository, "latest_version", read_then_meet)

    async def publish(publication: dict[str, Any]) -> tuple[int, bool]:
        async with session_factory() as session:
            version, created = await TestObjectRepository(session).publish(
                document_id, **publication
            )
            await session.commit()
            return version.version, created

    # A bound, so a broken race fails the test instead of hanging the gate.
    first, second = await asyncio.wait_for(
        asyncio.gather(*(publish(p) for p in publications)), timeout=30
    )
    return [first, second]


class TestPublicationRace:
    async def test_two_different_publications_take_two_numbers(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        committed_test: uuid.UUID,
    ) -> None:
        results = await _race(
            session_factory,
            monkeypatch,
            committed_test,
            (_publication(publication="x"), _publication(publication="y")),
        )

        assert sorted(number for number, _ in results) == [2, 3]
        assert all(created for _, created in results)
        async with session_factory() as session:
            assert await _versions(session, committed_test) == [1, 2, 3]

    async def test_two_identical_publications_make_one_version(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        committed_test: uuid.UUID,
    ) -> None:
        results = await _race(
            session_factory,
            monkeypatch,
            committed_test,
            (_publication(publication="x"), _publication(publication="x")),
        )

        assert sorted(results) == [(2, False), (2, True)]
        async with session_factory() as session:
            assert await _versions(session, committed_test) == [1, 2]


class TestDraftStorage:
    async def test_the_draft_is_one_row_replaced_whole(
        self, db_session: AsyncSession, written_test: AuthoredDocument
    ) -> None:
        repo = TestObjectRepository(db_session)
        changed = {**_body(), "pass_threshold": 80}

        await repo.replace_draft(written_test.id, _body())
        await repo.replace_draft(written_test.id, changed)

        draft = await repo.get_draft(written_test.id)
        assert draft is not None
        assert draft.body == changed
        rows = select(func.count()).where(
            TestDraft.authored_document_id == written_test.id
        )
        assert int((await db_session.execute(rows)).scalar_one()) == 1
