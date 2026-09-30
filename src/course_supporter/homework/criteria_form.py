"""The criterion form of task 08: what a task's criteria list is made of.

Purpose:
    A task version's criteria list is composed once, by a model, and then read
    by every review of that version, by the author's edit routes and — from
    task 09 on — by verdicts that point at single criteria and points. All of
    them read the one form defined here: data models and pure functions, no
    session and no model call, so the agent that composes a list, the service
    that stores it and the routes that edit it judge a document the same way.

Interface:
    :class:`Criterion` and :class:`MandatoryPoint` — a criterion as stored in
    ``task_criteria_lists.criteria`` and ``task_criteria_overrides.criteria``.
    :class:`CriterionDraft` — a criterion as its composer writes it: everything
    but the identifiers, which are the code's.
    :func:`compose_criteria` — drafts to criteria: identifiers assigned,
    concepts kept only when the input had them (:func:`keep_input_concepts`).
    Its result, :class:`CriteriaComposition`, is what one composition yields.
    :func:`criteria_from_document` and :func:`criteria_to_document` — the
    stored JSON document and back, the whole form checked on the way in.
    :func:`check_methods_for` — the check methods a task type admits.

Extending:
    The form is versioned: :data:`CRITERIA_FORM_VERSION` is stored with every
    list, and the form is the response contract of
    ``prompts/criteria_decomposition/v2.md``. A new form is a new prompt file,
    a new version number and readers that know both — never an in-place edit
    of this one, because lists composed in it stay in storage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Final, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

from course_supporter.concept_dedup import normalization_key
from course_supporter.models.source import AssignmentType

CRITERIA_FORM_VERSION: Final = 2
"""The form this module defines, as ``task_criteria_lists.form_version`` stores it.

Version 1 is the ``{statement, evidence}`` pair of ``task_criteria`` and of
``prompts/criteria_decomposition/v1.md``; version 2 adds identifiers, weight
categories, check methods, concepts and mandatory points.
"""

# The limits of decision 17 (``TASK.md`` section 9). The decision sets them for
# the author's edit; they bind the model's list as well, because the author
# edits by replacing the WHOLE list: a list the model may hold but the author
# may not send back would have the author's first edit refused over text the
# author never wrote.
MAX_CRITERIA: Final = 60
MAX_TEXT_CHARS: Final = 600
"""A criterion's text and its evidence, each."""
MAX_POINTS: Final = 10
"""Mandatory points of one criterion."""
MAX_POINT_CHARS: Final = 300
MAX_CONCEPTS: Final = 10
"""Concepts of one criterion."""


class WeightCategory(StrEnum):
    """How much a criterion matters (``TASK.md`` 3.1).

    The category is what the author edits; the number it counts for is the
    code's (:data:`WEIGHT_NUMBERS`), so stored lists hold no numbers and a new
    scale re-weighs every list at once. The values are the English labels the
    model is given (decision 18) and stay so in storage.
    """

    MUST = "must"
    SHOULD = "should"
    MAY = "may"


WEIGHT_NUMBERS: Final[Mapping[WeightCategory, int]] = MappingProxyType(
    {WeightCategory.MUST: 3, WeightCategory.SHOULD: 2, WeightCategory.MAY: 1}
)
"""What each weight category counts for (``TASK.md`` 3.1)."""


class CheckMethod(StrEnum):
    """How a criterion is checked (``TASK.md`` 3.1).

    * ``MODEL_VERDICT`` — a reviewing model judges the submission against the
      criterion's text and evidence.
    * ``MANDATORY_POINTS`` — the criterion holds when each of its mandatory
      points is present; the points travel with the criterion.
    * ``CODE_TEST`` — a check by running code. Nothing runs code tests in the
      first echelon, so every such criterion carries ``soft_descent`` and is
      judged by a model verdict meanwhile (``03-BINDING.md`` 2.4).
    """

    MODEL_VERDICT = "model_verdict"
    MANDATORY_POINTS = "mandatory_points"
    CODE_TEST = "code_test"


def check_methods_for(task_type: str) -> frozenset[CheckMethod]:
    """The check methods a criterion of a task of ``task_type`` may have.

    ``code_test`` belongs to projects alone: a short task and a task never get
    one (decision 15), and neither does a test, which is answered by choosing
    options rather than by code.

    >>> sorted(method.value for method in check_methods_for("project"))
    ['code_test', 'mandatory_points', 'model_verdict']
    >>> sorted(method.value for method in check_methods_for("task"))
    ['mandatory_points', 'model_verdict']
    """
    if task_type == AssignmentType.PROJECT:
        return frozenset(CheckMethod)
    return frozenset(CheckMethod) - {CheckMethod.CODE_TEST}


def criterion_id(number: int) -> str:
    """The identifier of the ``number``-th criterion of a list, counted from 1.

    >>> criterion_id(3)
    'c3'
    """
    return f"c{number}"


def point_id(criterion: str, number: int) -> str:
    """The identifier of a criterion's ``number``-th mandatory point, from 1.

    >>> point_id("c3", 2)
    'c3.p2'
    """
    return f"{criterion}.p{number}"


_CRITERION_ID = r"^c[1-9][0-9]*$"
_POINT_ID = r"^c[1-9][0-9]*\.p[1-9][0-9]*$"

_Text = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TEXT_CHARS),
]
_PointText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_POINT_CHARS),
]
_Concept = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _points_follow_method(method: CheckMethod, points: int) -> None:
    """Mandatory points belong to the ``mandatory_points`` method, and it to them."""
    if method is CheckMethod.MANDATORY_POINTS and not points:
        raise ValueError(
            "check_method 'mandatory_points' needs a non-empty mandatory_points list"
        )
    if method is not CheckMethod.MANDATORY_POINTS and points:
        raise ValueError(
            "mandatory_points must be empty unless check_method is "
            f"'mandatory_points' (it is {method.value!r})"
        )


class MandatoryPoint(BaseModel):
    """One mandatory point of a criterion — one thing a reviewer finds or not."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, StringConstraints(pattern=_POINT_ID)]
    text: _PointText


class Criterion(BaseModel):
    """One criterion of a task version, as storage keeps it and readers take it.

    ``id`` is the code's, unique in its list and fixed for the list's life; a
    mandatory point's ``id`` extends it (``c3`` → ``c3.p1``), so a point can be
    named on its own and still says whose it is. The weight number is
    computed, never stored.

    >>> criterion = Criterion(
    ...     id="c1",
    ...     text="Handles an empty list",
    ...     evidence="returns [] for []",
    ...     weight="should",
    ...     check_method="model_verdict",
    ...     soft_descent=False,
    ...     concepts=(),
    ...     mandatory_points=(),
    ... )
    >>> criterion.weight_number
    2
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, StringConstraints(pattern=_CRITERION_ID)]
    text: _Text
    evidence: _Text
    weight: WeightCategory
    check_method: CheckMethod
    soft_descent: bool
    concepts: Annotated[tuple[_Concept, ...], Field(max_length=MAX_CONCEPTS)]
    mandatory_points: Annotated[
        tuple[MandatoryPoint, ...], Field(max_length=MAX_POINTS)
    ]

    @property
    def weight_number(self) -> int:
        """What the criterion counts for, by its weight category."""
        return WEIGHT_NUMBERS[self.weight]

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        _points_follow_method(self.check_method, len(self.mandatory_points))
        # Nothing runs code tests in the first echelon, so the mark and the
        # method go together; an executor would make code_test without the
        # mark legal, and this rule is where that change lands.
        if self.soft_descent != (self.check_method is CheckMethod.CODE_TEST):
            raise ValueError(
                "soft_descent marks exactly the code_test criteria while "
                "nothing runs code tests"
            )
        prefix = f"{self.id}.p"
        ids = [point.id for point in self.mandatory_points]
        if not all(i.startswith(prefix) for i in ids):
            raise ValueError(f"the point ids of {self.id} must start with {prefix!r}")
        if len(set(ids)) != len(ids):
            raise ValueError(f"the point ids of {self.id} repeat")
        return self


class CriterionDraft(BaseModel):
    """A criterion as its composer writes it: everything but the identifiers.

    The model answers with one draft per criterion
    (``prompts/criteria_decomposition/v2.md``). Identifiers and the
    soft-descent mark are not fields — :func:`compose_criteria` assigns both —
    so a draft that brings its own is refused as an extra field rather than
    trusted; mandatory points are bare texts for the same reason. ``concepts``
    is the composer's claim, and :func:`keep_input_concepts` decides which of
    them survive.

    The two lists default to empty: an answer that leaves out an empty list
    says the same as ``[]``, and refusing it would buy a paid retry for no
    information.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: _Text
    evidence: _Text
    weight: WeightCategory
    check_method: CheckMethod
    mandatory_points: Annotated[
        tuple[_PointText, ...], Field(max_length=MAX_POINTS)
    ] = ()
    concepts: Annotated[tuple[_Concept, ...], Field(max_length=MAX_CONCEPTS)] = ()

    @model_validator(mode="after")
    def _check_points(self) -> Self:
        _points_follow_method(self.check_method, len(self.mandatory_points))
        return self


@dataclass(frozen=True, slots=True)
class CriteriaComposition:
    """What one composition of a task version's criteria list yields.

    Of the list-level form of ``TASK.md`` 3.1, the parts that come out of the
    model's answer. The rest describes the answer's input, which the list
    service gathers and records beside it: the input fingerprint and whether
    a node or root summary, the source of concepts, was available
    (``input_fingerprint`` and ``concepts_in_input`` of
    ``task_criteria_lists``). The form version is
    :data:`CRITERIA_FORM_VERSION`.

    Attributes:
        criteria: The criteria, identified by code, each concept one the
            input had.
        contradictions: Where the task contradicts its node's description —
            for the author only, never a criterion and never a score.
        dropped_concept_count: Concepts the model named that the input did
            not have, dropped by code.
    """

    criteria: tuple[Criterion, ...]
    contradictions: tuple[str, ...]
    dropped_concept_count: int


def _concept_index(input_concepts: Sequence[str]) -> dict[str, str]:
    """Grouping key → the input's spelling; the first spelling of a key wins."""
    index: dict[str, str] = {}
    for concept in input_concepts:
        index.setdefault(normalization_key(concept), concept)
    return index


def _keep(
    named: Sequence[str], index: Mapping[str, str]
) -> tuple[tuple[str, ...], int]:
    kept: list[str] = []
    seen: set[str] = set()
    dropped = 0
    for concept in named:
        key = normalization_key(concept)
        if key in seen:
            continue
        seen.add(key)
        spelling = index.get(key)
        if spelling is None:
            dropped += 1
        else:
            kept.append(spelling)
    return tuple(kept), dropped


def keep_input_concepts(
    named: Sequence[str], input_concepts: Sequence[str]
) -> tuple[tuple[str, ...], int]:
    """Keep the concepts a composer named that its input had; count the rest.

    A list may refer only to concepts from its input (``TASK.md`` 3.2); one
    from anywhere else is dropped here, not refused, and counted. Concepts are
    compared on :func:`~course_supporter.concept_dedup.normalization_key`, so
    case, hyphens and a plural ``s`` do not make a new concept, and a kept one
    takes the INPUT's spelling: every list names a concept exactly as the
    course does, and a lookup by that name finds it. A concept named twice is
    kept, or counted, once.

    Args:
        named: The concepts as the composer wrote them.
        input_concepts: The concepts the composer was given.

    Returns:
        The kept concepts in the order they were named, and how many named
        concepts were dropped.

    >>> keep_input_concepts(
    ...     ["html templates", "Recursion", "Monads"], ["HTML Template", "recursion"]
    ... )
    (('HTML Template', 'recursion'), 1)
    """
    return _keep(named, _concept_index(input_concepts))


def compose_criteria(
    drafts: Sequence[CriterionDraft],
    contradictions: Sequence[str],
    *,
    input_concepts: Sequence[str],
) -> CriteriaComposition:
    """Turn a composer's drafts into criteria of the stored form.

    * Identifiers are the code's: the ``n``-th draft becomes ``c{n}`` and its
      ``m``-th mandatory point ``c{n}.p{m}``, so the same drafts always get
      the same identifiers and a draft has no way to bring its own.
    * Concepts survive only if the input had them (:func:`keep_input_concepts`);
      the dropped ones are counted over the whole list.
    * ``soft_descent`` marks every ``code_test`` criterion (see
      :class:`CheckMethod`).

    Args:
        drafts: The criteria as composed, in the composer's order.
        contradictions: The contradictions the composer reported.
        input_concepts: Every concept the composer was given.

    Returns:
        The composition, its criteria checked as one stored document.
    """
    index = _concept_index(input_concepts)
    criteria: list[Criterion] = []
    dropped = 0
    for number, draft in enumerate(drafts, start=1):
        identifier = criterion_id(number)
        concepts, lost = _keep(draft.concepts, index)
        dropped += lost
        criteria.append(
            Criterion(
                id=identifier,
                text=draft.text,
                evidence=draft.evidence,
                weight=draft.weight,
                check_method=draft.check_method,
                soft_descent=draft.check_method is CheckMethod.CODE_TEST,
                concepts=concepts,
                mandatory_points=tuple(
                    MandatoryPoint(id=point_id(identifier, place), text=text)
                    for place, text in enumerate(draft.mandatory_points, start=1)
                ),
            )
        )
    return CriteriaComposition(
        criteria=_DOCUMENT.validate_python(tuple(criteria)),
        contradictions=tuple(contradictions),
        dropped_concept_count=dropped,
    )


def _unique_ids(criteria: tuple[Criterion, ...]) -> tuple[Criterion, ...]:
    ids = [criterion.id for criterion in criteria]
    if len(set(ids)) != len(ids):
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"criterion ids repeat: {repeated}")
    return criteria


# The whole-list rules of a stored document: 1 to MAX_CRITERIA criteria, ids
# unique. A document is never empty — the model's list has a semantic minimum
# and an empty edit is refused by the database (``TASK.md`` section 9, 20).
_DOCUMENT: Final[TypeAdapter[tuple[Criterion, ...]]] = TypeAdapter(
    Annotated[
        tuple[Criterion, ...],
        Field(min_length=1, max_length=MAX_CRITERIA),
        AfterValidator(_unique_ids),
    ]
)


def criteria_from_document(
    document: Sequence[Mapping[str, Any]],
) -> tuple[Criterion, ...]:
    """Read a stored criteria document, checking the whole form.

    Raises:
        pydantic.ValidationError: The document is not a list of this form.
    """
    return _DOCUMENT.validate_python(document)


def criteria_to_document(criteria: Sequence[Criterion]) -> list[dict[str, Any]]:
    """The JSON document ``criteria`` are stored as — checked as a whole first.

    Raises:
        pydantic.ValidationError: The criteria break a whole-list rule.
    """
    checked = _DOCUMENT.validate_python(tuple(criteria))
    return [criterion.model_dump(mode="json") for criterion in checked]
