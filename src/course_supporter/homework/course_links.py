"""Links from a failed criterion to where the course explains it (task 10).

Purpose:
    A criterion the student failed names what they have not mastered yet; the
    review points them to the place in the course that teaches it. The port
    answers one question about a submission: for each failed criterion, which
    places of the course to point to — at most :data:`MAX_LINKS`, best first.

    Nothing calls the port yet: the path's step that asks it, the prompt that
    words a link and the link's form in the review are the next commits.

    ``course_link`` names the thing throughout, because "reference" is taken
    by a test's answer key (``TaskReference``, ``reference_service.py``).

Input and output:
    :meth:`CourseLinkFinder.find` takes a :class:`SubmissionScope` — tenant,
    course root, the task's node and the task's document — and the failed
    criteria as :class:`FailedCriterion` values: id, text, concepts. It
    returns, for EVERY criterion id it was given, a tuple of up to
    :data:`MAX_LINKS` :class:`LinkCandidate` values; a criterion without
    concepts, or without a match, maps to an empty tuple. A candidate carries
    what a review needs to show the place (material, anchor, excerpt) and why
    it was chosen (the matched concepts, a confidence 0..1, and whether the
    place comes later in the course than the task).

    The criterion's text is a required input although the concept adapter
    never reads it: a content-based adapter will search by it, and the port
    stays as it is when that adapter arrives.

The concept adapter — :class:`ConceptCourseLinkFinder`:
    1. One read of the course's candidates
       (:meth:`~course_supporter.storage.document_segment_repository.DocumentSegmentRepository.list_candidates`
       — the candidates and the learning order of ``search_by_concepts``)
       and of the task document's place.
    2. A segment matches a criterion when a
       :func:`~course_supporter.concept_dedup.normalization_key` of one of the
       criterion's concepts is among the keys of the segment's MAIN concepts.
       Compared in Python, on keys from both sides; an exact match is a case
       of the key.
    3. Earlier or later is decided against the task document, whose place is
       put into the same :func:`~course_supporter.learning_order.in_learning_order`
       over the same tree. If a criterion has any earlier match, only earlier
       ones are kept; later ones (``later_in_course=True``) only when there is
       no earlier one at all.
    4. The kept matches are ranked by :func:`link_confidence` — equal values
       stay in learning order — and the best :data:`MAX_LINKS` are taken.
    5. The text of the taken segments only is read, in one narrow query, for
       the excerpt (:func:`excerpt_of`).

Replacing:
    Anything with an ``async find(scope, failed)`` of this signature satisfies
    :class:`CourseLinkFinder` — a structural ``Protocol``, no base class to
    inherit — and is passed where the concept adapter is passed; no caller
    changes. A content-based adapter, for example, searches the course's
    segments by each criterion's ``text`` (embeddings over the same candidates,
    ``list_candidates``), and builds the same :class:`LinkCandidate` values:
    its own similarity becomes ``confidence`` (still 0..1), and
    ``matched_concepts`` may stay empty. Two adapters can also be combined by
    a third that asks both and merges per criterion id.

    The production choice is made in one place, :func:`build_course_link_finder`:
    return the new adapter there. Tests of other modules substitute that
    factory, so they keep working whichever adapter it returns.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol

import structlog
from sqlalchemy import select

from course_supporter.concept_dedup import normalization_key
from course_supporter.learning_order import MaterialPlace, TreeNode, in_learning_order
from course_supporter.storage.document_segment_repository import (
    CandidateSegment,
    DocumentSegmentRepository,
    SegmentAnchor,
)
from course_supporter.storage.orm import AuthoredDocument

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

MAX_LINKS: Final = 3
"""Places one criterion is linked to, at most."""

EXCERPT_CHARS: Final = 300
"""Characters of a segment's text an excerpt keeps."""

# The confidence weights. They sum to 1, so the confidence stays in 0..1.
#
# Coverage — the share of the criterion's concepts the segment teaches — is the
# main feature and outweighs the other two together (0.7 > 0.2 + 0.1): a segment
# teaching every concept of the criterion always ranks above one teaching half
# of them, and for a criterion of one or two concepts one concept more always
# wins. From three concepts on, one concept is a step of 0.7/3 or less, and a
# segment of the task's own node can outrank it — on purpose: the material of
# the very lesson the task belongs to was written for this topic.
#
# Closeness in the tree comes second: the place the student studied just
# before the task is the likeliest to explain what it asks.
#
# Closeness in the learning order is the smallest, a tiebreak in practice: it
# separates segments that are otherwise alike, favouring the one met last
# before the task.
COVERAGE_WEIGHT: Final = 0.7
TREE_WEIGHT: Final = 0.2
ORDER_WEIGHT: Final = 0.1


class TreeRelation(StrEnum):
    """Where a segment's node sits in the course tree relative to the task's node.

    * ``SAME_NODE`` — the task's own node.
    * ``NEAR`` — a sibling (the same immediate parent), or on the task node's
      own line: an ancestor or a descendant.
    * ``FAR`` — anywhere else.
    """

    SAME_NODE = "same_node"
    NEAR = "near"
    FAR = "far"


TREE_SCORES: Final[Mapping[TreeRelation, float]] = MappingProxyType(
    {TreeRelation.SAME_NODE: 1.0, TreeRelation.NEAR: 0.5, TreeRelation.FAR: 0.0}
)
"""Each relation's value, 0..1, before :data:`TREE_WEIGHT`."""


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmissionScope:
    """Which submission the links are for: its course and its task.

    Keyword-only: all four fields are UUIDs, and a positional call could swap
    them without a type error.

    Attributes:
        tenant_id: The tenant. The concept adapter scopes by the course root
            and does not read it; an adapter over a tenant's own index does.
        course_root_id: The root of the course the task is in.
        task_node_id: The node the task's document is on.
        task_document_id: The task's document (``AuthoredDocument.id``) —
            what "earlier" and "later" are measured against.
    """

    tenant_id: uuid.UUID
    course_root_id: uuid.UUID
    task_node_id: uuid.UUID
    task_document_id: uuid.UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class FailedCriterion:
    """A criterion the student failed, as the finder takes it.

    Attributes:
        id: The criterion's id, unique among the criteria of one call; the
            key of the result.
        text: The criterion's text. Unread by the concept adapter, required
            for an adapter that searches by content (see the module).
        concepts: The criterion's concepts as its list names them; may be
            empty.
    """

    id: str
    text: str
    concepts: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class LinkCandidate:
    """One place of the course a failed criterion is linked to.

    Attributes:
        segment_id: The segment (``DocumentSegment.id``).
        authored_document_id: Its material.
        material_title: The material's title.
        source_type: The material's source type — what the anchor addresses.
        anchor: The segment's position within the material.
        excerpt: The first :data:`EXCERPT_CHARS` characters of the segment's
            text; ``None`` when the text is empty.
        later_in_course: The segment comes after the task in the learning
            order — the student has not reached it yet.
        confidence: How good a link this is, 0..1 (:func:`link_confidence`).
        matched_concepts: The criterion's concepts the segment teaches, in the
            criterion's spelling and order.
    """

    segment_id: uuid.UUID
    authored_document_id: uuid.UUID
    material_title: str
    source_type: str
    anchor: SegmentAnchor
    excerpt: str | None
    later_in_course: bool
    confidence: float
    matched_concepts: tuple[str, ...]


class CourseLinkFinder(Protocol):
    """The replaceable port; the module docstring states the contract."""

    async def find(
        self, scope: SubmissionScope, failed: Sequence[FailedCriterion]
    ) -> Mapping[str, tuple[LinkCandidate, ...]]:
        """Up to :data:`MAX_LINKS` places for each failed criterion, best first.

        Every id of ``failed`` is a key of the result; no place — an empty
        tuple.
        """
        ...


def link_confidence(
    *, coverage: float, relation: TreeRelation, order_closeness: float
) -> float:
    """How good a link a segment is for a criterion, 0..1.

    Args:
        coverage: The share of the criterion's concepts the segment teaches,
            0..1.
        relation: The segment's node relative to the task's node.
        order_closeness: For a segment before the task, how close to the task
            it comes in the learning order, 0..1 (1 — right before it); 0 for
            a segment after the task.
    """
    return (
        COVERAGE_WEIGHT * coverage
        + TREE_WEIGHT * TREE_SCORES[relation]
        + ORDER_WEIGHT * order_closeness
    )


def tree_relation(
    node_id: uuid.UUID,
    task_node_id: uuid.UUID,
    parents: Mapping[uuid.UUID, uuid.UUID | None],
) -> TreeRelation:
    """Where ``node_id`` sits relative to ``task_node_id`` in the tree ``parents``.

    ``parents`` maps each node of the course to its parent (the root's is
    ``None``).
    """
    if node_id == task_node_id:
        return TreeRelation.SAME_NODE
    parent = parents[node_id]
    if parent is not None and parent == parents[task_node_id]:
        return TreeRelation.NEAR
    if node_id in _ancestors(task_node_id, parents) or task_node_id in _ancestors(
        node_id, parents
    ):
        return TreeRelation.NEAR
    return TreeRelation.FAR


def _ancestors(
    node_id: uuid.UUID, parents: Mapping[uuid.UUID, uuid.UUID | None]
) -> set[uuid.UUID]:
    found: set[uuid.UUID] = set()
    parent = parents[node_id]
    while parent is not None and parent not in found:
        found.add(parent)
        parent = parents.get(parent)
    return found


def excerpt_of(content: str) -> str | None:
    """The first :data:`EXCERPT_CHARS` characters of ``content``; empty — ``None``.

    Cut in Python, by characters: a ``str`` slice never splits a Cyrillic
    letter the way a byte cut would.
    """
    if not content.strip():
        return None
    return content[:EXCERPT_CHARS]


@dataclass(frozen=True, slots=True)
class _Match:
    """A candidate that teaches a criterion, before its text is read."""

    segment: CandidateSegment
    matched: tuple[str, ...]
    later: bool
    confidence: float


class ConceptCourseLinkFinder:
    """The concept adapter: links by the criterion's concepts (module, steps 1-5)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._segments = DocumentSegmentRepository(session)

    async def find(
        self, scope: SubmissionScope, failed: Sequence[FailedCriterion]
    ) -> Mapping[str, tuple[LinkCandidate, ...]]:
        """See :meth:`CourseLinkFinder.find`.

        Raises:
            ValueError: An id repeats in ``failed``; or the task document is
                not in the course, or its node is not in the course tree.
        """
        ids = [criterion.id for criterion in failed]
        if len(set(ids)) != len(ids):
            raise ValueError(f"criterion ids repeat: {ids}")
        if not any(criterion.concepts for criterion in failed):
            return {criterion_id: () for criterion_id in ids}

        task = await self._task_place(scope)
        candidates = await self._segments.list_candidates(
            course_root_id=scope.course_root_id
        )
        earlier, later = _around(candidates.segments, candidates.tree, task)
        parents = {node.id: node.parent_id for node in candidates.tree}
        relations = {
            node_id: tree_relation(node_id, scope.task_node_id, parents)
            for node_id in parents
        }
        keys = {
            segment.segment_id: {normalization_key(c) for c in segment.main_concepts}
            for segment in candidates.segments
        }

        kept: dict[str, list[_Match]] = {}
        for criterion in failed:
            wanted = _criterion_keys(criterion.concepts)
            found = _matches(
                earlier, wanted, later=False, keys=keys, relations=relations
            ) or _matches(later, wanted, later=True, keys=keys, relations=relations)
            # A stable sort: equal confidences keep the learning order.
            ranked = sorted(found, key=lambda match: -match.confidence)
            kept[criterion.id] = ranked[:MAX_LINKS]

        contents = await self._segments.read_contents(
            {match.segment.segment_id for ms in kept.values() for match in ms}
        )
        logger.info(
            "course_links_found",
            tenant_id=str(scope.tenant_id),
            task_document_id=str(scope.task_document_id),
            criteria=len(failed),
            linked=sum(1 for ms in kept.values() if ms),
        )
        return {
            criterion_id: tuple(
                # A segment gone between the two reads has no text to show.
                _link(match, contents.get(match.segment.segment_id, ""))
                for match in ms
            )
            for criterion_id, ms in kept.items()
        }

    async def _task_place(self, scope: SubmissionScope) -> MaterialPlace:
        """The task document's place in the learning order.

        A document is met where it begins, so its place is that of a first
        segment. The task's own segments never compete for it: a task is never
        a candidate.
        """
        row = (
            await self._session.execute(
                select(AuthoredDocument.order, AuthoredDocument.created_at).where(
                    AuthoredDocument.id == scope.task_document_id,
                    AuthoredDocument.course_root_id == scope.course_root_id,
                )
            )
        ).one_or_none()
        if row is None:
            msg = (
                f"task document {scope.task_document_id} is not in course "
                f"{scope.course_root_id}"
            )
            raise ValueError(msg)
        return MaterialPlace(
            course_node_id=scope.task_node_id,
            document_order=row.order,
            document_created_at=row.created_at,
            document_id=scope.task_document_id,
            segment_order=0,
        )


def _around(
    segments: Sequence[CandidateSegment],
    tree: Sequence[TreeNode],
    task: MaterialPlace,
) -> tuple[Sequence[CandidateSegment], Sequence[CandidateSegment]]:
    """``segments`` before the task and after it, each in learning order.

    The task's place is sorted among the segments' places by the same
    :func:`in_learning_order` over the same tree; ``segments`` are already in
    that order (``list_candidates``), so its position is where they split.
    """
    places = [segment.place for segment in segments]
    split = in_learning_order([*places, task], tree, place=lambda p: p).index(task)
    return segments[:split], segments[split:]


def _matches(
    side: Sequence[CandidateSegment],
    wanted: Mapping[str, str],
    *,
    later: bool,
    keys: Mapping[uuid.UUID, set[str]],
    relations: Mapping[uuid.UUID, TreeRelation],
) -> list[_Match]:
    """The segments of ``side`` that teach a concept of ``wanted``, scored.

    ``side`` is all the candidates before the task (``later=False``) or after
    it, in learning order; ``wanted`` — the criterion's concept keys with
    their spellings; ``keys`` — each segment's main-concept keys.
    """
    found: list[_Match] = []
    for position, segment in enumerate(side):
        matched = tuple(
            spelling
            for key, spelling in wanted.items()
            if key in keys[segment.segment_id]
        )
        if not matched:
            continue
        confidence = link_confidence(
            coverage=len(matched) / len(wanted),
            relation=relations[segment.place.course_node_id],
            order_closeness=0.0 if later else (position + 1) / len(side),
        )
        found.append(_Match(segment, matched, later, confidence))
    return found


def _criterion_keys(concepts: Sequence[str]) -> dict[str, str]:
    """Each concept key of a criterion with its first spelling, in order."""
    wanted: dict[str, str] = {}
    for concept in concepts:
        wanted.setdefault(normalization_key(concept), concept)
    return wanted


def _link(match: _Match, content: str) -> LinkCandidate:
    segment = match.segment
    return LinkCandidate(
        segment_id=segment.segment_id,
        authored_document_id=segment.authored_document_id,
        material_title=segment.material_title,
        source_type=segment.source_type,
        anchor=segment.anchor,
        excerpt=excerpt_of(content),
        later_in_course=match.later,
        confidence=match.confidence,
        matched_concepts=match.matched,
    )


def build_course_link_finder(session: AsyncSession) -> CourseLinkFinder:
    """The production finder: the concept adapter over ``session``.

    The one place the adapter is chosen; tests of other modules substitute
    this factory.
    """
    return ConceptCourseLinkFinder(session)
