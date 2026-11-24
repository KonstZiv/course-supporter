"""Charging an action (BIL-E): price before, reserve at the start, fact at the end.

Purpose:
    The one place where an action meets the journal of units. The callers —
    the funds port of the review path and of a task's key explanations
    (:mod:`.funds`), the processing gate, the criteria decomposition, the
    estimate routes — speak in dollars and keys; this module turns them into
    units with the estimate's own rate (``config/cost_estimate.yaml``) and calls
    the journal (:mod:`.ledger`). It adds no rule of the journal: a reserve,
    a settle, a release are the ledger's.

The rules (operator 2026-10-30, 2026-11-04, 2026-11-11, 2026-11-15):

    * Before: the price P = ``ceil(E x (1 + margin) x K)`` (:func:`price_of`,
      THE function of the threshold — the estimate routes and the start of an
      action call the same one). An action may start only while its payer has
      ``available >= P`` — where the threshold applies
      (:func:`threshold_applies`): a student paying for their own review; an
      author without credit paying for their own action or for a student's
      review under a payment rule (operator 2026-11-15, 5a, item 2: the platform
      does not work on credit — the review waits for the author's units). A
      task preparation a student's submission started never waits for the
      author's units; an author with credit is never stopped.
    * Before, when the estimate failed: the price shown is the ceiling N
      (:func:`ceiling_units`) — the most the reserve can hold, so "no more
      than N" is true; the threshold is the task's typical price, else
      ``available > 0`` (``quotes.ceiling_threshold``); the reserve is
      ``min(N, available)``.
    * Start: a reserve of ``min(2E, the ceiling, available)`` units
      (:func:`reserve_for`; the ledger caps it by what is available, so it
      may be 0).
    * End: the fact F — the cost of the action's calls in the register that
      the payer pays (:data:`PAID_BY_PAYER`, operator 2026-11-17), in units
      (:func:`fact_of`) — is settled (:func:`finish`). A payer without
      credit (every student, an author without credit) is charged at most the
      reserve; the excess is ``platform_covered``. An author with credit
      keeps the ledger's difference. The author's benefit applies first,
      inside the ledger.
    * Failure: the reserve is released whole, nothing is charged
      (:func:`fail`).

    A test (a task of kind ``test``: checked against the key, no model) is
    free while its payer's balance, reserved units included, is above 0
    (:func:`test_allowed`) — an author with credit is never stopped; it
    reserves and charges nothing.

The switches:
    ``BILLING_CHARGING_ENABLED`` charges every payer; while it is off,
    ``BILLING_CHARGING_MARKED`` charges only a payer whose account is marked
    ``test_purchase_allowed`` (operator 2026-11-17). A place asks
    :func:`charging_possible` before its payer is known and
    :func:`charging_applies` once it is; a start that must not create an
    account asks :func:`charged_payer_of`.

Keys:
    The estimate's: a review is its submission (all its jobs), a processing is
    its job, a preparation is (task, version, kind) —
    :mod:`course_supporter.cost_estimate.shadow`. A repeat with the same key
    reserves nothing more and charges nothing twice (the ledger's idempotency).

Transactions:
    Every function flushes and never commits; the caller owns the session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING, Final

import structlog
from sqlalchemy import ColumnElement, and_, func, not_, or_, select

from course_supporter.billing import ledger
from course_supporter.billing.errors import (
    ActionAlreadySettledError,
    BillingConflictError,
)
from course_supporter.billing.journal import lock_account
from course_supporter.billing.kinds import (
    AccountKind,
    ActionKind,
    PayerSource,
    ReservationState,
    RuleScope,
)
from course_supporter.billing.links import PREPARATION_STAGES
from course_supporter.billing.payer import (
    PayerLookup,
    PayerResolution,
    default_kind,
    find_payer,
    resolve_payer,
)
from course_supporter.call_outcome import CallOutcome, RequestKind
from course_supporter.cost_estimate.core import fact_units, price_units, reserve_units
from course_supporter.cost_estimate.settings import get_estimate_settings
from course_supporter.homework.path_config import REPLY_SAFETY_ACTION
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuthorSettings,
    BillingPaymentRule,
    BillingReservation,
    ExternalServiceCall,
    Job,
)

if TYPE_CHECKING:
    from collections.abc import Collection

    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.config import Settings

logger = structlog.get_logger(__name__)


# ── The switch ─────────────────────────────────────────────────────────


def _settings(settings: Settings | None) -> Settings:
    if settings is None:
        from course_supporter.config import get_settings

        settings = get_settings()
    return settings


def charging_enabled(settings: Settings | None = None) -> bool:
    """Charging for every account (``BILLING_CHARGING_ENABLED``)? Off by default.

    Read only by the two questions below; a caller asks one of them —
    :func:`charging_possible` before its payer is known, :func:`charging_applies`
    once it is.
    """
    return _settings(settings).billing_charging_enabled


def charging_possible(settings: Settings | None = None) -> bool:
    """May charging apply to any payer — every one, or the marked ones
    (``BILLING_CHARGING_MARKED``)?

    For a place that does not know its payer yet: the choice of the funds
    port, the continuation of held reviews, the end of a processing (it
    settles only what its start reserved), the doors of a submission (the
    quote they read answers for the payer). False: nothing to do.
    """
    settings = _settings(settings)
    return charging_enabled(settings) or settings.billing_charging_marked


async def charging_applies(
    session: AsyncSession,
    account_id: uuid.UUID | None,
    settings: Settings | None = None,
) -> bool:
    """Does charging apply to this payer's account (``None``: not created yet)?

    Yes for every account while ``BILLING_CHARGING_ENABLED`` is on. While only
    ``BILLING_CHARGING_MARKED`` is on, yes for an existing account marked
    ``test_purchase_allowed`` — in a course where the author pays, the
    author's mark decides, not the student's. Otherwise no: the action runs as
    with charging off — nothing reserved, refused or held, no account created.
    A read; the account is not locked.

    The mark ``test_purchase_allowed`` means two things (operator 2026-11-17):
    (a) the account may buy while the payment provider is in its test mode
    (``payments.checkout.purchase_closed``); (b) the account is charged while
    ``BILLING_CHARGING_ENABLED`` is off and ``BILLING_CHARGING_MARKED`` is on.
    It is cleared for both at once — before the provider's live mode, when
    charging for every account is switched on anyway.
    """
    settings = _settings(settings)
    if charging_enabled(settings):
        return True
    if not settings.billing_charging_marked or account_id is None:
        return False
    account = await session.get(BillingAccount, account_id)
    return account is not None and account.test_purchase_allowed


# ── Units ──────────────────────────────────────────────────────────────


def units_per_usd() -> Decimal | None:
    """K, the estimate's rate; ``None`` while it is not set."""
    return get_estimate_settings().units_per_usd


def price_of(e_usd: Decimal) -> int:
    """P in units for an expected cost E — the threshold of every action.

    0 while no rate is set (nothing can be priced, so nothing is refused).
    """
    settings = get_estimate_settings()
    units = price_units(
        e_usd, margin=settings.margin, units_per_usd=settings.units_per_usd
    )
    return units or 0


def ceiling_units(ceiling_usd: Decimal) -> int:
    """N: a money ceiling in units, ``ceil(ceiling x K)`` — no margin.

    The cap of every reserve (:func:`reserve_request`) and the price of a
    review whose estimate failed ("no more than N"). 0 while no rate is set.
    """
    k = units_per_usd()
    if k is None:
        return 0
    return int((ceiling_usd * k).to_integral_value(rounding=ROUND_CEILING))


def reserve_request(e_usd: Decimal, ceiling_usd: Decimal | None) -> int:
    """What a start asks to hold: ``min(2E, the ceiling)`` in units.

    The ledger holds that or the available units, whichever is less.
    """
    wanted = reserve_units(e_usd, units_per_usd=units_per_usd()) or 0
    if ceiling_usd is None:
        return wanted
    return min(wanted, ceiling_units(ceiling_usd))


def fact_of(usd: Decimal) -> int:
    """F in units for what an action's calls cost (same rate as E)."""
    return fact_units(usd, units_per_usd=units_per_usd()) or 0


# ── Who pays ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Payer:
    """The payer of one action and what its account allows."""

    resolution: PayerResolution
    kind: AccountKind
    credit_allowed: bool

    @property
    def account_id(self) -> uuid.UUID:
        """The paying account."""
        return self.resolution.payer_account_id

    @property
    def for_someone_else(self) -> bool:
        """An author paying for a student's review under a payment rule."""
        return (
            self.resolution.payer_account_id != self.resolution.beneficiary_account_id
        )


@dataclass(frozen=True, slots=True)
class PayerView:
    """The payer of one action as a read sees it (:func:`find_payer_of`).

    Nothing is created for it (BIL-E3): ``account_id`` is ``None`` while the
    default payer has no account yet, and such a payer reads as an empty
    account — nothing available, no credit.
    """

    lookup: PayerLookup
    kind: AccountKind
    credit_allowed: bool

    @property
    def account_id(self) -> uuid.UUID | None:
        """The paying account; None before it exists."""
        return self.lookup.payer_account_id

    @property
    def for_someone_else(self) -> bool:
        """An author paying for a student's review under a payment rule."""
        return self.lookup.payer_account_id != self.lookup.beneficiary_account_id


async def credit_allowed(session: AsyncSession, account: BillingAccount) -> bool:
    """May the account run into a difference (an author with credit)?"""
    if account.kind != AccountKind.AUTHOR.value:
        return False
    settings = await session.get(BillingAuthorSettings, account.id)
    return settings is not None and settings.credit_allowed


async def payer_of(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    tenant_id: uuid.UUID,
    course_node_id: uuid.UUID | None,
    student_id: uuid.UUID | None,
) -> Payer:
    """Who pays (:func:`.payer.resolve_payer`), with the account's kind and credit.

    For an action that changes state: the account is created on first need. A
    read asks :func:`find_payer_of`.
    """
    resolution = await resolve_payer(
        session,
        action_kind=action_kind,
        tenant_id=tenant_id,
        course_node_id=course_node_id,
        student_id=student_id,
    )
    account = await session.get(BillingAccount, resolution.payer_account_id)
    assert account is not None  # resolve_payer returns existing accounts
    return Payer(
        resolution=resolution,
        kind=AccountKind(account.kind),
        credit_allowed=await credit_allowed(session, account),
    )


async def find_payer_of(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    tenant_id: uuid.UUID,
    course_node_id: uuid.UUID | None,
    student_id: uuid.UUID | None,
) -> PayerView:
    """:func:`payer_of` for a read (:func:`.payer.find_payer`): creates nothing."""
    lookup = await find_payer(
        session,
        action_kind=action_kind,
        tenant_id=tenant_id,
        course_node_id=course_node_id,
        student_id=student_id,
    )
    if lookup.payer_account_id is None:
        return PayerView(lookup, default_kind(action_kind), credit_allowed=False)
    account = await session.get(BillingAccount, lookup.payer_account_id)
    assert account is not None  # find_payer returns existing accounts or None
    return PayerView(
        lookup,
        AccountKind(account.kind),
        credit_allowed=await credit_allowed(session, account),
    )


async def charged_payer_of(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    tenant_id: uuid.UUID,
    course_node_id: uuid.UUID | None,
    student_id: uuid.UUID | None,
) -> Payer | None:
    """:func:`payer_of` for a start, when charging applies to the payer.

    The payer is read first (:func:`find_payer_of`); ``None`` — charging does
    not apply to it (:func:`charging_applies`): the start goes on as with
    charging off, and nothing was created or written.
    """
    found = await find_payer_of(
        session,
        action_kind=action_kind,
        tenant_id=tenant_id,
        course_node_id=course_node_id,
        student_id=student_id,
    )
    if not await charging_applies(session, found.account_id):
        return None
    return await payer_of(
        session,
        action_kind=action_kind,
        tenant_id=tenant_id,
        course_node_id=course_node_id,
        student_id=student_id,
    )


async def available_units(
    session: AsyncSession, account_id: uuid.UUID | None, *, now: datetime | None = None
) -> int:
    """What the account can spend now; 0 for an account not created yet."""
    if account_id is None:
        return 0
    return (await ledger.balance(session, account_id, now=now)).available


def threshold_applies(
    action_kind: ActionKind, payer: Payer | PayerView, *, own_action: bool
) -> bool:
    """Does ``available >= P`` gate this action (module docstring)?

    A review: unless an author with credit pays for it — a student for their
    own, an author without credit under a payment rule (5a, item 2). Any other
    action: only when the author asked for it (``own_action``) and has no
    credit.
    """
    if action_kind is ActionKind.REVIEW:
        return not payer.credit_allowed
    return own_action and not payer.credit_allowed


# ── The threshold ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Quote:
    """The price of an action and whether its payer can start it."""

    price_units: int
    available_units: int
    enough: bool
    missing_units: int
    threshold: bool
    payer: Payer | PayerView


async def quote(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    payer: Payer | PayerView,
    price_units: int,
    own_action: bool = False,
    lock: bool = False,
) -> Quote:
    """P against the payer's available units; ``enough`` where no threshold applies.

    ``lock`` takes the account's lock first, so a start that follows in the
    same transaction reserves from what was checked; a start resolves its payer
    with :func:`payer_of`, so the account exists. A read (:class:`PayerView`)
    of a payer without an account sees 0 available.
    """
    if lock:
        assert payer.account_id is not None  # a start's payer has its account
        await lock_account(session, payer.account_id)
    available = await available_units(session, payer.account_id)
    threshold = threshold_applies(action_kind, payer, own_action=own_action)
    enough = not threshold or available >= price_units
    return Quote(
        price_units=price_units,
        available_units=available,
        enough=enough,
        missing_units=max(0, price_units - available) if threshold else 0,
        threshold=threshold,
        payer=payer,
    )


async def test_allowed(session: AsyncSession, payer: Payer | PayerView) -> bool:
    """A test is free while its payer's balance (reserved included) is above 0.

    The same for an author paying under a rule (operator 2026-11-15, 5a, item 2);
    an author with credit is never stopped. A payer without an account yet has
    a balance of 0.
    """
    if payer.credit_allowed:
        return True
    if payer.account_id is None:
        return False
    return (await ledger.balance(session, payer.account_id)).balance > 0


# ── Start, end, failure ────────────────────────────────────────────────


async def reserve_for(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    action_key: str,
    payer: Payer,
    e_usd: Decimal,
    ceiling_usd: Decimal | None,
    now: datetime | None = None,
) -> BillingReservation:
    """Hold ``min(2E, ceiling, available)`` units for the action's start."""
    return await ledger.reserve(
        session,
        action_kind=action_kind,
        action_key=action_key,
        account_id=payer.account_id,
        units=reserve_request(e_usd, ceiling_usd),
        now=now,
    )


async def reservation_of(
    session: AsyncSession, action_kind: ActionKind, action_key: str
) -> BillingReservation | None:
    """The action's reservation, if its start reserved."""
    return (
        await session.execute(
            select(BillingReservation).where(
                BillingReservation.action_kind == action_kind.value,
                BillingReservation.action_key == action_key,
            )
        )
    ).scalar_one_or_none()


async def _payment_of(
    session: AsyncSession,
    reservation: BillingReservation,
    resolution: PayerResolution,
    *,
    tenant_id: uuid.UUID,
    action_kind: ActionKind,
) -> tuple[PayerSource, uuid.UUID | None]:
    """Why the reserving account pays: today's answer, or the rule it paid by.

    A rule may end while the action runs; the account that reserved still
    pays, under the latest rule that named it.
    """
    if resolution.payer_account_id == reservation.account_id:
        return resolution.source, resolution.rule_id
    if reservation.account_id == resolution.beneficiary_account_id:
        return PayerSource.DEFAULT, None
    rule = (
        await session.execute(
            select(BillingPaymentRule)
            .where(
                BillingPaymentRule.payer_account_id == reservation.account_id,
                BillingPaymentRule.tenant_id == tenant_id,
                BillingPaymentRule.action_kind == action_kind.value,
            )
            .order_by(BillingPaymentRule.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if rule is None:
        return PayerSource.DEFAULT, None
    source = (
        PayerSource.STUDENT_RULE
        if rule.scope == RuleScope.STUDENT.value
        else PayerSource.COURSE_RULE
    )
    return source, rule.id


async def finish(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    action_key: str,
    fact_usd: Decimal,
    tenant_id: uuid.UUID,
    course_node_id: uuid.UUID | None,
    student_id: uuid.UUID | None,
    now: datetime | None = None,
) -> ledger.Settlement | None:
    """Settle the action's fact; ``None`` when there is nothing to settle.

    Nothing is settled for an action whose start reserved nothing (charging
    was off then, or it is free) and for one whose reserve was released
    before its end — a hold outlived ``billing_stale_reserve_hours`` (operator
    2026-11-15, S3: nothing is charged, a WARNING names the action). A
    repeated end returns the first outcome; repeating an already settled
    action with a different fact (a platform-initiated repeat) charges
    nothing and is logged as a WARNING.
    """
    reservation = await reservation_of(session, action_kind, action_key)
    log = logger.bind(action_kind=action_kind.value, action_key=action_key)
    if reservation is None:
        log.info("billing_finish_without_reserve")
        return None
    if reservation.state == ReservationState.RELEASED.value:
        log.warning("billing_reserve_released_before_end", fact_usd=str(fact_usd))
        return None
    resolution = await resolve_payer(
        session,
        action_kind=action_kind,
        tenant_id=tenant_id,
        course_node_id=course_node_id,
        student_id=student_id,
    )
    source, rule_id = await _payment_of(
        session, reservation, resolution, tenant_id=tenant_id, action_kind=action_kind
    )
    account = await session.get(BillingAccount, reservation.account_id)
    assert account is not None  # a reservation's account is never deleted
    try:
        settlement = await ledger.settle(
            session,
            action_kind=action_kind,
            action_key=action_key,
            fact_units=fact_of(fact_usd),
            beneficiary_account_id=resolution.beneficiary_account_id,
            payer_source=source,
            rule_id=rule_id,
            excess_to_platform=not await credit_allowed(session, account),
            now=now or datetime.now(UTC),
        )
    except ActionAlreadySettledError as exc:
        log.warning(
            "billing_finish_repeat_not_charged",
            error=str(exc),
            fact_usd=str(fact_usd),
        )
        return None
    except BillingConflictError as exc:
        log.error("billing_finish_conflict", error=str(exc))
        return None
    log.info(
        "billing_action_settled",
        fact_units=settlement.fact_units,
        charged_units=settlement.charged_units,
        platform_units=settlement.platform_units,
        benefit_units=settlement.benefit_units,
        difference_units=settlement.difference_units,
    )
    return settlement


async def fail(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    action_key: str,
    now: datetime | None = None,
) -> ledger.Release | None:
    """The action failed: release its whole reserve, charge nothing.

    ``None`` when it reserved nothing; a settled action is left as it is.
    """
    reservation = await reservation_of(session, action_kind, action_key)
    if reservation is None:
        return None
    if reservation.state == ReservationState.SETTLED.value:
        logger.warning(
            "billing_fail_after_settle",
            action_kind=action_kind.value,
            action_key=action_key,
        )
        return None
    return await ledger.release(
        session, action_kind=action_kind, action_key=action_key, now=now
    )


# ── Facts from the register ────────────────────────────────────────────

_PRICED_CALL = ExternalServiceCall.unit_type.is_not(None)
"""A row of a call that was made and priced (the register's
``ck_esc_priced_call``); the funds-port and metrics rows have none."""

PAYER_REQUEST_KINDS: Final = (RequestKind.FIRST.value, RequestKind.ITEM_REPEAT.value)
"""The kinds of request whose cost is the payer's (:data:`PAID_BY_PAYER`)."""

REPLY_SCREEN_ACTIONS: Final = frozenset({REPLY_SAFETY_ACTION})
"""The actions the reply screen's calls are recorded under (task 12a, K3,
``homework/reply_screen.py``): today its safety check alone — the topic port
is a placeholder that calls no model. A topic adapter with an action of its
own joins this set."""

REPLY_SCREEN_CALL: Final[ColumnElement[bool]] = ExternalServiceCall.action.in_(
    REPLY_SCREEN_ACTIONS
)
"""A call of the reply screen — never in the payer's fact, whatever its kind.

The billing session, 2026-11-17 ~21:30, on the Mentor's remark (an addition to
BIL-FACT): the screen of a student's replies to the remarks of the last review
runs in the submission's job, before the funds port and outside the shadow
estimate, and its cost is not the fact of the review. An exclusion of its own,
apart from ``request_kind``; it is the platform's (``billing.costs``,
``platform_usd``). Whether replies are ever paid for separately is a decision
of its own, not made here.
"""

_PAYER_REQUEST: Final[ColumnElement[bool]] = and_(
    or_(
        ExternalServiceCall.request_kind.is_(None),
        ExternalServiceCall.request_kind.in_(PAYER_REQUEST_KINDS),
    ),
    ExternalServiceCall.outcome.is_distinct_from(CallOutcome.TRANSPORT_ERROR.value),
)
"""The request half of :data:`PAID_BY_PAYER`: its kind and its own failure."""

PAID_BY_PAYER: Final[ColumnElement[bool]] = and_(
    _PAYER_REQUEST, not_(REPLY_SCREEN_CALL)
)
"""A register row whose cost the payer pays — THE definition of the fact.

The operator, 2026-11-17 (15:51, confirmed 16:01): «Платник платить за перший
запит стадії і за повтор сумнівного пункту. Повтори через поламану структуру
відповіді й через збій мережі несе платформа. Оцінка ціни калібрується за тим
самим визначенням факту.» It stands on ``request_kind``
(:class:`~course_supporter.call_outcome.RequestKind`):

* the payer's — ``first``, ``item_repeat`` and ``NULL`` (speech-to-text
  attempts, rows written before the column; rows of no call carry no cost);
* the platform's — ``structural_retry``, ``infra_retry``, and any attempt that
  itself ended in an infrastructure failure (``outcome = 'transport_error'``:
  a timeout, 429, 5xx, a dropped connection), whatever its kind — a ``first``
  and a ``NULL`` row too. One rule for every failed attempt: the billing
  session's reading (vision-rules#24), confirmed by the coordinator, and its
  answer of 2026-11-17 ~21:55 on the rows without a kind (a failed
  speech-to-text attempt is the platform's).

Apart from the kind: a call of the reply screen is never the payer's
(:data:`REPLY_SCREEN_CALL`).

Says nothing about whether a row is priced: each reader keeps its own test
for that. The rest of a row's cost — ``NOT PAID_BY_PAYER`` — is what the
platform took on (``billing.costs``, ``platform_usd``). The facts of the
journal (:func:`review_facts_usd`, :func:`job_fact_usd`) and the calibration
report (``scripts/cost_estimate_report.py``) read this one condition.
"""  # noqa: RUF001 - the operator's words, quoted as said


async def review_facts_usd(
    session: AsyncSession, submission_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """What each review's calls cost the payer (:data:`PAID_BY_PAYER`): every
    job of the submission, without the task's preparation run inside them (as
    the cost report, ``billing.costs``). One query, grouped by submission; a
    submission without such calls is 0."""
    if not submission_ids:
        return {}
    rows = await session.execute(
        select(Job.subject_id, func.sum(ExternalServiceCall.cost_usd))
        .join(Job, Job.id == ExternalServiceCall.job_id)
        .where(
            Job.subject_type == JOB_SUBJECT_TYPE[JobType.HOMEWORK_PROCESSING],
            Job.subject_id.in_(submission_ids),
            _PRICED_CALL,
            PAID_BY_PAYER,
            ExternalServiceCall.action.not_in(PREPARATION_STAGES),
        )
        .group_by(Job.subject_id)
    )
    facts = dict.fromkeys(submission_ids, Decimal(0))
    for submission_id, total in rows.tuples():
        if submission_id is not None:  # matched the IN; the column is nullable
            facts[submission_id] = Decimal(total or 0)
    return facts


async def review_fact_usd(session: AsyncSession, submission_id: uuid.UUID) -> Decimal:
    """:func:`review_facts_usd` of one review — the fact its end settles."""
    return (await review_facts_usd(session, [submission_id]))[submission_id]


async def job_fact_usd(
    session: AsyncSession, job_id: uuid.UUID, *, action: str | None = None
) -> Decimal:
    """What one job's calls cost the payer (:data:`PAID_BY_PAYER`) — all of
    them, or those of one ``action``."""
    stmt = select(func.coalesce(func.sum(ExternalServiceCall.cost_usd), 0)).where(
        ExternalServiceCall.job_id == job_id, _PRICED_CALL, PAID_BY_PAYER
    )
    if action is not None:
        stmt = stmt.where(ExternalServiceCall.action == action)
    return Decimal(await session.scalar(stmt) or 0)
