"""The closed vocabularies of a task's criteria and of the verdicts on them.

Purpose:
    A verdict on a criterion is stored as a row (mentor-rebuild task 09b), and
    the database refuses a row whose words it has not been told about. The
    storage layer must therefore build its ``CHECK`` constraints from the same
    enums the criteria code speaks — and the storage layer does not import
    ``homework/``. These vocabularies live here, below both, for that reason
    alone: the criteria form (``homework/criteria_form.py``) and the criteria
    list service (``homework/criteria_list_service.py``) import them from here,
    and so does ``storage/orm.py``. :class:`RepeatReason` lives here for the
    same reason: the evaluation's rules decide it
    (``homework/criteria_verdicts.py``), and the verdict row keeps it
    (``storage/submission_verdict_repository.py``, ``storage/orm.py``).

Interface:
    :class:`CriteriaLayer` — whose list a criterion comes from.
    :class:`WeightCategory` — how much a criterion matters.
    :class:`VerdictValue` — what a verdict says.
    :class:`VerdictItemKind` — what a verdict is about: a criterion or one of
    its mandatory points.
    :class:`RepeatReason` — why a verdict was asked about once more; the
    verdict row keeps it, so the share and the cost of repeats can be counted
    from the database by reason.
    :data:`CRITERION_ID_PATTERN`, :data:`POINT_ID_PATTERN` — the shape of their
    identifiers.

Extending:
    A new member is a new enum value AND a migration re-issuing every ``CHECK``
    built from that enum — Postgres cannot widen a CHECK in place, and the
    database refuses a value it has not been told about, so Python and SQL
    cannot drift apart silently. A new member of :class:`VerdictValue` (a third
    state such as "partly") is more than that: the score formula and the pass
    rule of task 09b assume exactly two states (``PRE-PLAN.md``, decision 1).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class CriteriaLayer(StrEnum):
    """Which layer a list in force comes from (task 08).

    Half of a verdict's address: identifiers are stable within one list, not
    across the lists of one task version (task 08, ``TASK.md`` section 9,
    decision 21), so a verdict names its criterion by the list's layer and row
    together with the identifier.
    """

    AUTHOR = "author"
    MODEL = "model"


class WeightCategory(StrEnum):
    """How much a criterion matters (task 08, ``TASK.md`` 3.1).

    The category is what the author edits; the number it counts for is the
    code's (``homework.criteria_form.WEIGHT_NUMBERS``), so stored lists hold no
    numbers and a new scale re-weighs every list at once. The values are the
    English labels the model is given (task 08, decision 18) and stay so in
    storage.
    """

    MUST = "must"
    SHOULD = "should"
    MAY = "may"


class VerdictValue(StrEnum):
    """What a verdict on a criterion or a mandatory point says (task 09b).

    Two states and no more (``PRE-PLAN.md``, decision 1): shades of "done
    partly" or "done beyond what was asked" are carried by the Mentor's words,
    not by a state or a score.
    """

    MET = "met"
    NOT_MET = "not_met"


class VerdictItemKind(StrEnum):
    """What one verdict row is about (task 09b).

    A criterion checked by a model verdict gets its verdict from the model; a
    criterion checked by mandatory points gets one verdict per point from the
    model and its own verdict from the code — met only when every point is met
    (``PRE-PLAN.md``, decision 3).
    """

    CRITERION = "criterion"
    POINT = "point"


class RepeatReason(StrEnum):
    """Why a verdict cannot stand as given and is asked about once more (09b).

    ``homework/criteria_verdicts.py`` decides it (``_repeat_reason``) on the
    model's first answer, and names it to the model in the repeated request;
    the item's verdict row keeps it (``repeat_reason`` of
    ``submission_criterion_verdicts``) — the trace of a repeat, which the
    stage's register rows (``external_service_calls``) price.
    """

    NO_QUOTE = "no_quote"
    """A "met" without a quote."""
    QUOTE_NOT_ONE_LINE = "quote_not_one_line"
    """A "met" whose quote spans several lines."""
    QUOTE_TOO_LONG = "quote_too_long"
    """A "met" whose quote is longer than ``MAX_QUOTE_CHARS`` characters."""
    QUOTE_TOO_SHORT = "quote_too_short"
    """Too short to prove anything: counted as a quote not found (9.4)."""
    QUOTE_NOT_FOUND = "quote_not_found"
    """A "met" whose quote is not in the work word for word."""
    QUOTE_FROM_BASE = "quote_from_base"
    """The quote is the author's: in a file of the project's base (K3)."""
    NO_SENTENCE = "no_sentence"
    """A "not met" without a sentence on what is missing."""


CRITERION_ID_PATTERN: Final = r"^c[1-9][0-9]*$"
"""A criterion's identifier — ``c1``, ``c2``, … — assigned by the code."""

POINT_ID_PATTERN: Final = r"^c[1-9][0-9]*\.p[1-9][0-9]*$"
"""A mandatory point's identifier — ``c3.p1``, … — inside its criterion."""
