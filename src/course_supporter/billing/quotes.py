"""Prices before an action (BIL-E, part B): what a screen shows, what a route checks.

Purpose:
    The estimate routes and the gates of the submission routes ask the same
    question — what will this action cost its payer, and may it start — and
    get the answer from here. The price is :func:`.charging.price_of`, the one
    function of the threshold; the payer is :func:`.payer.resolve_payer`.

    * :func:`review_quote` — a student's review of a task: from the file the
      student is about to send (basis ``file``: the doors read it as the path
      will, nothing is stored) or without one (basis ``typical``: the median
      size of this task's earlier submissions, the prompts alone when there
      are none). 0 for a test while the balance is above 0 and for the same
      work again (the fingerprint of the file matches an earlier attempt that
      was reviewed). An author paying under a rule: no numbers of the author,
      ``enough`` always (operator 2026-11-15).
    * :func:`author_quote` — an author's processing or task preparation from
      its expected cost.
    * :class:`UnitsRefusedError` — the refusal a route raises; the API
      answers 402 ``{"detail", "reason": "insufficient_units", "price_units",
      "available_units", "missing_units"}``.

    ``charging_enabled`` is the switch AND the path not in test mode; while it
    is false nothing is refused and ``enough`` is true.
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

from course_supporter.billing import charging, ledger
from course_supporter.billing.estimates import review_e_usd
from course_supporter.billing.kinds import AccountKind, ActionKind
from course_supporter.homework.path_config import (
    PathConfig,
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

Basis = Literal["typical", "file"]
PaidBy = Literal["self", "author"]


@dataclass(frozen=True, slots=True)
class Quote:
    """The answer of an estimate route (and of a gate)."""

    price_units: int
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

    def body(self) -> dict[str, Any]:
        """The 402 body: the shape of every billing refusal, plus the numbers."""
        return {
            "detail": self.detail,
            "reason": INSUFFICIENT_UNITS,
            "price_units": self.quote.price_units,
            "available_units": self.quote.available_units,
            "missing_units": self.quote.missing_units,
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


async def _typical_chars(session: AsyncSession, task_id: uuid.UUID) -> int:
    """The median submission size of the task's latest review estimates; 0 if none."""
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
    return int(statistics.median(known)) if known else 0


async def _same_work(
    session: AsyncSession,
    *,
    task: AuthoredDocument,
    student_id: uuid.UUID,
    tenant_id: uuid.UUID,
    reading: Any,
    config: PathConfig,
) -> bool:
    """Would this file be answered with an earlier review (task 13, K3)?

    The attempt dedup's own rule — fingerprint, task version, criteria list in
    force, evaluator version — read for a work not yet stored. Any error reads
    as "no": the price is then the usual one.
    """
    from course_supporter.homework.attempt_dedup import _JUDGED_AGAINST_A_LIST
    from course_supporter.homework.attempt_ledger import task_version_of
    from course_supporter.homework.attempts import (
        AttemptWork,
        attempt_snapshotter_for,
    )
    from course_supporter.homework.criteria_list_service import load_in_force
    from course_supporter.homework.evaluator_version import evaluator_version
    from course_supporter.storage.homework_repository import HomeworkRepository

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
        criteria_list: tuple[str, uuid.UUID] | None = None
        if task_type in _JUDGED_AGAINST_A_LIST:
            in_force = await load_in_force(session, task.id)
            if in_force is None:
                return False
            criteria_list = (in_force.layer, in_force.source_id)
        source = await HomeworkRepository(session).latest_unchanged_attempt(
            tenant_id=tenant_id,
            student_id=student_id,
            authored_document_id=task.id,
            excluding=uuid.uuid4(),
            fingerprint=fingerprint,
            task_version=version,
            criteria_list=criteria_list,
            evaluator_version=evaluator,
        )
    except Exception:
        logger.warning("billing_quote_same_work_failed", exc_info=True)
        return False
    return source is not None and source.attempt_no is not None


async def _review_price(
    session: AsyncSession,
    *,
    task: AuthoredDocument,
    student_id: uuid.UUID,
    tenant_id: uuid.UUID,
    reading: Any | None,
    config: PathConfig,
) -> int:
    """P of a review that is not a test (0 when it would cost nothing)."""
    from course_supporter.homework.path_selection import choose_path

    task_type = AssignmentType(task.task_type or "")
    choice = await choose_path(
        session,
        task_type=task_type,
        student_id=student_id,
        authored_document_id=task.id,
        config=config,
    )
    if choice is None:
        return 0  # today's Mentor: not charged (operator 2026-11-15, S2)
    if reading is not None and await _same_work(
        session,
        task=task,
        student_id=student_id,
        tenant_id=tenant_id,
        reading=reading,
        config=config,
    ):
        return 0
    priced = model_stages(config)
    chars = (
        len(reading.text)
        if reading is not None
        else await _typical_chars(session, task.id)
    )
    e_usd = review_e_usd(
        stage_names=[name for name in choice.stages if name in priced],
        path_stages=priced,
        submission_chars=chars,
        fallback_usd=Decimal(str(path_ceiling_estimate(config, choice.key))),
    )
    return charging.price_of(e_usd)


async def review_quote(
    session: AsyncSession,
    *,
    task: AuthoredDocument,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID,
    reading: Any | None = None,
    config: PathConfig | None = None,
) -> Quote:
    """What the student's review of ``task`` costs, and whether it may start.

    ``reading`` — the doors' reading of the file about to be sent
    (:class:`~course_supporter.homework.doors.DoorReading`), or ``None`` for
    the typical price.
    """
    config = config if config is not None else get_path_config()
    task_type = AssignmentType(task.task_type or "")
    enabled = charging.charging_enabled() and not in_test_mode(task_type, config)
    payer = await charging.payer_of(
        session,
        action_kind=ActionKind.REVIEW,
        tenant_id=tenant_id,
        course_node_id=task.course_root_id,
        student_id=student_id,
    )
    by_author = payer.kind is AccountKind.AUTHOR
    basis: Basis = "file" if reading is not None else "typical"
    paid_by: PaidBy = "author" if by_author else "self"
    if task_type is AssignmentType.TEST:
        price, enough_now = 0, await charging.test_allowed(session, payer)
    else:
        price = await _review_price(
            session,
            task=task,
            student_id=student_id,
            tenant_id=tenant_id,
            reading=reading,
            config=config,
        )
        enough_now = True
    if by_author:
        return Quote(price, None, True, None, enabled, basis, paid_by)
    available = (await ledger.balance(session, payer.account_id)).available
    if task_type is AssignmentType.TEST:
        missing = 0 if enough_now else 1
    else:
        missing = max(0, price - available)
        enough_now = missing == 0
    if not enabled:
        return Quote(price, available, True, 0, enabled, basis, paid_by)
    return Quote(price, available, enough_now, missing, enabled, basis, paid_by)


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
    stopped. ``e_usd=None`` (no estimate) prices nothing (S2).
    """
    enabled = charging.charging_enabled()
    payer = await charging.payer_of(
        session,
        action_kind=action_kind,
        tenant_id=tenant_id,
        course_node_id=course_node_id,
        student_id=None,
    )
    price = charging.price_of(e_usd) if e_usd is not None else 0
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
