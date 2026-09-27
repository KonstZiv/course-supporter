"""Reads and writes of a test written in the system: its draft and versions (task 07b).

Not tenant-scoped: a test belongs to an ``AuthoredDocument`` whose tenant the
route resolves before calling in here (the ``TaskReferenceRepository`` and
``ProjectBaseRepository`` arrangement, same reason). The repository flushes and
never commits — the caller owns the transaction boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import TestDraft, TestVersion

_PUBLISH_ATTEMPTS: Final[int] = 3
"""How many times :meth:`TestObjectRepository.publish` reads the latest version.

Two publications of one test racing for the same number both try it; the
loser's insert comes back empty and it reads the latest version again — now the
winner's. Three, because the contention is between an author's own requests:
the second attempt already sees the winner's row, and the third exists only so
a pathological interleaving is a raised error rather than a silent ``None``.
"""


class TestObjectRepository:
    """Storage for a test written in the system: one draft, published versions."""

    __test__ = False  # a repository, not a pytest class, whatever its name says

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── the draft ─────────────────────────────────────────────────────────

    async def get_draft(self, authored_document_id: uuid.UUID) -> TestDraft | None:
        """The test's draft, or ``None`` when it has none."""
        stmt = select(TestDraft).where(
            TestDraft.authored_document_id == authored_document_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def replace_draft(
        self, authored_document_id: uuid.UUID, body: dict[str, Any]
    ) -> TestDraft:
        """Write the draft whole — the first one, or over the last.

        One statement, ``ON CONFLICT`` on ``uq_test_draft_document``: a draft
        has no history, so the old body simply gives way. The body arrives
        checked; nothing here looks inside it.
        """
        stmt = (
            pg_insert(TestDraft)
            .values(authored_document_id=authored_document_id, body=body)
            .on_conflict_do_update(
                index_elements=["authored_document_id"],
                set_={"body": body, "updated_at": func.now()},
            )
            .returning(TestDraft)
            .execution_options(populate_existing=True)
        )
        draft = (await self._session.execute(stmt)).scalar_one()
        await self._session.flush()
        return draft

    # ── the published versions ────────────────────────────────────────────

    async def publish(
        self,
        authored_document_id: uuid.UUID,
        *,
        language: str,
        body: dict[str, Any],
        content_digest: str,
        answers_digest: str,
        publication_digest: str,
    ) -> tuple[TestVersion, bool]:
        """Publish the next version — unless this publication IS the current one.

        Returns ``(version, created)``. The publication is compared with the
        LATEST version only. Identical to it: nothing is created and the latest
        comes back — "publishing an identical draft costs zero versions" (task
        07b, acceptance 2). Anything else: a new version, current from now on,
        even when it repeats an earlier one — returning to an earlier state is
        a publication like any other (decision 6, clarified 2026-09-25), which
        a digest unique among ALL versions would have made impossible.

        Two publications racing for one number are settled by the database. The
        insert carries ``ON CONFLICT DO NOTHING`` with no arbiter named, as
        ``TaskReferenceRepository.create_version`` does: the only unique rule it
        can meet besides the key is ``uq_test_version_document_version``. An
        empty result sends the loop back to read the latest version, which is
        then the winner's: this very publication (identical drafts — one
        version, returned as not created) or another one (the next number).
        """
        for _ in range(_PUBLISH_ATTEMPTS):
            latest = await self.latest_version(authored_document_id)
            if latest is not None and latest.publication_digest == publication_digest:
                return latest, False
            stmt = (
                pg_insert(TestVersion)
                .values(
                    authored_document_id=authored_document_id,
                    version=(latest.version if latest is not None else 0) + 1,
                    language=language,
                    body=body,
                    content_digest=content_digest,
                    answers_digest=answers_digest,
                    publication_digest=publication_digest,
                )
                .on_conflict_do_nothing()
                .returning(TestVersion)
                .execution_options(populate_existing=True)
            )
            created = (await self._session.execute(stmt)).scalar_one_or_none()
            if created is not None:
                await self._session.flush()
                return created, True

        msg = (
            f"Could not allocate a version for test {authored_document_id} "
            f"after {_PUBLISH_ATTEMPTS} attempts."
        )
        raise RuntimeError(msg)

    async def latest_version(
        self, authored_document_id: uuid.UUID
    ) -> TestVersion | None:
        """The test's newest published version, or ``None`` before the first."""
        stmt = (
            select(TestVersion)
            .where(TestVersion.authored_document_id == authored_document_id)
            .order_by(TestVersion.version.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_version(self, version_id: uuid.UUID) -> TestVersion | None:
        """A published version by its id — what a submission names."""
        return await self._session.get(TestVersion, version_id)

    async def published_among(
        self, authored_document_ids: Collection[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of these tests have been published — one query for a whole tree.

        No ids, no query: a tree without a test written in the system asks
        nothing.
        """
        if not authored_document_ids:
            return set()
        stmt = (
            select(TestVersion.authored_document_id)
            .where(TestVersion.authored_document_id.in_(authored_document_ids))
            .distinct()
        )
        return set((await self._session.execute(stmt)).scalars())

    async def version_with_digests(
        self,
        authored_document_id: uuid.UUID,
        *,
        content_digest: str,
        answers_digest: str,
    ) -> TestVersion | None:
        """The newest published version with these two digests, or ``None``.

        What a version of the explanations was asked for: its axes are the
        visible digest and the key's digest. Several versions can share them —
        a pass mark or an explanation of the author's own changed in between —
        and every one of them has the same text and the same key, so the newest
        stands for all. Served by ``ix_test_versions_document_content``.
        """
        stmt = (
            select(TestVersion)
            .where(
                TestVersion.authored_document_id == authored_document_id,
                TestVersion.content_digest == content_digest,
                TestVersion.answers_digest == answers_digest,
            )
            .order_by(TestVersion.version.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
