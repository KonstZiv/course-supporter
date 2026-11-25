"""Prices before an action (BIL-E, part B): what a screen shows, what a route checks.

Purpose:
    The estimate routes and the gates of the submission routes ask the same
    question — what will this action cost its payer, and may it start — and
    get the answer from here. The price is :func:`.charging.price_of`, the one
    function of the threshold; the payer is :func:`.payer.find_payer` — a
    quote is a read and creates no account (BIL-E3): a payer without one reads
    as an empty account. The account comes with the first action that changes
    state (for a review, the start of its run).

    * :func:`review_quote` — a student's review of a task: from the file the
      student is about to send (basis ``file``: the doors read it as the path
      will, nothing is stored) or without one (basis ``typical``: the median
      size of this task's earlier submissions; without them, the configured
      typical size, ``typical_submission.chars`` of
      ``config/cost_estimate.yaml``). 0 for a test while the balance is above
      0 and for the same work again (the fingerprint of the file matches an
      earlier attempt that was reviewed). An author paying under a rule: no
      numbers at all, ``enough`` always — the submission is taken; whether
      its review starts is decided by its run (operator 2026-11-15, 5a, item 1).
    * :func:`review_price` — P of a review whoever pays it, the number behind
      :func:`review_quote` and the continuation of held reviews. When
      anything on the way fails — the choice of the path, the reading of the
      file (a refusal of the doors is not a failure: the route answers it with
      422), the typical size, the estimate, the rate K — the price is the
      ceiling N (basis ``ceiling``, :func:`.charging.ceiling_units`): "no more
      than N", and the fact is charged (BIL-E2, 4a). One WARNING
      ``billing_quote_ceiling`` per failed estimate names the reason, never
      the work.
    * :func:`ceiling_threshold` — what a review priced at its ceiling needs to
      start: the task's typical price, else ``available > 0``; the reserve
      is ``min(N, available)`` (operator 2026-11-15).
    * :func:`author_quote` — an author's processing or task preparation from
      its expected cost.
    * :class:`UnitsRefusedError` — the refusal of an action for want of
      units; a route answers it with 402 in the project's form of a refusal
      with a code: ``{"detail": {"code": "insufficient_units", "details":
      {"price_units", "available_units", "missing_units"}}}``
      (:meth:`UnitsRefusedError.detail_body`).

    ``charging_enabled`` is: charging applies to the payer of this action
    (:func:`.charging.charging_applies` — every payer, or a marked one), AND,
    for a student's review, the path not in test mode; while it is false
    nothing is refused and ``enough`` is true.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal

import structlog
from sqlalchemy import Integer, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing import charging
from course_supporter.billing.estimates import CEILING_WARNING, review_e_usd
from course_supporter.billing.kinds import ActionKind
from course_supporter.homework.path_config import (
    PathConfig,
    PathKey,
    ServedBy,
    get_path_config,
    in_test_mode,
    model_stages,
    path_ceiling_estimate,
)
from course_supporter.models.source import AssignmentType
from course_supporter.storage.orm import (
    ActionCostEstimate,
    AuthoredDocument,
    HomeworkSubmission,
)

logger = structlog.get_logger(__name__)

INSUFFICIENT_UNITS: Final = "insufficient_units"
"""The reason code of a refusal for want of units (a screen picks its words)."""

TYPICAL_HISTORY: Final = 20
"""How many of a task's latest review estimates make its typical size."""

Basis = Literal["typical", "file", "ceiling"]
PaidBy = Literal["self", "author"]


@dataclass(frozen=True, slots=True)
class Quote:
    """The answer of an estimate route (and of a gate).

    ``price_units`` is ``None`` only for a review the author pays for.
    """

    price_units: int | None
    available_units: int | None
    enough: bool
    missing_units: int | None
    charging_enabled: bool
    basis: Basis | None = None
    paid_by: PaidBy | None = None


class UnitsRefusedError(Exception):
    """A route refuses an action its payer cannot cover (402)."""

    def __init__(self, quote: Quote, detail: str) -> None:
        super().__init__(detail)
        self.quote = quote
        self.detail = detail

    def detail_body(self) -> dict[str, Any]:
        """The ``detail`` of the 402: the code and the numbers (``{code, details}``,
        as ``api/routes/_author_shared.py`` and ``criteria.py`` refuse)."""
        return {
            "code": INSUFFICIENT_UNITS,
            "details": {
                "price_units": self.quote.price_units,
                "available_units": self.quote.available_units,
                "missing_units": self.quote.missing_units,
            },
        }


def require_enough(quote: Quote, what: str) -> None:
    """Raise :class:`UnitsRefusedError` when ``quote`` says no start."""
    if not quote.enough:
        raise UnitsRefusedError(
            quote,
            f"{what}: the price is {quote.price_units} units, "
            f"{quote.available_units} are available",
        )


# ── A student's review ────────────────────────────────────────────────


def typical_submission_chars() -> int:
    """The configured typical size of a student's work (``typical_submission``)."""
    from course_supporter.cost_estimate.settings import get_estimate_settings

    return get_estimate_settings().typical_submission.chars


async def _typical_chars(session: AsyncSession, task_id: uuid.UUID) -> int:
    """The median submission size of the task's latest review estimates; without
    any, the configured typical size (``typical_submission.chars``)."""
    sizes = (
        await session.execute(
            select(ActionCostEstimate.inputs["submission_chars"].astext.cast(Integer))
            .join(
                HomeworkSubmission,
                HomeworkSubmission.id == ActionCostEstimate.submission_id,
            )
            .where(
                ActionCostEstimate.action_kind == ActionKind.REVIEW.value,
                HomeworkSubmission.authored_document_id == task_id,
            )
            .order_by(ActionCostEstimate.created_at.desc())
            .limit(TYPICAL_HISTORY)
        )
    ).scalars()
    known = [size for size in sizes if size is not None]
    return int(statistics.median(known)) if known else typical_submission_chars()


async def _same_work(
    session: AsyncSession,
    *,
    task: AuthoredDocument,
    student_id: uuid.UUID,
    tenant_id: uuid.UUID,
    reading: Any,
    config: PathConfig,
    response_language: str | None,
) -> bool:
    """Would this file be answered with an earlier review (task 13, K3)?

    The attempt dedup's own rule — fingerprint, task version, criteria list in
    force, evaluator version, and the language of the review (V-6: a copy only
    in the same language) — read for a work not yet stored. The language is
    the one its review will be written in, from the same function as the
    dedup's (:func:`~course_supporter.homework.review_language.review_language_of`)
    over a probe made of the student, the task and ``response_language``, the
    language the form names (``None``: none named). No language at all is no
    copy. Any error reads as "no": the price is then the usual one. Its reads
    run in a savepoint, so a failed one leaves the session usable.
    """
    from course_supporter.homework.attempt_dedup import _JUDGED_AGAINST_A_LIST
    from course_supporter.homework.attempt_ledger import task_version_of
    from course_supporter.homework.attempts import (
        AttemptWork,
        attempt_snapshotter_for,
    )
    from course_supporter.homework.criteria_list_service import load_in_force
    from course_supporter.homework.evaluator_version import evaluator_version
    from course_supporter.homework.review_language import review_language_of
    from course_supporter.storage.homework_repository import HomeworkRepository
    from course_supporter.storage.orm import HomeworkSubmission

    try:
        task_type = task.task_type or ""
        snapshotter = attempt_snapshotter_for(task_type)
        if not snapshotter.dedup_enabled:
            return False
        work = AttemptWork(reading=reading, snapshot_hash=None, manifest=None)
        fingerprint = snapshotter.snapshot(work).fingerprint
        version = task_version_of(task_type, task.content_hash, reading)
        evaluator = evaluator_version(AssignmentType(task_type), config)
        if fingerprint is None or version is None or evaluator is None:
            return False
        async with session.begin_nested():
            criteria_list: tuple[str, uuid.UUID] | None = None
            if task_type in _JUDGED_AGAINST_A_LIST:
                in_force = await load_in_force(session, task.id)
                if in_force is None:
                    return False
                criteria_list = (in_force.layer, in_force.source_id)
            probe = HomeworkSubmission(
                student_id=student_id,
                authored_document_id=task.id,
                course_node_id=task.course_root_id,
                response_language=response_language,
            )
            review_language = await review_language_of(session, probe)
            if review_language is None:
                return False
            source = await HomeworkRepository(session).latest_unchanged_attempt(
                tenant_id=tenant_id,
                student_id=student_id,
                authored_document_id=task.id,
                excluding=uuid.uuid4(),
                fingerprint=fingerprint,
                task_version=version,
                criteria_list=criteria_list,
                evaluator_version=evaluator,
                review_language=review_language,
            )
    except Exception:
        logger.warning("billing_quote_same_work_failed", exc_info=True)
        return False
    return source is not None and source.attempt_no is not None


@dataclass(frozen=True, slots=True)
class ReviewPrice:
    """P of a review, whoever pays it, and what it was priced from."""

    units: int
    basis: Basis


def _widest_ceiling(config: PathConfig, task_type: AssignmentType) -> Decimal:
    """The largest money ceiling among the paths of ``task_type``.

    The ceiling of a review whose path could not be chosen; 0 for a type the
    new path does not serve (today's Mentor charges nothing, S2).
    """
    declared = config.task_types.get(task_type)
    if declared is None or declared.served_by is not ServedBy.NEW_PATH:
        return Decimal(0)
    return max(
        (
            Decimal(str(path_ceiling_estimate(config, PathKey(task_type, state))))
            for state in declared.paths
        ),
        default=Decimal(0),
    )


def _at_ceiling(
    ceiling_usd: Decimal, *, task: AuthoredDocument, reason: str, error: str | None
) -> ReviewPrice:
    """The price N of a review whose estimate failed, with its one WARNING."""
    logger.warning(
        CEILING_WARNING,
        reason=reason,
        error=error,
        task_id=str(task.id),
        task_type=task.task_type,
        ceiling_usd=str(ceiling_usd),
    )
    return ReviewPrice(charging.ceiling_units(ceiling_usd), "ceiling")


async def review_price(
    session: AsyncSession,
    *,
    task: AuthoredDocument,
    student_id: uuid.UUID,
    tenant_id: uuid.UUID,
    reading: Any | None = None,
    read_error: str | None = None,
    config: PathConfig | None = None,
    response_language: str | None = None,
) -> ReviewPrice:
    """P of a review of ``task`` (not a test), whoever pays it (module docstring).

    ``reading`` — the doors' reading of the file about to be sent, ``None``
    for the typical size; ``read_error`` — the class of the error that broke
    the reading of the file sent: the price is the ceiling. 0 for a type
    today's Mentor serves (S2) and for the same work again — in the language
    its review will be written in; asked in another, the work is a new review
    and has the usual price. ``response_language`` — the language the form
    names, ``None`` if none (the student's stored one, the task's and the
    course root's then decide, as for the dedup).
    """
    from course_supporter.homework.path_selection import choose_path

    config = config if config is not None else get_path_config()
    task_type = AssignmentType(task.task_type or "")
    try:
        async with session.begin_nested():
            choice = await choose_path(
                session,
                task_type=task_type,
                student_id=student_id,
                authored_document_id=task.id,
                config=config,
            )
    except Exception as exc:
        return _at_ceiling(
            _widest_ceiling(config, task_type),
            task=task,
            reason="path_choice_failed",
            error=type(exc).__name__,
        )
    if choice is None:
        return ReviewPrice(0, "file" if reading is not None else "typical")
    ceiling = Decimal(str(path_ceiling_estimate(config, choice.key)))
    if read_error is not None:
        return _at_ceiling(
            ceiling, task=task, reason="file_read_failed", error=read_error
        )
    if charging.units_per_usd() is None:
        return _at_ceiling(ceiling, task=task, reason="rate_not_set", error=None)
    basis: Basis
    if reading is not None:
        if await _same_work(
            session,
            task=task,
            student_id=student_id,
            tenant_id=tenant_id,
            reading=reading,
            config=config,
            response_language=response_language,
        ):
            return ReviewPrice(0, "file")
        chars, basis = len(reading.text), "file"
    else:
        try:
            async with session.begin_nested():
                chars = await _typical_chars(session, task.id)
        except Exception as exc:
            return _at_ceiling(
                ceiling,
                task=task,
                reason="typical_size_failed",
                error=type(exc).__name__,
            )
        basis = "typical"
    priced = model_stages(config)
    e_usd = review_e_usd(
        stage_names=[name for name in choice.stages if name in priced],
        path_stages=priced,
        submission_chars=chars,
        fallback_usd=ceiling,
    )
    if e_usd is None:  # review_e_usd has warned
        return ReviewPrice(charging.ceiling_units(ceiling), "ceiling")
    return ReviewPrice(charging.price_of(e_usd), basis)


NOTHING_PRICED_THRESHOLD: Final = 1
"""The threshold of a review that cannot be priced at all: ``available > 0``."""


async def ceiling_threshold(
    session: AsyncSession,
    *,
    task: AuthoredDocument,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID,
    typical: ReviewPrice | None = None,
    config: PathConfig | None = None,
) -> int:
    """The threshold of a review priced at its ceiling (operator 2026-11-15).

    The price shown is N, the most the review can cost; the start needs only
    the task's typical price — basis ``typical``, the median size of its
    history or ``typical_submission.chars``, margin included. When even that
    cannot be priced, ``available > 0``
    (:data:`NOTHING_PRICED_THRESHOLD`). The reserve is then the smaller of N
    and what is available, the fact is charged, and what it costs over the
    reserve is the platform's — as for any payer without credit.

    The ONE answer for the gate of a submission (:func:`review_quote`), the
    start of its run (``funds.LedgerFundsPort``) and the continuation of a
    held review (:func:`.held.price_of_held`). ``typical`` — the typical price
    already tried (a review sent without a file); ``None`` — it is priced here.
    """
    if typical is None:
        typical = await review_price(
            session,
            task=task,
            tenant_id=tenant_id,
            student_id=student_id,
            config=config,
        )
    if typical.basis == "typical":
        return typical.units
    return NOTHING_PRICED_THRESHOLD


async def review_quote(
    session: AsyncSession,
    *,
    task: AuthoredDocument,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID,
    reading: Any | None = None,
    read_error: str | None = None,
    config: PathConfig | None = None,
    response_language: str | None = None,
) -> Quote:
    """What the student's review of ``task`` costs, and whether it may start.

    ``response_language`` — the language the form of the submission names
    (``None``: none named); the price of the same work again depends on it
    (:func:`review_price`).

    ``reading`` — the doors' reading of the file about to be sent
    (:class:`~course_supporter.homework.doors.DoorReading`), or ``None`` for
    the typical price (:func:`_typical_chars`); ``read_error`` — reading the
    file sent failed (:func:`review_price`). The threshold is the run's own
    (:func:`.charging.threshold_applies`): ``available >= P``; at the ceiling
    the price shown is N and the threshold is :func:`ceiling_threshold`.
    """
    config = config if config is not None else get_path_config()
    task_type = AssignmentType(task.task_type or "")
    payer = await charging.find_payer_of(
        session,
        action_kind=ActionKind.REVIEW,
        tenant_id=tenant_id,
        course_node_id=task.course_root_id,
        student_id=student_id,
    )
    enabled = await charging.charging_applies(
        session, payer.account_id
    ) and not in_test_mode(task_type, config)
    sent = reading is not None or read_error is not None
    basis: Basis = "file" if sent else "typical"
    if payer.for_someone_else:
        return Quote(None, None, True, None, enabled, basis, "author")
    available = await charging.available_units(session, payer.account_id)
    if task_type is AssignmentType.TEST:
        price = 0
        enough = await charging.test_allowed(session, payer)
        missing = 0 if enough else 1
    else:
        found = await review_price(
            session,
            task=task,
            student_id=student_id,
            tenant_id=tenant_id,
            reading=reading,
            read_error=read_error,
            config=config,
            response_language=response_language,
        )
        price, basis = found.units, found.basis
        threshold = price
        if basis == "ceiling":
            threshold = await ceiling_threshold(
                session,
                task=task,
                tenant_id=tenant_id,
                student_id=student_id,
                typical=None if sent else found,
                config=config,
            )
        missing = max(0, threshold - available)
        enough = missing == 0
    if not enabled:
        return Quote(price, available, True, 0, enabled, basis, "self")
    return Quote(price, available, enough, missing, enabled, basis, "self")


# ── An author's own action ────────────────────────────────────────────


async def author_quote(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    tenant_id: uuid.UUID,
    course_node_id: uuid.UUID | None,
    e_usd: Decimal | None,
) -> Quote:
    """What an author's processing or task preparation costs, and may it start.

    The threshold of the author's own action: an author with credit is never
    stopped. ``e_usd=None`` (no estimate): no price — ``price_units`` and
    ``missing_units`` are ``None`` and ``enough`` is true; the action is not
    refused in advance, its start checks the threshold on its real features
    (BIL-E3; the route says why there is no price).
    """
    payer = await charging.find_payer_of(
        session,
        action_kind=action_kind,
        tenant_id=tenant_id,
        course_node_id=course_node_id,
        student_id=None,
    )
    enabled = await charging.charging_applies(session, payer.account_id)
    if e_usd is None:
        available = await charging.available_units(session, payer.account_id)
        return Quote(None, available, True, None, enabled)
    price = charging.price_of(e_usd)
    found = await charging.quote(
        session,
        action_kind=action_kind,
        payer=payer,
        price_units=price,
        own_action=True,
    )
    if not enabled:
        return Quote(price, found.available_units, True, 0, enabled)
    return Quote(
        price, found.available_units, found.enough, found.missing_units, enabled
    )
