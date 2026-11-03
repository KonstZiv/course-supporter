"""The course links a review gives, checked before it is given (task 10, K6).

Purpose:
    The explanation stage showed the model up to three places of the course
    for each failed criterion, under local identifiers (``c3.l1``), and kept
    what the model chose (``CriterionRemark.links``). Between that choice and
    the review being built the course may change: a segment deleted, a
    material hidden or reprocessed. A link to a place the student cannot open
    is worse than none, so the review's builder resolves each choice to its
    place and checks, once, that the place is still a candidate.

Interface:
    :func:`settle_course_links` — the links of each remark, checked and kept
    (the steps below); what the builder of a text review
    (``homework/text_result.py``) calls before it lays the review out.
    :func:`replacement_for` — the rule of a replacement, pure.
    :func:`reference_of` — a settled place as the review's
    :class:`~course_supporter.models.review_structure.Reference`.

Steps of :func:`settle_course_links`:
    1. The kept row of the search (``submission_course_link_candidates``).
       None — the path did not search: the explanation was shown no place,
       so it chose none, and the review gives no link. Settled already — its
       links are given as they are and nothing is checked again (decision 3):
       a review built twice links the same places, whatever the course did in
       between.
    2. Each remark's choice, resolved: local identifier to the place shown
       under it
       (:meth:`~course_supporter.homework.course_links.FoundCourseLinks.shown`
       — the same definition the stage showed them by). An identifier that
       resolves to nothing is skipped with a warning: the explanation stage
       cut every identifier it had not shown, so this is a defect, not a
       choice.
    3. One read: which of the chosen places are candidates now
       (:meth:`~course_supporter.storage.document_segment_repository.DocumentSegmentRepository.still_candidates`
       — the rule of the search itself, not a second one).
    4. For each criterion with a chosen place gone: a new search for that
       criterion alone, with its text and concepts as the list it was judged
       against states them (``build_course_link_finder``). The replacement
       (:func:`replacement_for`) is a place of the same material first, else
       the most confident. Nothing — the link is left out, and a row in the
       call register says so without a model call (``action =
       course_link_stale``; the criterion and the local identifiers only,
       nothing the student wrote).
    5. A place whose material has no title — empty or blank — is not given:
       the review would print a link with nothing to call it by. The log
       names its fragment; the review is built without it.
    6. What is given is kept in the same row
       (:attr:`~course_supporter.homework.course_links.FoundCourseLinks.settled`),
       written in the build's session: committed together with the review it
       belongs to.

What the check does not reach (accepted, task 10, K6):
    The text of a remark. The explanation may name a place it was shown —
    by its material's title and position, in ``why`` or ``todo`` — and that
    text is the model's, kept as written. When the place is replaced or left
    out here, the text still names the old one while ``read`` points at the
    new one or at none. The window is the minutes between the search and
    the review's build, and a place deleted or hidden within it is rare; the
    discrepancy is accepted rather than mended by rewriting the model's text.

Replacing:
    The rule of a replacement is :func:`replacement_for` alone; the search
    is the port's (``homework/course_links.py``), whichever adapter
    ``build_course_link_finder`` returns.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence, Set
from typing import TYPE_CHECKING

import structlog

from course_supporter.homework.course_link_position import link_position
from course_supporter.homework.course_links import (
    CriterionLinks,
    FailedCriterion,
    FoundCourseLinks,
    LinkCandidate,
    build_course_link_finder,
    submission_scope,
)
from course_supporter.homework.judged_list import read_judged_list
from course_supporter.models.review_structure import Reference
from course_supporter.service_logging import CourseLinkTrace, record_course_link_trace
from course_supporter.storage.document_segment_repository import (
    DocumentSegmentRepository,
)
from course_supporter.storage.submission_verdict_repository import (
    SubmissionVerdictRepository,
)

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from course_supporter.homework.verdict_explanation import ExplanationAnswer
    from course_supporter.storage.orm import (
        HomeworkSubmission,
        SubmissionCriterionVerdict,
    )

__all__ = ["reference_of", "replacement_for", "settle_course_links"]

logger = structlog.get_logger(__name__)

_Chosen = tuple[tuple[str, LinkCandidate], ...]
"""A remark's choice, resolved: each local identifier with its place."""


async def settle_course_links(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    submission: HomeworkSubmission,
    answer: ExplanationAnswer,
    rows: Sequence[SubmissionCriterionVerdict],
) -> dict[str, tuple[LinkCandidate, ...]]:
    """The places each remark of ``answer`` links to, checked and kept.

    The steps are the module's. The settled links are written in
    ``session`` and flushed, never committed: the caller commits them with
    the review. A trace of a link left out goes through ``session_factory``,
    in a session of its own, as every row of the call register does.

    Args:
        session: The submission's session.
        session_factory: For the register's rows.
        submission: The submission whose review is built.
        answer: The explanation as it is kept — its unshown links cut.
        rows: The submission's verdict rows, for the list a new search reads.

    Returns:
        By criterion id, the places its remark links to, in the model's
        order; empty for a remark without links. Empty when the path did
        not search.

    Raises:
        ValueError: A new search was needed and the rows name no list of
            this task (:func:`~course_supporter.homework.judged_list.read_judged_list`),
            or the finder refused the scope — a defect of the path.
    """
    verdicts = SubmissionVerdictRepository(session)
    kept = await verdicts.get_candidates(
        tenant_id=submission.tenant_id, submission_id=submission.id
    )
    if kept is None:
        return {}
    found = FoundCourseLinks.model_validate(kept.body)
    if found.settled is not None:
        return {given.item_id: given.candidates for given in found.settled}

    chosen = _chosen(answer, found, submission_id=submission.id)
    live = await DocumentSegmentRepository(session).still_candidates(
        course_root_id=submission.course_node_id,
        segment_ids={
            place.segment_id for places in chosen.values() for _, place in places
        },
    )
    gone = sorted(
        criterion_id
        for criterion_id, places in chosen.items()
        if any(place.segment_id not in live for _, place in places)
    )
    fresh = await _search_again(session, submission, rows, gone) if gone else {}

    settled: dict[str, tuple[LinkCandidate, ...]] = {}
    stale: dict[str, tuple[str, ...]] = {}
    for criterion_id, places in chosen.items():
        given, left_out = _given(
            places,
            live,
            # A place that cannot be given is no replacement either.
            [place for place in fresh.get(criterion_id, ()) if _titled(place)],
        )
        settled[criterion_id] = _only_titled(
            given, submission_id=submission.id, criterion_id=criterion_id
        )
        if left_out:
            stale[criterion_id] = left_out

    await verdicts.store_candidates(
        tenant_id=submission.tenant_id,
        submission_id=submission.id,
        body=found.model_copy(
            update={
                "settled": tuple(
                    CriterionLinks(item_id=criterion_id, candidates=places)
                    for criterion_id, places in settled.items()
                )
            }
        ).model_dump(mode="json"),
    )
    for criterion_id, ids in stale.items():
        await record_course_link_trace(
            session_factory,
            kind=CourseLinkTrace.STALE,
            criterion_id=criterion_id,
            ids=ids,
        )
    logger.info(
        "course_links_settled",
        submission_id=str(submission.id),
        chosen=sum(len(places) for places in chosen.values()),
        given=sum(len(places) for places in settled.values()),
        replaced=sum(
            1
            for places in settled.values()
            for place in places
            if place.segment_id not in live
        ),
        stale=sum(len(ids) for ids in stale.values()),
    )
    return settled


def replacement_for(
    gone: LinkCandidate,
    found: Iterable[LinkCandidate],
    *,
    taken: Iterable[uuid.UUID] = (),
) -> LinkCandidate | None:
    """The place to give instead of ``gone``, or ``None`` when there is none.

    A place of ``gone``'s own material first — the material the model chose
    from, most likely still about the same thing — the most confident of
    them; else the most confident place found. A place in ``taken`` — one
    the remark already gives — is never given twice.

    Args:
        gone: The chosen place that is no candidate any more.
        found: What a new search for its criterion found.
        taken: The segments the remark gives already.

    >>> import uuid
    >>> from course_supporter.storage.document_segment_repository import (
    ...     SegmentAnchor,
    ... )
    >>> def place(material: str, confidence: float) -> LinkCandidate:
    ...     return LinkCandidate(
    ...         segment_id=uuid.uuid4(),
    ...         authored_document_id=uuid.uuid5(uuid.NAMESPACE_URL, material),
    ...         material_title=material,
    ...         source_type="text",
    ...         anchor=SegmentAnchor("paragraph", 1, 2),
    ...         excerpt=None,
    ...         later_in_course=False,
    ...         confidence=confidence,
    ...         matched_concepts=(),
    ...     )
    >>> gone, other, same = place("a", 0.9), place("b", 0.8), place("a", 0.5)
    >>> replacement_for(gone, [other, same]) is same
    True
    >>> replacement_for(gone, [other, same], taken=[same.segment_id]) is other
    True
    >>> replacement_for(gone, []) is None
    True
    """
    excluded = set(taken)
    open_places = [place for place in found if place.segment_id not in excluded]
    same_material = [
        place
        for place in open_places
        if place.authored_document_id == gone.authored_document_id
    ]
    pool = same_material or open_places
    if not pool:
        return None
    # max() keeps the first of equal confidences: the port's own order.
    return max(pool, key=lambda place: place.confidence)


def reference_of(place: LinkCandidate) -> Reference:
    """A settled place as the review's reference to it.

    The material's title, where in it (:func:`link_position` — a place whose
    anchor does not fit is given without one), the excerpt for the portal,
    the fragment and whether it comes later in the course. No address: the
    portal builds it from the fragment.
    """
    return Reference(
        title=place.material_title,
        url=None,
        position=link_position(
            place.anchor, place.source_type, segment_id=place.segment_id
        ),
        excerpt=place.excerpt,
        segment_id=str(place.segment_id),
        later_in_course=place.later_in_course,
    )


def _chosen(
    answer: ExplanationAnswer, found: FoundCourseLinks, *, submission_id: uuid.UUID
) -> dict[str, _Chosen]:
    """Each remark's chosen identifiers with the places shown under them."""
    chosen: dict[str, _Chosen] = {}
    for remark in answer.remarks:
        shown = dict(found.shown(remark.id))
        places: list[tuple[str, LinkCandidate]] = []
        for local_id in remark.links:
            place = shown.get(local_id)
            if place is None:
                logger.warning(
                    "course_link_unresolved",
                    submission_id=str(submission_id),
                    criterion_id=remark.id,
                    link_id=local_id,
                )
                continue
            places.append((local_id, place))
        chosen[remark.id] = tuple(places)
    return chosen


async def _search_again(
    session: AsyncSession,
    submission: HomeworkSubmission,
    rows: Sequence[SubmissionCriterionVerdict],
    criterion_ids: Sequence[str],
) -> dict[str, tuple[LinkCandidate, ...]]:
    """A new search for each of ``criterion_ids``, one criterion at a time.

    The criterion as the list it was judged against states it — the same
    text and concepts the first search had — and the same scope.
    """
    wanted = set(criterion_ids)
    criteria = await read_judged_list(session, submission, rows)
    finder = build_course_link_finder(session)
    scope = submission_scope(submission)
    fresh: dict[str, tuple[LinkCandidate, ...]] = {}
    for criterion in criteria:
        if criterion.id not in wanted:
            continue
        found = await finder.find(
            scope,
            [
                FailedCriterion(
                    id=criterion.id,
                    text=criterion.text,
                    concepts=tuple(criterion.concepts),
                )
            ],
        )
        fresh[criterion.id] = tuple(found.get(criterion.id, ()))
    return fresh


def _given(
    places: _Chosen,
    live: Set[uuid.UUID],
    fresh: Sequence[LinkCandidate],
) -> tuple[tuple[LinkCandidate, ...], tuple[str, ...]]:
    """One remark's places as given, and the identifiers left out.

    A live place is given as chosen; a gone one gives way to its
    replacement, in its own position of the list, or is left out.
    """
    taken = {place.segment_id for _, place in places if place.segment_id in live}
    given: list[LinkCandidate] = []
    left_out: list[str] = []
    for local_id, place in places:
        if place.segment_id in live:
            given.append(place)
            continue
        replacement = replacement_for(place, fresh, taken=taken)
        if replacement is None:
            left_out.append(local_id)
            continue
        taken.add(replacement.segment_id)
        given.append(replacement)
    return tuple(given), tuple(left_out)


def _titled(place: LinkCandidate) -> bool:
    """Whether the place's material has a title to call the link by."""
    return bool(place.material_title.strip())


def _only_titled(
    places: Sequence[LinkCandidate], *, submission_id: uuid.UUID, criterion_id: str
) -> tuple[LinkCandidate, ...]:
    """``places`` without those whose material has no title (step 5)."""
    kept: list[LinkCandidate] = []
    for place in places:
        if _titled(place):
            kept.append(place)
            continue
        logger.warning(
            "course_link_untitled",
            submission_id=str(submission_id),
            criterion_id=criterion_id,
            segment_id=str(place.segment_id),
        )
    return tuple(kept)
