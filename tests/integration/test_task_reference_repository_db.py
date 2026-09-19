"""Storage of a task's reference against a live database (mentor-rebuild task 06).

Every rule this file is about is held by PostgreSQL, not by Python, and that is
the point: ``TASK.md`` invariant 4 says the "same key costs zero model calls"
guarantee must be the database's, so a test that only exercised the repository
would prove the code polite rather than the rule enforced. Each test here has a
matching mutation — drop the index, drop the CHECK, take ``updated_at`` out of
the upsert — recorded in the block report; without one, a green test says only
that nothing happened to be wrong today.

Two tests need their own transactions rather than the shared savepoint session:
``now()`` in PostgreSQL is the time the TRANSACTION started, so two writes in
one transaction leave ``updated_at`` unmoved however correct the code is, and a
race needs two sessions to race at all.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.reference_kinds import ReferenceKind, ReferenceState
from course_supporter.storage.orm import (
    AuthoredDocument,
    TaskReference,
    TaskReferenceOverride,
    Tenant,
)
from course_supporter.storage.task_reference_repository import TaskReferenceRepository
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_ANSWERS_HASH = "c" * 64
_ANSWERS: dict[str, list[str]] = {"1": ["б"], "2": ["в"]}


def _key(doc_id: uuid.UUID, **over: object) -> dict[str, object]:
    """The six-column version key, with named parts overridable per test."""
    key: dict[str, object] = {
        "authored_document_id": doc_id,
        "kind": ReferenceKind.TEST_KEY,
        "source_content_hash": _HASH_A,
        "source_task_type": "test",
        "answers_hash": _ANSWERS_HASH,
        "language": "ukr",
    }
    key.update(over)
    return key


class TestTheKeyIsUniqueWhileItLives:
    async def test_second_live_version_of_the_same_key_is_not_inserted(
        self,
        db_session: AsyncSession,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """The partial unique index refuses a second live version of one key.

        Written as raw SQL on purpose: the repository would hand back the
        existing row, which is the behaviour the next class is about. Here the
        claim is narrower and about the DATABASE — a second row cannot exist.
        """
        repo = TaskReferenceRepository(db_session)
        first, created = await repo.create_version(**_key(seed_material_entry.id))  # type: ignore[arg-type]
        assert created is True
        assert first.state == ReferenceState.PENDING.value

        with pytest.raises(IntegrityError) as exc_info:
            await db_session.execute(
                text(
                    "INSERT INTO task_references (id, authored_document_id, version, "
                    "kind, source_content_hash, source_task_type, answers_hash, "
                    "language, state) VALUES (:id, :doc, 99, 'test_key', :content, "
                    "'test', :answers, 'ukr', 'pending')"
                ),
                {
                    "id": uuid.uuid4(),
                    "doc": seed_material_entry.id,
                    "content": _HASH_A,
                    "answers": _ANSWERS_HASH,
                },
            )
        assert "uq_task_reference_axes_active" in str(exc_info.value)

    async def test_a_failed_version_frees_the_key_for_the_next_attempt(
        self,
        db_session: AsyncSession,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """``WHERE state <> 'failed'`` is what makes a retry possible at all.

        The index is partial for one reason only, and this is it: after a
        generation gives up, the SAME key must be insertable again. Without the
        WHERE the failed row would keep the key forever and the author could
        never retry — which is why this test and the one above must both exist.
        """
        repo = TaskReferenceRepository(db_session)
        first, _ = await repo.create_version(**_key(seed_material_entry.id))  # type: ignore[arg-type]
        await repo.mark_failed(first.id, "ladder exhausted")

        second, created = await repo.create_version(**_key(seed_material_entry.id))  # type: ignore[arg-type]
        assert created is True, "a failed version must not block the same key"
        assert second.id != first.id
        assert second.version == first.version + 1

        rows = (
            (
                await db_session.execute(
                    select(TaskReference).where(
                        TaskReference.authored_document_id == seed_material_entry.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2, "the failed version stays as history"
        assert {r.state for r in rows} == {
            ReferenceState.FAILED.value,
            ReferenceState.PENDING.value,
        }

    async def test_a_different_key_is_a_different_version(
        self,
        db_session: AsyncSession,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Each axis of the key is an axis: change one, get another version."""
        repo = TaskReferenceRepository(db_session)
        base, _ = await repo.create_version(**_key(seed_material_entry.id))  # type: ignore[arg-type]

        for axis, value in (
            ("source_content_hash", _HASH_B),
            ("answers_hash", "d" * 64),
            ("language", "eng"),
        ):
            other, created = await repo.create_version(
                **_key(seed_material_entry.id, **{axis: value})  # type: ignore[arg-type]
            )
            assert created is True, f"{axis} must be part of the key"
            assert other.id != base.id


class TestTheSameKeyArrivingTwiceIsOneVersion:
    async def test_second_request_for_a_live_key_gets_the_existing_version(
        self,
        db_session: AsyncSession,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """No refusal, no second job — the same row, and ``created`` is False."""
        repo = TaskReferenceRepository(db_session)
        first, first_created = await repo.create_version(**_key(seed_material_entry.id))  # type: ignore[arg-type]
        again, again_created = await repo.create_version(**_key(seed_material_entry.id))  # type: ignore[arg-type]

        assert first_created is True
        assert again_created is False, "a repeat must not enqueue a second generation"
        assert again.id == first.id

        count = await db_session.scalar(
            select(func.count())
            .select_from(TaskReference)
            .where(TaskReference.authored_document_id == seed_material_entry.id)
        )
        assert count == 1

    async def test_two_sessions_racing_on_one_key_leave_one_version(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_task: AuthoredDocument,
    ) -> None:
        """Two transactions, one key, one row — and neither caller sees an error.

        The savepoint session cannot show this: a race needs two connections.
        Both sides call the repository the way a route would, and the loser is
        expected to come back with ``created=False`` rather than an exception —
        a refusal here would be an error message about someone else's success
        (ratified 2026-09-19).
        """

        async def attempt() -> bool:
            async with session_factory() as session:
                repo = TaskReferenceRepository(session)
                _, created = await repo.create_version(**_key(committed_task.id))  # type: ignore[arg-type]
                await session.commit()
                return created

        results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
        assert not any(isinstance(r, BaseException) for r in results), results
        assert sorted(bool(r) for r in results) == [False, True], (
            "exactly one of the two racing requests creates the version"
        )

        async with session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(TaskReference)
                .where(TaskReference.authored_document_id == committed_task.id)
            )
        assert count == 1

    async def test_a_taken_version_number_is_retried_not_raised(
        self,
        db_session: AsyncSession,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """A version number lost to someone else costs a retry, never an error.

        The race the repository really has to survive is the one where the
        insert itself collides, and the previous test does not reach it: two
        awaited coroutines in one event loop interleave only where they yield,
        so the second one reads the first one's committed row and never
        inserts. Here the collision is forced instead of hoped for — the first
        ``MAX(version) + 1`` is answered with a number that is already taken,
        as it would be if another author's request had just used it.

        With ``ON CONFLICT DO NOTHING`` the collision comes back as an empty
        result and the loop recomputes; without it the same collision raises
        ``IntegrityError`` and poisons the caller's transaction. That is the
        difference this test exists to hold.
        """
        repo = TaskReferenceRepository(db_session)
        taken, _ = await repo.create_version(
            **_key(seed_material_entry.id, answers_hash="e" * 64)  # type: ignore[arg-type]
        )

        real_next_version = repo._next_version  # the seam under test
        answers: list[int] = []

        async def collide_once(document_id: uuid.UUID) -> int:
            """Answer the first call with a number already in use."""
            value = (
                taken.version if not answers else await real_next_version(document_id)
            )
            answers.append(value)
            return value

        repo._next_version = collide_once  # type: ignore[method-assign]

        created_row, created = await repo.create_version(**_key(seed_material_entry.id))  # type: ignore[arg-type]

        assert created is True, "the collision must not swallow the creation"
        assert answers[0] == taken.version, "the first attempt really did collide"
        assert len(answers) > 1, "and the loop really did recompute"
        assert created_row.version != taken.version

        count = await db_session.scalar(
            select(func.count())
            .select_from(TaskReference)
            .where(TaskReference.authored_document_id == seed_material_entry.id)
        )
        assert count == 2


class TestTheAuthorLayer:
    async def test_the_database_refuses_an_empty_key(
        self,
        db_session: AsyncSession,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """``answers <> '{}'::jsonb`` — an empty key is not a key."""
        with pytest.raises(IntegrityError) as exc_info:
            await db_session.execute(
                text(
                    "INSERT INTO task_reference_overrides (id, authored_document_id, "
                    "kind, answers, source_content_hash) VALUES (:id, :doc, "
                    "'test_key', '{}'::jsonb, :content)"
                ),
                {
                    "id": uuid.uuid4(),
                    "doc": seed_material_entry.id,
                    "content": _HASH_A,
                },
            )
        assert "ck_task_reference_overrides_answers_present" in str(exc_info.value)

    async def test_one_layer_per_task_and_kind(
        self,
        db_session: AsyncSession,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """A replacement replaces: one row, the new answers, ``carried_over`` off."""
        repo = TaskReferenceRepository(db_session)
        await repo.replace_override(
            authored_document_id=seed_material_entry.id,
            kind=ReferenceKind.TEST_KEY,
            answers=_ANSWERS,
            source_content_hash=_HASH_A,
            carried_over=True,
        )
        second = await repo.replace_override(
            authored_document_id=seed_material_entry.id,
            kind=ReferenceKind.TEST_KEY,
            answers={"1": ["а"]},
            source_content_hash=_HASH_B,
        )

        assert second.answers == {"1": ["а"]}
        assert second.source_content_hash == _HASH_B
        assert second.carried_over is False, "replacing clears the carried-over mark"

        count = await db_session.scalar(
            select(func.count())
            .select_from(TaskReferenceOverride)
            .where(TaskReferenceOverride.authored_document_id == seed_material_entry.id)
        )
        assert count == 1

    async def test_clearing_the_layer_reports_whether_there_was_one(
        self,
        db_session: AsyncSession,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """Absence IS the "waiting for a key" state — clearing twice is no error."""
        repo = TaskReferenceRepository(db_session)
        await repo.replace_override(
            authored_document_id=seed_material_entry.id,
            kind=ReferenceKind.TEST_KEY,
            answers=_ANSWERS,
            source_content_hash=_HASH_A,
        )
        assert await repo.clear_override(seed_material_entry.id, ReferenceKind.TEST_KEY)
        assert (
            await repo.get_override(seed_material_entry.id, ReferenceKind.TEST_KEY)
            is None
        )
        assert not await repo.clear_override(
            seed_material_entry.id, ReferenceKind.TEST_KEY
        )

    async def test_a_replacement_moves_updated_at(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_task: AuthoredDocument,
    ) -> None:
        """``updated_at`` is set explicitly in the upsert — proven across two commits.

        Two separate transactions, because ``now()`` is the transaction's start
        time: inside one transaction the timestamp cannot move whatever the code
        does, and the test would pass over the bug rather than through it (the
        lesson task 05 recorded).
        """
        async with session_factory() as session:
            first = await TaskReferenceRepository(session).replace_override(
                authored_document_id=committed_task.id,
                kind=ReferenceKind.TEST_KEY,
                answers=_ANSWERS,
                source_content_hash=_HASH_A,
            )
            created_at, first_updated = first.created_at, first.updated_at
            await session.commit()

        async with session_factory() as session:
            second = await TaskReferenceRepository(session).replace_override(
                authored_document_id=committed_task.id,
                kind=ReferenceKind.TEST_KEY,
                answers={"1": ["г"]},
                source_content_hash=_HASH_A,
            )
            await session.commit()
            assert second.updated_at > first_updated, "the upsert must move updated_at"
            assert second.created_at == created_at, "and must not move created_at"


@pytest.fixture()
async def committed_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AuthoredDocument]:
    """A committed task the racing / two-transaction tests can both see.

    The savepoint ``db_session`` is invisible to a second connection, so these
    rows are committed for real and deleted afterwards; the document cascade
    takes both reference tables with it.
    """
    async with session_factory() as session:
        tenant = Tenant(name=f"ref-tenant-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = make_root_course_node(
            tenant_id=tenant.id, title="Reference race course", order=0
        )
        session.add(node)
        await session.flush()
        doc = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="web",
            source_url="https://example.com/reference-race",
            task_type="test",
        )
        session.add(doc)
        await session.commit()

    yield doc

    async with session_factory() as session:
        await session.execute(
            text("DELETE FROM tenants WHERE id = :tenant"), {"tenant": tenant.id}
        )
        await session.commit()
