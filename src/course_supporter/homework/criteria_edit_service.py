"""The author's edit of a task's criteria list (mentor-rebuild task 08).

Purpose:
    The author reads what the criteria of a task version are — the model's
    list, their own edit, the list in force and the contradictions the model
    found — and replaces the edit whole or resets it (``TASK.md`` 3.6). This
    service decides what the three routes of ``api/routes/criteria.py``
    answer; the routes are HTTP around it.

Interface:
    :class:`CriteriaEditService` — :meth:`~CriteriaEditService.read`,
    :meth:`~CriteriaEditService.replace` and
    :meth:`~CriteriaEditService.reset`, each answering the
    :class:`CriteriaView` the author reads, or raising
    :class:`CriteriaRefusedError` with one :class:`CriteriaRefusalCode` per
    reason.
    :func:`apply_edit` — the author's criteria against the list they edit:
    identifiers kept or assigned, check methods and concepts checked. Pure:
    the service reads what it needs and stores what it returns.

The rules:
    * Nothing is composed here. No route composes a list (``TASK.md``
      section 9, decision 1): it is composed at the first submission of a
      student's work, and until then the reading says so and an edit is
      refused — there is nothing to edit yet.
    * While no list is in force, the reading says truthfully why — not yet
      asked for, being composed, or not composed and for what reason — how
      many students' works wait for it, and what the author can do
      (:class:`CriteriaReasonCode`; task 09b, ``PRE-FLIGHT.md`` 9.2).
    * Which list is in force is
      :func:`~course_supporter.homework.criteria_list_service.choose_in_force`
      — the rule a review applies. Each layer is shown as that rule sees it on
      its own, so a list or an edit of an earlier version of the task is
      neither in force nor shown: an edit is not carried to a new version.
    * An edit keeps the identifiers of the list in force when it is made
      (decision 21); what the author adds gets identifiers of its own
      (:func:`apply_edit`).
    * A reset soft-deletes the edit, and a replacement does the same to the
      edit it replaces: every earlier edit stays as a snapshot.
    * A test is not served: it is checked by its answer key
      (``reference_service``), not by criteria.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from course_supporter.concept_dedup import normalization_key
from course_supporter.criteria_list_state import CriteriaListState
from course_supporter.homework.criteria_form import (
    CheckMethod,
    Criterion,
    CriterionEdit,
    MandatoryPoint,
    check_methods_for,
    criteria_to_document,
    criterion_id,
    keep_input_concepts,
    point_id,
)
from course_supporter.homework.criteria_list_service import (
    CriteriaInForce,
    choose_in_force,
    has_ready_summary,
    recorded_stop,
)
from course_supporter.llm.error_categories import LadderStop
from course_supporter.models.source import AssignmentType
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.node_summary_final_repository import (
    NodeSummaryFinalRepository,
)
from course_supporter.storage.orm import AuthoredDocument, CourseNode, NodeSummaryFinal
from course_supporter.storage.task_criteria_list_repository import (
    TaskCriteriaListRepository,
)
from course_supporter.storage.task_criteria_override_repository import (
    TaskCriteriaOverrideRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_TEXT_TASK_TYPES: Final = frozenset(
    {
        AssignmentType.SHORT_TASK.value,
        AssignmentType.TASK.value,
        AssignmentType.PROJECT.value,
    }
)
"""The assignment types a criteria list belongs to — every one but the test."""


class CriteriaRefusalCode(StrEnum):
    """Why a request about a task's criteria is refused — one code per reason.

    The code is what crosses to the surface, which picks its own words for
    the author (``language-rules.md``); ``details`` is for whoever reads the
    response or the logs.
    """

    NOT_A_TEXT_TASK = "NOT_A_TEXT_TASK"
    """Not a short task, a task or a project: a test is checked by its key."""

    TASK_NOT_READY = "TASK_NOT_READY"
    """The task has not finished processing: it has no version yet."""

    AWAITING_FIRST_SUBMISSION = "AWAITING_FIRST_SUBMISSION"
    """No list is in force yet: there is nothing to edit (the reading says why)."""

    UNKNOWN_CRITERION_ID = "UNKNOWN_CRITERION_ID"
    """An identifier the list being edited does not have — a criterion's or
    a mandatory point's."""

    CHECK_METHOD_NOT_ADMITTED = "CHECK_METHOD_NOT_ADMITTED"
    """A check method the task type does not admit (``code_test`` outside a
    project)."""

    UNKNOWN_CONCEPT = "UNKNOWN_CONCEPT"
    """A concept outside those a criterion of the task may name."""


class CriteriaRefusedError(Exception):
    """A refusal the author can act on, carrying its code and its details.

    Raised before anything is written, so a refused request stores nothing.
    """

    def __init__(self, code: CriteriaRefusalCode, details: str) -> None:
        super().__init__(f"{code.value}: {details}")
        self.code = code
        self.details = details


class CriteriaStatus(StrEnum):
    """Whether a list is in force for the task's current version, and if not, why."""

    AWAITING_FIRST_SUBMISSION = "awaiting_first_submission"
    """None yet, and nothing has asked for one: no student's work waits."""

    COMPOSING = "composing"
    """One is being composed for this version right now (task 09b)."""

    NOT_COMPOSED = "not_composed"
    """None, though students' works wait for one, or the last attempt failed;
    the reason code says why (task 09b)."""

    READY = "ready"


class CriteriaReasonCode(StrEnum):
    """Why no list is composed for the task's version — the key of the advice.

    A code, not a sentence: the surface picks its own words by it
    (language-rules); the reading carries the Ukrainian sentence of
    :data:`NOT_COMPOSED_MESSAGES` beside it, for the author who reads the
    response as it is. Every advice leads somewhere in this system — none
    suggests editing the task's text, which makes a new version and leaves the
    works already waiting where they are.
    """

    TASK_NOT_PROCESSED = "task_not_processed"
    """The task's text has no ready summary: there is nothing to compose from."""

    MODELS_UNAVAILABLE = "models_unavailable"
    """The last attempt's ladder gave up for reasons that may pass."""

    LIMIT_REACHED = "limit_reached"
    """The last attempt met a ceiling — of output or of money; the same input
    meets it again until the configuration changes."""

    COMPOSITION_ERROR = "composition_error"
    """The last attempt broke on a defect, not on a model's answer."""

    NOT_ATTEMPTED = "not_attempted"
    """Works wait, and no attempt was made for this version yet."""


AWAITING_FIRST_SUBMISSION_MESSAGE: Final = (
    "Перелік критеріїв буде складено після першої подачі роботи студентом на "
    "перевірку, після цього його можна поправити."
)
"""What the author reads while no list is in force (``TASK.md`` section 9,
decision 1) — a sentence in Ukrainian, as the decision sets it; the status
beside it is the code a surface picks its own words by."""

COMPOSING_MESSAGE: Final = (
    "Перелік критеріїв складається зараз. Роботи студентів, що чекають на "
    "нього, буде перевірено, щойно він буде готовий; після цього перелік можна "
    "поправити."
)
"""What the author reads while a list is being composed (task 09b)."""

# The texts the operator and vision-side approved, verbatim (2026-10-02).
# A retry comes without anyone's hand only with a new work's evaluation
# (PRE-FLIGHT.md 9.2, entry 1); otherwise the advice names the one who can
# start it — an administrator, whose restart of the worker runs the startup
# pass (entry 3).
NOT_COMPOSED_MESSAGES: Final[dict[CriteriaReasonCode, str]] = {
    CriteriaReasonCode.TASK_NOT_PROCESSED: (
        "Перелік критеріїв ще не складено: обробку тексту завдання не завершено. Якщо "
        "обробка ще триває — дочекайтеся її завершення; якщо вона завершилася з "
        "помилкою — запустіть її знову. Після цього перелік складеться автоматично, "
        "щойно хтось зі студентів подасть роботу на це завдання, і тоді ж буде "
        "перевірено роботи, що чекають. Якщо нових робіт найближчим часом не буде — "
        "зверніться до адміністратора системи, і він запустить повторну спробу."
    ),
    CriteriaReasonCode.MODELS_UNAVAILABLE: (
        "Перелік критеріїв не вдалося скласти: сервіс, що його складає, був тимчасово "
        "недоступний. Від вас нічого не потрібно: повторна спроба відбудеться "
        "автоматично, щойно хтось зі студентів подасть роботу на це завдання, і тоді ж "
        "буде перевірено роботи, що чекають. Якщо нових робіт найближчим часом не буде "
        "або стан не зміниться — зверніться до адміністратора системи."
    ),
    CriteriaReasonCode.LIMIT_REACHED: (
        "Перелік критеріїв не вдалося скласти: завдання завелике для обмежень, що "
        "зараз діють у системі. Зверніться до адміністратора системи: після зміни "
        "обмежень він запустить повторну спробу, і тоді ж буде перевірено роботи, що "
        "чекають. Не правте заради цього текст завдання: правка створить нову версію "
        "завдання й не допоможе роботам, що вже чекають."
    ),
    CriteriaReasonCode.COMPOSITION_ERROR: (
        "Перелік критеріїв не вдалося скласти через внутрішню помилку системи. "
        "Зверніться до адміністратора системи: після виправлення він запустить "
        "повторну спробу, і тоді ж буде перевірено роботи, що чекають."
    ),
    CriteriaReasonCode.NOT_ATTEMPTED: (
        "Перелік критеріїв для поточної версії завдання ще не складався, а роботи "
        "студентів уже чекають на нього. Він складеться автоматично, щойно хтось зі "
        "студентів подасть роботу на це завдання, і тоді ж буде перевірено роботи, що "
        "чекають. Якщо нових робіт найближчим часом не буде — зверніться до "
        "адміністратора системи, і він запустить повторну спробу."
    ),
}
"""What the author reads when no list is composed — the state and what to do.

Total over :class:`CriteriaReasonCode`, and guarded below: a reason without
an advice would reach the author as a state with nothing to do about it.
"""

_unadvised = set(CriteriaReasonCode) - set(NOT_COMPOSED_MESSAGES)
if _unadvised:  # pragma: no cover — test-locked
    msg = f"Reason codes without an advice: {sorted(c.value for c in _unadvised)}"
    raise RuntimeError(msg)

_REASON_FOR_STOP: Final[dict[LadderStop, CriteriaReasonCode]] = {
    LadderStop.EXHAUSTED: CriteriaReasonCode.MODELS_UNAVAILABLE,
    LadderStop.OUTPUT_CEILING: CriteriaReasonCode.LIMIT_REACHED,
    LadderStop.MONEY_CEILING: CriteriaReasonCode.LIMIT_REACHED,
}
"""How a composition's ending becomes the author's reason; a defect has none."""

_unread_stops = set(LadderStop) - set(_REASON_FOR_STOP)
if _unread_stops:  # pragma: no cover — test-locked
    msg = f"Ladder endings without a reason: {sorted(s.value for s in _unread_stops)}"
    raise RuntimeError(msg)


@dataclass(frozen=True, slots=True)
class CriteriaView:
    """What the author reads about the criteria of a task's current version.

    Attributes:
        status: Whether a list is in force, and if not, why.
        reason_code: Why none is composed (``not_composed`` only).
        waiting_submissions: How many students' works wait for a list — live
            revisions of the task held in ``awaiting_criteria``.
        message: While none is in force, the state and what to do; None
            otherwise.
        model: The model's list for this version, if it is composed.
        author: The author's edit for this version, if there is one.
        in_force: The list a review uses — the edit if there is one, else the
            model's list.
        contradictions: Where the task contradicts its node's description, as
            the composition of the model's list found — for the author only.
        concepts: The concepts a criterion of the task may name: the main
            concepts of its node and course root, then those the list in force
            or the model's list names beyond them.
    """

    status: CriteriaStatus
    reason_code: CriteriaReasonCode | None
    waiting_submissions: int
    message: str | None
    model: tuple[Criterion, ...] | None
    author: tuple[Criterion, ...] | None
    in_force: CriteriaInForce | None
    contradictions: tuple[str, ...]
    concepts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Task:
    """A live text task that has a version; its version key, known to be set."""

    document: AuthoredDocument
    content_hash: str
    task_type: str


class CriteriaEditService:
    """What the author's three routes answer about a task's criteria list."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the service to the request's session; the caller commits."""
        self._session = session

    async def read(self, authored_document_id: uuid.UUID) -> CriteriaView:
        """The criteria of the task's current version; writes nothing.

        Raises:
            CriteriaRefusedError: ``NOT_A_TEXT_TASK`` or ``TASK_NOT_READY``.
        """
        task = await self._require_text_task(authored_document_id)
        return await self._view(task)

    async def replace(
        self, authored_document_id: uuid.UUID, edits: Sequence[CriterionEdit]
    ) -> CriteriaView:
        """Make ``edits`` the author's edit of the task's current version.

        The edit is made on the list in force and keeps its identifiers
        (:func:`apply_edit`); the edit it replaces, of whichever version,
        stays as a snapshot.

        Raises:
            CriteriaRefusedError: ``NOT_A_TEXT_TASK`` or ``TASK_NOT_READY``;
                ``AWAITING_FIRST_SUBMISSION`` when no list is in force; or a
                refusal of :func:`apply_edit`. Nothing is stored.
        """
        task = await self._require_text_task(authored_document_id)
        before = await self._view(task)
        if before.in_force is None:
            raise CriteriaRefusedError(
                CriteriaRefusalCode.AWAITING_FIRST_SUBMISSION,
                "no criteria list is in force for this version of the task — the "
                "reading says why — and an edit edits one",
            )
        criteria = apply_edit(
            edits,
            base=before.in_force.criteria,
            also_taken=before.model or (),
            task_type=task.task_type,
            concepts=before.concepts,
        )
        await TaskCriteriaOverrideRepository(self._session).replace(
            authored_document_id=task.document.id,
            source_content_hash=task.content_hash,
            source_task_type=task.task_type,
            criteria=criteria_to_document(criteria),
        )
        return await self._view(task)

    async def reset(self, authored_document_id: uuid.UUID) -> CriteriaView:
        """Soft-delete the author's edit; the model's list, if any, is in force.

        Resetting a task with no edit is not an error: the state asked for is
        the state that results.

        Raises:
            CriteriaRefusedError: ``NOT_A_TEXT_TASK`` or ``TASK_NOT_READY``.
        """
        task = await self._require_text_task(authored_document_id)
        await TaskCriteriaOverrideRepository(self._session).reset(task.document.id)
        return await self._view(task)

    async def _require_text_task(self, authored_document_id: uuid.UUID) -> _Task:
        """The task, if it is a live text task that has a version."""
        document = await self._session.get(AuthoredDocument, authored_document_id)
        if document is None or document.deleted_at is not None:
            raise CriteriaRefusedError(
                CriteriaRefusalCode.TASK_NOT_READY, "the task does not exist any more"
            )
        task_type = document.task_type
        if task_type is None or task_type not in _TEXT_TASK_TYPES:
            raise CriteriaRefusedError(
                CriteriaRefusalCode.NOT_A_TEXT_TASK,
                "criteria lists belong to task_type 'short_task', 'task' and "
                "'project' documents; a test is checked by its answer key",
            )
        if not document.content_hash:
            raise CriteriaRefusedError(
                CriteriaRefusalCode.TASK_NOT_READY,
                "the task has not finished processing — it has no version yet",
            )
        return _Task(document, document.content_hash, task_type)

    async def _view(self, task: _Task) -> CriteriaView:
        document = task.document
        live = await TaskCriteriaListRepository(self._session).get_live(document.id)
        edit = await TaskCriteriaOverrideRepository(self._session).get_live(document.id)
        # Each layer as the review's rule sees it on its own: a list or an edit
        # of an earlier version is out of force, and so out of view.
        model = choose_in_force(document, machine=live, override=None)
        author = choose_in_force(document, machine=None, override=edit)
        in_force = choose_in_force(document, machine=live, override=edit)
        contradictions = (
            tuple(live.contradictions or ())
            if model is not None and live is not None
            else ()
        )
        waiting = await self._waiting(document)
        status, reason = (
            (CriteriaStatus.READY, None)
            if in_force is not None
            else await self._why_none(task, waiting=waiting)
        )
        return CriteriaView(
            status=status,
            reason_code=reason,
            waiting_submissions=waiting,
            message=_message(status, reason),
            model=model.criteria if model is not None else None,
            author=author.criteria if author is not None else None,
            in_force=in_force,
            contradictions=contradictions,
            concepts=_concepts_to_name(
                await self._course_concepts(document),
                (layer.criteria for layer in (in_force, model) if layer is not None),
            ),
        )

    async def _why_none(
        self, task: _Task, *, waiting: int
    ) -> tuple[CriteriaStatus, CriteriaReasonCode | None]:
        """Why no list is in force for the task's version, read from its rows.

        In this order: an attempt under way; the last attempt failed — by how
        it ended; works wait with no attempt — by whether the task is
        processed; otherwise nothing has asked for a list yet.
        """
        attempt = await TaskCriteriaListRepository(
            self._session
        ).get_latest_for_version(
            task.document.id,
            source_content_hash=task.content_hash,
            source_task_type=task.task_type,
        )
        if (
            attempt is not None
            and attempt.deleted_at is None
            and attempt.state == CriteriaListState.PENDING
        ):
            return CriteriaStatus.COMPOSING, None
        if attempt is not None and attempt.state == CriteriaListState.FAILED:
            stop = recorded_stop(attempt.failure_reason)
            return CriteriaStatus.NOT_COMPOSED, (
                _REASON_FOR_STOP[stop]
                if stop is not None
                else CriteriaReasonCode.COMPOSITION_ERROR
            )
        if waiting:
            processed = await has_ready_summary(self._session, task.document.id)
            return CriteriaStatus.NOT_COMPOSED, (
                CriteriaReasonCode.NOT_ATTEMPTED
                if processed
                else CriteriaReasonCode.TASK_NOT_PROCESSED
            )
        return CriteriaStatus.AWAITING_FIRST_SUBMISSION, None

    async def _waiting(self, document: AuthoredDocument) -> int:
        """How many live revisions of the task wait for its criteria list."""
        node = await self._session.get(CourseNode, document.course_node_id)
        if node is None:  # pragma: no cover — a document always has its node
            return 0
        waiting = await HomeworkRepository(self._session).held_for_criteria(
            tenant_id=node.tenant_id, authored_document_id=document.id
        )
        return len(waiting)

    async def _course_concepts(self, document: AuthoredDocument) -> list[str]:
        """The main concepts of the task's node and its course root.

        The two summaries a composition takes its concepts from
        (``criteria_list_service``); a task attached to the root has one.
        """
        finals = NodeSummaryFinalRepository(self._session)
        node = await finals.get_by_course_node_id(document.course_node_id)
        root = (
            None
            if document.course_root_id == document.course_node_id
            else await finals.get_by_course_node_id(document.course_root_id)
        )
        return [*_main_concepts(node), *_main_concepts(root)]


def _message(status: CriteriaStatus, reason: CriteriaReasonCode | None) -> str | None:
    """The sentence the author reads for a state; None when a list is in force."""
    if status is CriteriaStatus.READY:
        return None
    if status is CriteriaStatus.COMPOSING:
        return COMPOSING_MESSAGE
    if reason is not None:
        return NOT_COMPOSED_MESSAGES[reason]
    return AWAITING_FIRST_SUBMISSION_MESSAGE


def apply_edit(
    edits: Sequence[CriterionEdit],
    *,
    base: Sequence[Criterion],
    also_taken: Sequence[Criterion] = (),
    task_type: str,
    concepts: Sequence[str],
) -> tuple[Criterion, ...]:
    """The author's edit as criteria of the stored form.

    * An identifier names a criterion of ``base`` — the list in force when the
      edit is made — or a mandatory point of that criterion, and is kept
      (``TASK.md`` section 9, decision 21). One that ``base`` does not have is
      refused rather than reissued: it would claim to be a criterion it is not.
    * What comes without an identifier is new. A new criterion gets the number
      after the highest that ``base`` and ``also_taken`` use, so it takes no
      identifier either of them gives another criterion — a criterion of the
      model's list that the author removed included; a new point, the number
      after the highest its criterion's points use there.
    * ``code_test`` only where the task type admits it, marked
      ``soft_descent`` by the code as a composition marks it.
    * A concept only from ``concepts``: compared by its grouping key and kept
      in that list's spelling, as a composition keeps it. One from anywhere
      else is refused, not dropped — the author wrote it and should learn
      that it is not kept.

    Args:
        edits: The author's criteria, in the author's order.
        base: The list the edit is made on.
        also_taken: Another list of the version whose identifiers a new
            criterion must not take — the model's, when ``base`` is an edit.
        task_type: The task's assignment type.
        concepts: The concepts a criterion may name.

    Returns:
        The criteria, in the author's order.

    Raises:
        CriteriaRefusedError: ``UNKNOWN_CRITERION_ID``,
            ``CHECK_METHOD_NOT_ADMITTED`` or ``UNKNOWN_CONCEPT``.
    """
    unknown = _unknown_ids(edits, {criterion.id: criterion for criterion in base})
    if unknown:
        raise CriteriaRefusedError(
            CriteriaRefusalCode.UNKNOWN_CRITERION_ID,
            f"the list being edited has no {unknown}; a new criterion or point "
            "is sent without an id",
        )
    admitted = check_methods_for(task_type)
    not_admitted = [
        f"criteria.{index}"
        for index, edit in enumerate(edits)
        if edit.check_method not in admitted
    ]
    if not_admitted:
        raise CriteriaRefusedError(
            CriteriaRefusalCode.CHECK_METHOD_NOT_ADMITTED,
            f"{not_admitted} use a check method a {task_type!r} task does not "
            f"admit; use one of {sorted(method.value for method in admitted)}",
        )
    course = {normalization_key(concept) for concept in concepts}
    outside = sorted(
        {
            concept
            for edit in edits
            for concept in edit.concepts
            if normalization_key(concept) not in course
        }
    )
    if outside:
        raise CriteriaRefusedError(
            CriteriaRefusalCode.UNKNOWN_CONCEPT,
            f"{outside} are not among the concepts a criterion of this task may "
            "name; the reading lists them under 'concepts'",
        )

    taken = (*base, *also_taken)
    next_number = 1 + max((_criterion_number(c.id) for c in taken), default=0)
    criteria: list[Criterion] = []
    for edit in edits:
        if edit.id is None:
            identifier = criterion_id(next_number)
            next_number += 1
        else:
            identifier = edit.id
        # A kept criterion numbers new points after its own; a new one has
        # none, so it numbers them from 1.
        used = [
            point.id
            for criterion in taken
            if criterion.id == edit.id
            for point in criterion.mandatory_points
        ]
        next_point = 1 + max((_point_number(p) for p in used), default=0)
        points: list[MandatoryPoint] = []
        for point in edit.mandatory_points:
            if point.id is None:
                points.append(
                    MandatoryPoint(id=point_id(identifier, next_point), text=point.text)
                )
                next_point += 1
            else:
                points.append(MandatoryPoint(id=point.id, text=point.text))
        kept, _ = keep_input_concepts(edit.concepts, concepts)
        criteria.append(
            Criterion(
                id=identifier,
                text=edit.text,
                evidence=edit.evidence,
                weight=edit.weight,
                check_method=edit.check_method,
                soft_descent=edit.check_method is CheckMethod.CODE_TEST,
                concepts=kept,
                mandatory_points=tuple(points),
            )
        )
    return tuple(criteria)


def _unknown_ids(
    edits: Sequence[CriterionEdit], known: Mapping[str, Criterion]
) -> list[str]:
    """The identifiers an edit sends that the list being edited does not have."""
    unknown: list[str] = []
    for edit in edits:
        criterion = known.get(edit.id) if edit.id is not None else None
        if edit.id is not None and criterion is None:
            unknown.append(edit.id)
        points = (
            {point.id for point in criterion.mandatory_points}
            if criterion is not None
            else set()
        )
        unknown.extend(
            point.id
            for point in edit.mandatory_points
            if point.id is not None and point.id not in points
        )
    return unknown


def _criterion_number(identifier: str) -> int:
    """The number of a criterion's identifier; the form's pattern fixes its shape.

    >>> _criterion_number("c12")
    12
    """
    return int(identifier.removeprefix("c"))


def _point_number(identifier: str) -> int:
    """The number of a point within its criterion.

    >>> _point_number("c3.p2")
    2
    """
    return int(identifier.rpartition(".p")[2])


def _main_concepts(final: NodeSummaryFinal | None) -> list[str]:
    return list(final.main_concepts or []) if final is not None else []


def _concepts_to_name(
    course: Sequence[str], lists: Iterable[Sequence[Criterion]]
) -> tuple[str, ...]:
    """The course's concepts, then those the given lists name beyond them.

    A list names only concepts its input had, but the course's summaries may
    have changed since it was composed or edited; keeping the lists' own lets
    the author send back what they were shown. One entry per grouping key, in
    the first spelling met — the course's, when it has the concept.
    """
    named = [
        *course,
        *(
            concept
            for criteria in lists
            for criterion in criteria
            for concept in criterion.concepts
        ),
    ]
    # Grouped by the very function an edit's concepts are kept by, so the
    # list the author is shown and the check their edit meets cannot differ.
    kept, _ = keep_input_concepts(named, named)
    return kept
