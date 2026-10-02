"""The criteria list of a task version: chosen, composed once, shared (task 08).

Purpose:
    A review needs the criteria of the task version it reviews. The list in
    force is the author's edit when there is one for that version, else the
    model's list; the model's list is composed once per version by an
    expensive call, while several submissions of the version may arrive as it
    is being composed. This service says which criteria apply and, on a miss,
    composes the list exactly once (``TASK.md`` section 9, decision 2).

Interface:
    :func:`choose_in_force` — the rule "the author's edit for this version,
    else the model's ready list for this version", on rows already read. The
    one place the rule lives; :func:`load_in_force` reads the rows and applies
    it. The author's routes call it directly; today's review and the
    evaluation stage of task 09b, through :meth:`CriteriaListService.get_or_compose`.
    :meth:`CriteriaListService.get_or_compose` — for a review: the list in
    force, composed on a miss, or :class:`CriteriaUnavailable` with a reason.
    :func:`build_criteria_list_service` — the production wiring.
    :func:`has_ready_summary` and :func:`recorded_stop` — two facts the
    author's reading of a task without a list states (task 09b): whether the
    task is processed, and how its last composition gave up.

Replacing it:
    The service needs a session factory and a composer — anything with the
    two methods of :class:`CriteriaComposer`. Another way of composing a list
    is another composer handed to the constructor; the protocol below does not
    change with it.

The claim protocol:
    * A miss — no live row, a stale version, or the one recomposition once
      concepts exist — claims the list: a ``pending`` row committed in a short
      transaction of its own, so every other submission sees the claim.
    * The claimer calls the model holding no session — the call may take
      minutes and must pin neither a connection nor a lock — and records
      ``ready`` or ``failed`` in another short transaction.
    * While the model answers, a heartbeat renews the claim's ``claimed_at``
      every :data:`HEARTBEAT_INTERVAL`, each time only if the claim is still
      the claimer's. It stops on every way out of the composition — an answer,
      a failure, a defect, a cancelled job — before the final write.
    * A submission that finds a live claim waits, re-reading the row, and
      takes the list when it is ready; it waits while the heartbeat lives, but
      no longer than :data:`CLAIM_WAIT_LIMIT`. A composition that gave up is
      history (``failed``), so the next submission may claim again, and every
      submission waiting for it goes on without a list — without a call of its
      own (``TASK.md`` 3.7).
    * A claim silent for :data:`CLAIM_SILENCE_LIMIT` is abandoned — its claimer
      is gone — and a submission that finds it so, arriving or waiting, takes
      it over by a conditional update of ``claimed_at``; of two that judge it
      abandoned exactly one does. A cancelled composition is not recorded as
      failed: its heartbeat stops, the claim falls silent and is taken over.
    * Whoever loses a race — for a claim or a takeover — waits for the winner;
      a claimer whose claim was taken over writes nothing.

The one recomposition (decision 13): a list composed while neither the node's
nor the course root's summary existed is composed again, once, when one of
them appears; the new list records that concepts were in its input, and a
summary that later disappears does not trigger another.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

import structlog
from sqlalchemy import func, select

from course_supporter.agents.criteria_decomposer import CriteriaDecomposerAgent
from course_supporter.criteria_kinds import CriteriaLayer
from course_supporter.criteria_list_state import CriteriaListState
from course_supporter.homework.criteria_form import (
    CRITERIA_FORM_VERSION,
    CriteriaComposition,
    Criterion,
    criteria_from_document,
    criteria_to_document,
)
from course_supporter.homework.task_context import load_task_context
from course_supporter.language import display_name
from course_supporter.llm.error_categories import LadderExhaustedError, LadderStop
from course_supporter.storage.node_summary_final_repository import (
    NodeSummaryFinalRepository,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    DocumentSummary,
    NodeSummaryFinal,
    TaskCriteriaList,
    TaskCriteriaOverride,
)
from course_supporter.storage.task_criteria_list_repository import (
    TaskCriteriaListRepository,
)
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from course_supporter.llm.stage_router import StageRouter

logger = structlog.get_logger(__name__)

CLAIM_POLL_INTERVAL: Final = timedelta(seconds=10)
"""How often a waiting submission re-reads the claim it waits for.

A finished list is picked up within ten seconds — little against a composition
that takes minutes — for one short read per interval.
"""

CLAIM_WAIT_LIMIT: Final = timedelta(minutes=25)
"""How long after a claim a submission still waits for its list.

The heartbeat says whether a claim is alive; this says how long a live one is
worth waiting for. Counted from the claim — the row's ``created_at``, which a
takeover does not reset — not from the waiter's arrival, so a submission that
finds an overdue claim does not wait at all. Twenty-five minutes cover a
healthy composition on the first rung with one retry: DeepSeek with reasoning
on answered in 149 to 707 s in the live runs of task 2.4.17
(``llm/providers/openai_compat.py``), and twice 707 s is under 24 minutes. A
composition still running after that has left the healthy range, and the
waiter reviews without the list rather than hold its worker.
"""

HEARTBEAT_INTERVAL: Final = timedelta(seconds=60)
"""How often a claimer renews its claim while the model is answering.

One short conditional update a minute: cheap against a composition of several
minutes, and often enough that a claimer that is gone falls silent within a
few.
"""

CLAIM_SILENCE_LIMIT: Final = timedelta(minutes=5)
"""How long a claim may stay silent before it counts as abandoned.

Five heartbeat intervals. A live claimer can miss a beat or two — a renewal
delayed by a slow database, or by an event loop busy with other work — but not
five in a row; a claimer that is gone (a deploy that recreated its worker, a
crash, a cancelled job) misses all of them, and its task version waits five
minutes for a new claimer instead of a job's timeout.
"""


@dataclass(frozen=True, slots=True)
class CriteriaInForce:
    """The criteria a review of a task version uses.

    Attributes:
        criteria: The criteria, in the task-08 form.
        layer: The author's edit or the model's list.
        source_id: The row they were read from — ``task_criteria_overrides``
            for the author's layer, ``task_criteria_lists`` for the model's. A
            verdict names a criterion by this pair (``TASK.md`` section 9,
            decision 21): identifiers are stable within one list, not across
            the lists of one version.
    """

    criteria: tuple[Criterion, ...]
    layer: CriteriaLayer
    source_id: uuid.UUID


class UnavailableReason(StrEnum):
    """Why a review goes on without a criteria list."""

    TASK_NOT_READY = "task_not_ready"
    """Not a live, ingested task version: there is nothing to compose from."""

    COMPOSITION_FAILED = "composition_failed"
    """The composition this submission ran, or waited for, gave up."""

    WAIT_EXHAUSTED = "wait_exhausted"
    """Another submission's composition did not finish within the wait."""


@dataclass(frozen=True, slots=True)
class CriteriaUnavailable:
    """No list for this review; the next submission tries again."""

    reason: UnavailableReason


class CriteriaComposer(Protocol):
    """What the service needs from whoever composes a list."""

    async def compose(
        self,
        *,
        task_title: str,
        task_description: str,
        task_text: str,
        task_type: str,
        language: str | None,
        node_description: str,
        node_concepts: Sequence[str],
        root_concepts: Sequence[str],
    ) -> CriteriaComposition: ...

    def prompt_hash(self) -> str: ...


def choose_in_force(
    document: AuthoredDocument,
    *,
    machine: TaskCriteriaList | None,
    override: TaskCriteriaOverride | None,
) -> CriteriaInForce | None:
    """The list in force for the document's current version, or None.

    The author's edit wins when it was written for this version; an edit of an
    earlier version is not carried over (``TASK.md`` 3.6). Otherwise the
    model's list, if it is ready and composed for this version.

    Args:
        document: The task, with its current ``content_hash`` and ``task_type``.
        machine: The task's live ``task_criteria_lists`` row, if any.
        override: The task's live ``task_criteria_overrides`` row, if any.
    """
    if (
        override is not None
        and override.deleted_at is None
        and _is_current(override, document)
    ):
        return CriteriaInForce(
            criteria=criteria_from_document(override.criteria),
            layer=CriteriaLayer.AUTHOR,
            source_id=override.id,
        )
    if (
        machine is not None
        and machine.deleted_at is None
        and machine.state == CriteriaListState.READY
        and machine.criteria is not None
        and _is_current(machine, document)
    ):
        return CriteriaInForce(
            criteria=criteria_from_document(machine.criteria),
            layer=CriteriaLayer.MODEL,
            source_id=machine.id,
        )
    return None


async def load_in_force(
    session: AsyncSession, authored_document_id: uuid.UUID
) -> CriteriaInForce | None:
    """Read a task's rows and apply :func:`choose_in_force`; no model call."""
    document = await session.get(AuthoredDocument, authored_document_id)
    if document is None or document.deleted_at is not None:
        return None
    return choose_in_force(
        document,
        machine=await TaskCriteriaListRepository(session).get_live(
            authored_document_id
        ),
        override=await TaskCriteriaOverrideRepository(session).get_live(
            authored_document_id
        ),
    )


def input_fingerprint(
    *,
    content_hash: str,
    node_description: str,
    node_concepts: Sequence[str],
    root_concepts: Sequence[str],
    prompt_hash: str,
) -> str:
    """SHA-256 of what a composition was given (decision 13) — diagnostics only.

    Never a version key: a list is current by its task version alone, so a new
    node description does not recompose it. The fingerprint says whether two
    lists of one version were composed from the same input.
    """
    canonical = json.dumps(
        {
            "content_hash": content_hash,
            "node_description": node_description,
            "node_concepts": list(node_concepts),
            "root_concepts": list(root_concepts),
            "prompt_hash": prompt_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def has_ready_summary(
    session: AsyncSession, authored_document_id: uuid.UUID
) -> bool:
    """Is the task processed — does it have a ready summary to compose from?

    The content guard of today's cache, and the one reason a list cannot even
    be tried: a task that is not ingested has nothing to compose from.
    """
    ready_summary = await session.scalar(
        select(DocumentSummary.id).where(
            DocumentSummary.authored_document_id == authored_document_id,
            DocumentSummary.deleted_at.is_(None),
            DocumentSummary.status == "ready",
        )
    )
    return ready_summary is not None


def recorded_stop(failure_reason: str | None) -> LadderStop | None:
    """How a failed composition ended, as its row recorded it; None for a defect.

    The reading half of what :meth:`CriteriaListService._compose` writes —
    ``"<ending>: <message>"`` when the model's ladder gave up, ``"error:
    <type>"`` when the code broke — kept beside the writer, and locked against
    it by a test that fails a composition each way and reads the row back.

    >>> recorded_stop("output_ceiling: the answer was cut at its ceiling")
    <LadderStop.OUTPUT_CEILING: 'output_ceiling'>
    >>> recorded_stop("error: KeyError") is None
    True
    """
    head = (failure_reason or "").partition(":")[0]
    try:
        return LadderStop(head)
    except ValueError:
        return None


def _is_current(
    row: TaskCriteriaList | TaskCriteriaOverride, document: AuthoredDocument
) -> bool:
    return (
        row.source_content_hash == document.content_hash
        and row.source_task_type == document.task_type
    )


def _node_description(final: NodeSummaryFinal | None) -> str:
    """The description the review reads; an author's empty module has its own."""
    if final is None:
        return ""
    return (final.manual_description if final.is_manual else final.description) or ""


def _main_concepts(final: NodeSummaryFinal | None) -> list[str]:
    return list(final.main_concepts or []) if final is not None else []


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """What one read of a task says, before anything is decided.

    ``content_hash`` and ``task_type`` are the document's, taken once they are
    known to be set: they are the version key a claim is made for.
    """

    document: AuthoredDocument
    content_hash: str
    task_type: str
    live: TaskCriteriaList | None
    override: TaskCriteriaOverride | None
    node_final: NodeSummaryFinal | None
    root_final: NodeSummaryFinal | None
    now: datetime

    @property
    def concepts_available(self) -> bool:
        """Whether a node or root summary — the source of concepts — exists."""
        return self.node_final is not None or self.root_final is not None


@dataclass(slots=True)
class _Claim:
    """The claim this submission holds; its heartbeat moves ``claimed_at``."""

    list_id: uuid.UUID
    claimed_at: datetime


class CriteriaListService:
    """The list in force for a review, composed on a miss exactly once."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        composer: CriteriaComposer,
        *,
        abandoned_after: timedelta = CLAIM_SILENCE_LIMIT,
        heartbeat_interval: timedelta = HEARTBEAT_INTERVAL,
        wait_limit: timedelta = CLAIM_WAIT_LIMIT,
        poll_interval: timedelta = CLAIM_POLL_INTERVAL,
    ) -> None:
        """Wire the service.

        Args:
            session_factory: Every step opens and closes its own short session;
                none is held across the model call.
            composer: Composes a list on a miss.
            abandoned_after: How long a claim may stay silent before it counts
                as abandoned.
            heartbeat_interval: How often a claimer renews its claim.
            wait_limit: How long after a claim a submission waits for it.
            poll_interval: How often a waiting submission re-reads the claim.
        """
        self._session_factory = session_factory
        self._composer = composer
        self._abandoned_after = abandoned_after
        self._heartbeat_interval = heartbeat_interval
        self._wait_limit = wait_limit
        self._poll_interval = poll_interval

    async def get_or_compose(
        self, authored_document_id: uuid.UUID
    ) -> CriteriaInForce | CriteriaUnavailable:
        """The criteria a review of the task's current version uses.

        Returns the list in force; on a miss composes the model's list first —
        at most once per version, however many submissions ask together.

        Returns:
            The list in force, or why there is none for this review.

        Raises:
            Exception: A defect in composing (not a model's failure — that is
                :class:`CriteriaUnavailable`) propagates after the claim is
                recorded as failed, so the version is not left blocked.
        """
        async with self._session_factory() as session:
            snapshot = await self._read(session, authored_document_id)
        if snapshot is None:
            return self._unavailable(
                authored_document_id, UnavailableReason.TASK_NOT_READY
            )
        document, live = snapshot.document, snapshot.live

        in_force = choose_in_force(document, machine=live, override=snapshot.override)
        if in_force is not None and not (
            in_force.layer is CriteriaLayer.MODEL and self._recompose_once(snapshot)
        ):
            return in_force

        if (
            live is not None
            and live.state == CriteriaListState.PENDING
            and _is_current(live, document)
        ):
            # Another submission's claim: wait for it — or take it over, if it
            # has gone silent.
            return await self._wait(live.id, snapshot)

        # A miss: no live row, a stale version (a claim of one included — its
        # claimer's write is refused once the row is released), or the one
        # recomposition now that concepts exist.
        claimed = await self._claim(snapshot)
        if claimed is None:
            return self._unavailable(document.id, UnavailableReason.TASK_NOT_READY)
        if isinstance(claimed, uuid.UUID):
            return await self._wait(claimed, snapshot)
        return await self._compose(snapshot, *claimed)

    # ── Reading ─────────────────────────────────────────────────────

    async def _read(
        self, session: AsyncSession, authored_document_id: uuid.UUID
    ) -> _Snapshot | None:
        document = await session.get(AuthoredDocument, authored_document_id)
        if document is None or document.deleted_at is not None:
            return None
        task_type, content_hash = document.task_type, document.content_hash
        if task_type is None or content_hash is None:
            return None
        if not await has_ready_summary(session, authored_document_id):
            return None
        finals = NodeSummaryFinalRepository(session)
        node_final = await finals.get_by_course_node_id(document.course_node_id)
        # A task attached to the root has one summary, not two.
        root_final = (
            None
            if document.course_root_id == document.course_node_id
            else await finals.get_by_course_node_id(document.course_root_id)
        )
        return _Snapshot(
            document=document,
            content_hash=content_hash,
            task_type=task_type,
            live=await TaskCriteriaListRepository(session).get_live(
                authored_document_id
            ),
            override=await TaskCriteriaOverrideRepository(session).get_live(
                authored_document_id
            ),
            node_final=node_final,
            root_final=root_final,
            # The database's clock, the one claimed_at is written by — an age
            # read against the application's clock would carry its skew.
            now=await _database_now(session),
        )

    @staticmethod
    def _recompose_once(snapshot: _Snapshot) -> bool:
        live = snapshot.live
        return (
            live is not None
            and live.concepts_in_input is False
            and snapshot.concepts_available
        )

    # ── Claiming ────────────────────────────────────────────────────

    async def _claim(
        self, snapshot: _Snapshot
    ) -> tuple[uuid.UUID, datetime] | uuid.UUID | None:
        """Claim the version, releasing the live row the snapshot found stale.

        Returns:
            The claim — row id and ``claimed_at``; or, when another submission
            claimed first, that submission's row id; or None when the task's
            rows are gone (the task was deleted meanwhile).
        """
        document, release = snapshot.document, snapshot.live
        async with self._session_factory() as session:
            repo = TaskCriteriaListRepository(session)
            if release is not None:
                await repo.release(release.id)
            row = await repo.claim(
                authored_document_id=document.id,
                source_content_hash=snapshot.content_hash,
                source_task_type=snapshot.task_type,
                form_version=CRITERIA_FORM_VERSION,
            )
            if row is None:
                # Lost the race. The winner's row is read in the same breath,
                # so even a winner that has already given up is found.
                winner = await _latest_row_id(session, document.id)
                await session.rollback()
                return winner
            claim = (row.id, row.claimed_at)
            await session.commit()
        logger.info(
            "criteria_list_claimed",
            authored_document_id=str(document.id),
            list_id=str(claim[0]),
            released=str(release.id) if release is not None else None,
        )
        return claim

    async def _take_over(
        self, live: TaskCriteriaList, authored_document_id: uuid.UUID
    ) -> datetime | None:
        async with self._session_factory() as session:
            claimed_at = await TaskCriteriaListRepository(session).take_over(
                live.id, seen_claimed_at=live.claimed_at
            )
            await session.commit()
        if claimed_at is not None:
            logger.warning(
                "criteria_list_claim_taken_over",
                authored_document_id=str(authored_document_id),
                list_id=str(live.id),
                abandoned_claim_at=live.claimed_at.isoformat(),
            )
        return claimed_at

    # ── Composing ───────────────────────────────────────────────────

    async def _compose(
        self, snapshot: _Snapshot, list_id: uuid.UUID, claimed_at: datetime
    ) -> CriteriaInForce | CriteriaUnavailable:
        document = snapshot.document
        claim = _Claim(list_id, claimed_at)
        node_description = _node_description(snapshot.node_final)
        node_concepts = _main_concepts(snapshot.node_final)
        root_concepts = _main_concepts(snapshot.root_final)
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(claim, stop))
        try:
            try:
                async with self._session_factory() as session:
                    title, description, text = await load_task_context(
                        session, document.id
                    )
                fingerprint = input_fingerprint(
                    content_hash=snapshot.content_hash,
                    node_description=node_description,
                    node_concepts=node_concepts,
                    root_concepts=root_concepts,
                    prompt_hash=self._composer.prompt_hash(),
                )
                composition = await self._composer.compose(
                    task_title=title,
                    task_description=description,
                    task_text=text,
                    task_type=snapshot.task_type,
                    # The course language, by name: criteria are shared by every
                    # student, so no one student's preference decides
                    # (decision 18).
                    language=display_name(document.language)
                    if document.language
                    else None,
                    node_description=node_description,
                    node_concepts=node_concepts,
                    root_concepts=root_concepts,
                )
            finally:
                # Every way out stops the heartbeat, a cancelled job included,
                # and before the final write: the write must hold the claim time
                # the heartbeat wrote last.
                stop.set()
                await heartbeat
        except LadderExhaustedError as exc:
            await self._fail(claim, f"{exc.stop.value}: {exc}")
            return self._unavailable(document.id, UnavailableReason.COMPOSITION_FAILED)
        except Exception as exc:
            # A defect rather than a model's failure. Recording it keeps the
            # version from waiting for the claim to fall silent; raising keeps
            # the defect visible. A cancelled job is neither: its claim is left
            # to fall silent and be taken over.
            await self._fail(claim, f"error: {type(exc).__name__}")
            raise

        async with self._session_factory() as session:
            stored = await TaskCriteriaListRepository(session).mark_ready(
                list_id,
                claimed_at=claim.claimed_at,
                criteria=criteria_to_document(composition.criteria),
                contradictions=list(composition.contradictions),
                concepts_in_input=snapshot.concepts_available,
                dropped_concept_count=composition.dropped_concept_count,
                input_fingerprint=fingerprint,
            )
            await session.commit()
        if not stored:
            # The claim was taken over while the model was answering; the list
            # belongs to the new claimer, and this answer is not written.
            logger.warning(
                "criteria_list_write_refused",
                authored_document_id=str(document.id),
                list_id=str(list_id),
            )
            return await self._wait(list_id, snapshot)
        logger.info(
            "criteria_list_composed",
            authored_document_id=str(document.id),
            list_id=str(list_id),
            criteria_count=len(composition.criteria),
            dropped_concept_count=composition.dropped_concept_count,
            concepts_in_input=snapshot.concepts_available,
        )
        return CriteriaInForce(
            criteria=composition.criteria,
            layer=CriteriaLayer.MODEL,
            source_id=list_id,
        )

    async def _fail(self, claim: _Claim, reason: str) -> None:
        async with self._session_factory() as session:
            recorded = await TaskCriteriaListRepository(session).mark_failed(
                claim.list_id, claimed_at=claim.claimed_at, failure_reason=reason
            )
            await session.commit()
        logger.warning(
            "criteria_list_failed",
            list_id=str(claim.list_id),
            reason=reason,
            recorded=recorded,
        )

    async def _heartbeat(self, claim: _Claim, stop: asyncio.Event) -> None:
        """Renew the claim every interval until stopped, or until it is lost."""
        interval = self._heartbeat_interval.total_seconds()
        while not await _stopped_within(stop, interval):
            try:
                async with self._session_factory() as session:
                    renewed = await TaskCriteriaListRepository(session).renew(
                        claim.list_id, claimed_at=claim.claimed_at
                    )
                    await session.commit()
            except Exception as exc:
                # A missed beat, not a lost claim: the next one tries again,
                # and only a claim silent for the whole limit is abandoned.
                logger.warning(
                    "criteria_list_heartbeat_failed",
                    list_id=str(claim.list_id),
                    error=type(exc).__name__,
                )
                continue
            if renewed is None:
                # Taken over: the claim is somebody else's, and this claimer's
                # final write will be refused.
                logger.warning(
                    "criteria_list_heartbeat_lost", list_id=str(claim.list_id)
                )
                return
            claim.claimed_at = renewed

    # ── Waiting ─────────────────────────────────────────────────────

    async def _wait(
        self, list_id: uuid.UUID, snapshot: _Snapshot
    ) -> CriteriaInForce | CriteriaUnavailable:
        """Wait for another submission's claim: its list, its failure, its silence.

        Ends with the list once it is ready; without one when the composition
        failed, when the row was released for a newer version, or when the claim
        is older than the wait limit. A claim that has gone silent is taken over
        here — by a waiting submission as by an arriving one.
        """
        document_id = snapshot.document.id
        while True:
            async with self._session_factory() as session:
                row = await TaskCriteriaListRepository(session).get_by_id(list_id)
                now = await _database_now(session)
            if row is None:
                return self._unavailable(document_id, UnavailableReason.TASK_NOT_READY)
            if (
                row.state == CriteriaListState.READY
                and row.deleted_at is None
                and row.criteria is not None
            ):
                return CriteriaInForce(
                    criteria=criteria_from_document(row.criteria),
                    layer=CriteriaLayer.MODEL,
                    source_id=row.id,
                )
            if row.state == CriteriaListState.FAILED:
                return self._unavailable(
                    document_id, UnavailableReason.COMPOSITION_FAILED
                )
            if row.deleted_at is not None:
                # Released for a newer version: this list is not coming.
                return self._unavailable(document_id, UnavailableReason.WAIT_EXHAUSTED)
            if now - row.claimed_at >= self._abandoned_after:
                claimed_at = await self._take_over(row, document_id)
                if claimed_at is not None:
                    return await self._compose(snapshot, row.id, claimed_at)
                continue  # taken over by another submission: wait for it
            if now - row.created_at >= self._wait_limit:
                return self._unavailable(document_id, UnavailableReason.WAIT_EXHAUSTED)
            await asyncio.sleep(self._poll_interval.total_seconds())

    @staticmethod
    def _unavailable(
        authored_document_id: uuid.UUID, reason: UnavailableReason
    ) -> CriteriaUnavailable:
        logger.info(
            "criteria_list_unavailable",
            authored_document_id=str(authored_document_id),
            reason=reason.value,
        )
        return CriteriaUnavailable(reason)


async def _database_now(session: AsyncSession) -> datetime:
    now: datetime = (await session.execute(select(func.now()))).scalar_one()
    return now


async def _stopped_within(stop: asyncio.Event, seconds: float) -> bool:
    """Wait up to ``seconds`` for ``stop``; whether it was set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


async def _latest_row_id(
    session: AsyncSession, authored_document_id: uuid.UUID
) -> uuid.UUID | None:
    """The task's most recent list row — the live one, or the one just given up."""
    row_id: uuid.UUID | None = await session.scalar(
        select(TaskCriteriaList.id)
        .where(TaskCriteriaList.authored_document_id == authored_document_id)
        .order_by(TaskCriteriaList.created_at.desc(), TaskCriteriaList.id.desc())
        .limit(1)
    )
    return row_id


def build_criteria_list_service(
    session_factory: async_sessionmaker[AsyncSession], stage_router: StageRouter
) -> CriteriaListService:
    """Wire the service with the production composer and the default timings."""
    return CriteriaListService(session_factory, CriteriaDecomposerAgent(stage_router))
