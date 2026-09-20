"""Reads and writes of a task's reference — both layers (mentor-rebuild task 06).

One module for the machine layer and the author's, because the only interesting
questions are about the pair: which version answers the author's current key,
and whether a key exists at all. Splitting them would put half of each answer in
each file.

Not tenant-scoped: a reference belongs to an ``AuthoredDocument`` whose tenant
the route resolves before calling in here (the ``ProjectBaseRepository``
arrangement, same reason). The repository flushes and never commits — the caller
owns the transaction boundary.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.reference_kinds import ReferenceKind, ReferenceState
from course_supporter.storage.orm import TaskReference, TaskReferenceOverride

_OVERRIDE_UNIQUE: Final[tuple[str, ...]] = ("authored_document_id", "kind")
"""The index ``ON CONFLICT`` arbitrates on — one author layer per (task, kind)."""

_VERSION_ATTEMPTS: Final[int] = 3
"""How many times :meth:`create_version` recomputes ``MAX(version) + 1``.

Two racing requests for the SAME task read the same next version; the loser's
insert hits ``uq_task_reference_document_version`` and comes back empty, so it
recomputes. Three attempts because the contention is between a handful of author
requests, never a crowd: the second attempt already sees the winner's row, and
the third exists only so a pathological interleaving is a raised error rather
than a silent ``None``.
"""


class TaskReferenceRepository:
    """Storage for both layers of a task's reference."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── the author's layer ───────────────────────────────────────────────

    async def get_override(
        self, authored_document_id: uuid.UUID, kind: ReferenceKind
    ) -> TaskReferenceOverride | None:
        """The author's current layer for this task and kind, or ``None``.

        ``None`` is not an error state here: its absence IS the "waiting for a
        key" state (``TASK.md``, clarification of 2026-09-19), so the caller
        reports it rather than 404s on it.
        """
        stmt = select(TaskReferenceOverride).where(
            TaskReferenceOverride.authored_document_id == authored_document_id,
            TaskReferenceOverride.kind == kind.value,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def replace_override(
        self,
        *,
        authored_document_id: uuid.UUID,
        kind: ReferenceKind,
        answers: dict[str, list[str]],
        source_content_hash: str,
        author_explanations: dict[str, str] | None = None,
        carried_over: bool = False,
    ) -> TaskReferenceOverride:
        """Replace the author's layer WHOLE — the last replacement wins.

        One statement, not read-then-write: two replacements can arrive
        together, and only the database decides that race the same way every
        time. There is deliberately no "replace only if unchanged" arm and no
        conflict refusal (ratified 2026-09-19, revising the pre-flight draft):
        an upsert does not lose a write, it orders two of them, and the route
        hands the stored row back so the author sees what actually stands.

        ``updated_at`` is set EXPLICITLY in the update branch. SQLAlchemy does
        not apply a Python-side ``onupdate`` on this path
        (``dialects/postgresql/dml.py``: the ``set_`` dictionary "does not take
        into account Python-specified default UPDATE values"), so leaving it out
        would freeze the time of the first key on every later one — the lesson
        task 05 paid for.

        ``populate_existing`` refreshes the returned object: RETURNING rows are
        matched against the session's identity map, and a row already in it
        would otherwise keep its old values — and the caller reads the current
        layer before replacing it, so it IS in the map by then.
        """
        stmt = (
            pg_insert(TaskReferenceOverride)
            .values(
                authored_document_id=authored_document_id,
                kind=kind.value,
                answers=answers,
                author_explanations=author_explanations,
                source_content_hash=source_content_hash,
                carried_over=carried_over,
            )
            .on_conflict_do_update(
                index_elements=list(_OVERRIDE_UNIQUE),
                set_={
                    "answers": answers,
                    "author_explanations": author_explanations,
                    "source_content_hash": source_content_hash,
                    "carried_over": carried_over,
                    "updated_at": func.now(),
                },
            )
            .returning(TaskReferenceOverride)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one()

    async def clear_override(
        self, authored_document_id: uuid.UUID, kind: ReferenceKind
    ) -> bool:
        """Drop the author's layer, returning whether there was one.

        A hard delete, not a soft one: the table carries no ``deleted_at``
        (its versions live next door, in the machine layer), and the state this
        leaves behind — no row — is a meaningful one the reader already knows.
        """
        override = await self.get_override(authored_document_id, kind)
        if override is None:
            return False
        await self._session.delete(override)
        await self._session.flush()
        return True

    # ── the machine layer ────────────────────────────────────────────────

    async def get_live_version(
        self,
        *,
        authored_document_id: uuid.UUID,
        kind: ReferenceKind,
        source_content_hash: str,
        source_task_type: str,
        answers_hash: str,
        language: str,
    ) -> TaskReference | None:
        """The live version for this exact key, or ``None``.

        "Live" is everything but ``failed`` — the same set the partial unique
        index covers, so a hit here is exactly what a second insert would
        collide with. A ``failed`` version is history: it must not be handed
        back as an answer, and it must not block the retry that replaces it.
        """
        stmt = select(TaskReference).where(
            TaskReference.authored_document_id == authored_document_id,
            TaskReference.kind == kind.value,
            TaskReference.source_content_hash == source_content_hash,
            TaskReference.source_task_type == source_task_type,
            TaskReference.answers_hash == answers_hash,
            TaskReference.language == language,
            TaskReference.state != ReferenceState.FAILED.value,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def latest_for_key(
        self,
        *,
        authored_document_id: uuid.UUID,
        kind: ReferenceKind,
        source_content_hash: str,
        source_task_type: str,
        answers_hash: str,
        language: str,
    ) -> TaskReference | None:
        """The newest version for this key, ``failed`` ones included.

        The reader's counterpart to :meth:`get_live_version`, and the two
        differ on purpose. Creating a version asks "is there one that blocks a
        new insert", and a failed one does not. Reporting the state asks "what
        happened to this key", and a failure IS what happened — without this
        method the author who is waiting for an answer would be told they never
        sent a key.

        Newest by version number, so a retry after a failure shows the retry.
        """
        stmt = (
            select(TaskReference)
            .where(
                TaskReference.authored_document_id == authored_document_id,
                TaskReference.kind == kind.value,
                TaskReference.source_content_hash == source_content_hash,
                TaskReference.source_task_type == source_task_type,
                TaskReference.answers_hash == answers_hash,
                TaskReference.language == language,
            )
            .order_by(TaskReference.version.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, reference_id: uuid.UUID) -> TaskReference | None:
        """Load one version by id (identity-map hit if already loaded)."""
        return await self._session.get(TaskReference, reference_id)

    async def create_version(
        self,
        *,
        authored_document_id: uuid.UUID,
        kind: ReferenceKind,
        source_content_hash: str,
        source_task_type: str,
        answers_hash: str,
        language: str,
    ) -> tuple[TaskReference, bool]:
        """Return the live version for this key, creating it if there is none.

        Returns ``(version, created)`` — ``created`` is what tells the caller
        whether to enqueue a generation. Two requests that arrive together with
        the SAME key must produce ONE version and ONE job (ratified
        2026-09-19), so the loser of the race gets the winner's row back rather
        than a refusal: a refusal here would be an error message about someone
        else's success.

        How the race is settled, and why this way. The insert carries
        ``ON CONFLICT DO NOTHING`` with NO arbiter named, so it covers BOTH
        unique rules of the table at once — the key (``uq_task_reference_axes_
        active``) and the version number (``uq_task_reference_document_
        version``) — and neither raises ``IntegrityError``, which would poison
        the caller's transaction. An empty result means somebody won something,
        and the loop starts over: it re-reads the key first (the usual case —
        the winner's version is the answer) and only then recomputes
        ``MAX(version) + 1`` (the version-number race between two DIFFERENT
        keys on one task). ``SELECT ... FOR UPDATE`` on the task row would also
        work and would serialize every author of that task against each other;
        this stays lock-free because the contention is rare and the database
        already refuses to persist a duplicate either way.
        """
        for _ in range(_VERSION_ATTEMPTS):
            existing = await self.get_live_version(
                authored_document_id=authored_document_id,
                kind=kind,
                source_content_hash=source_content_hash,
                source_task_type=source_task_type,
                answers_hash=answers_hash,
                language=language,
            )
            if existing is not None:
                return existing, False

            stmt = (
                pg_insert(TaskReference)
                .values(
                    authored_document_id=authored_document_id,
                    version=await self._next_version(authored_document_id),
                    kind=kind.value,
                    source_content_hash=source_content_hash,
                    source_task_type=source_task_type,
                    answers_hash=answers_hash,
                    language=language,
                    state=ReferenceState.PENDING.value,
                )
                .on_conflict_do_nothing()
                .returning(TaskReference)
                .execution_options(populate_existing=True)
            )
            created = (await self._session.execute(stmt)).scalar_one_or_none()
            if created is not None:
                await self._session.flush()
                return created, True

        msg = (
            f"Could not allocate a reference version for task "
            f"{authored_document_id} after {_VERSION_ATTEMPTS} attempts."
        )
        raise RuntimeError(msg)

    async def mark_ready(
        self, reference_id: uuid.UUID, explanations: dict[str, str]
    ) -> TaskReference | None:
        """Store the generated explanations and open the version for reading."""
        reference = await self.get_by_id(reference_id)
        if reference is None:
            return None
        reference.explanations = explanations
        reference.state = ReferenceState.READY.value
        reference.failure_reason = None
        await self._session.flush()
        return reference

    async def mark_failed(
        self, reference_id: uuid.UUID, failure_reason: str
    ) -> TaskReference | None:
        """Record that generation gave up, with the reason the author will read.

        The row stays. It is what keeps the failure visible, and — because the
        live-key index excludes ``failed`` — it is also what lets the next
        attempt take the same key without colliding with this one.
        """
        reference = await self.get_by_id(reference_id)
        if reference is None:
            return None
        reference.state = ReferenceState.FAILED.value
        reference.failure_reason = failure_reason
        await self._session.flush()
        return reference

    async def _next_version(self, authored_document_id: uuid.UUID) -> int:
        """``MAX(version) + 1`` for the task, computed in the caller's transaction.

        The read is a guess, not a guarantee — the unique index is the arbiter
        (the ``ProjectBaseRepository`` arrangement). The difference is what
        happens to the loser: there it surfaces as a 409 the author retries,
        here :meth:`create_version` recomputes and tries again, because the
        author asked for a key and not for a version number.
        """
        stmt = select(func.coalesce(func.max(TaskReference.version), 0)).where(
            TaskReference.authored_document_id == authored_document_id
        )
        return int((await self._session.execute(stmt)).scalar_one()) + 1

    async def latest_version_number(self, authored_document_id: uuid.UUID) -> int:
        """Highest version number this task has, or ``0`` when it has none."""
        stmt = select(func.coalesce(func.max(TaskReference.version), 0)).where(
            TaskReference.authored_document_id == authored_document_id
        )
        return int((await self._session.execute(stmt)).scalar_one())
