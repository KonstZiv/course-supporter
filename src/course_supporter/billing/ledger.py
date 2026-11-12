"""The journal of units (BIL-B part 1): credits, reserves, charges, the balance.

Purpose:
    The service layer of the operator's model of units (ratified 2026-11-05).
    BIL-E will call :func:`reserve` when an action starts and :func:`settle` /
    :func:`release` when it ends; nothing calls them yet, and nothing about a
    review or a processing job changes here.

The model, and where it is:

    1. balance = the remaining units of valid batches; available = balance -
       the active reserves (of the other actions) — :func:`balance`
       (:mod:`.journal` for the arithmetic).
    2. A reserve is the least of what the caller asks — the smaller of 2E and
       the path's money ceiling, in units, which the caller computes
       (:func:`course_supporter.cost_estimate.core.reserve_units`) — and the
       available units; taken from the batch that expires first —
       :func:`reserve`.
    3. When the action ends, its fact is charged from its own reserve first,
       then from the available units; the rest of the reserve is released —
       :func:`settle`.
    4. What is not there becomes a difference against the payer — :func:`settle`.
    5. A difference is repaid FIRST from any units that become free — a
       purchase, an admin credit, a reserve released (the rest after a settle,
       or a failure), a refund rejected; the oldest difference first
       (operator 2026-11-05) — :func:`.journal.repay_differences`.
    6. A failure releases the whole reserve and charges nothing —
       :func:`release`.
    7. Batches are valid 6 months; the reserved part of an expired batch
       still serves its action, the rest burns — :mod:`.expiry`.
    8. A difference is annulled after 6 months; never for an author allowed
       credit — :mod:`.expiry`, ``annul_after`` set by :func:`settle`.
    9. An administrator's credit is a batch of its own, with a reason, an
       actor and an audit entry — :func:`credit_admin`.
    10. The author's benefit — :mod:`.benefit`, applied by :func:`settle`;
        re-processing at the platform's initiative — ``payer_source=platform``.
    11. Refunds — :mod:`.refunds`.
    12. Nothing here is deleted with an account's owner: no foreign key leaves
        billing (``storage.orm``, BIL-B block).

An action without a reserve (point 10, re-processing by the platform, or any
action BIL-E chooses not to reserve for) is settled by the same
:func:`settle`, given the payer's ``account_id``: it writes a reservation of
0 units, settled at once. One code path charges every action, and the
action's unique key makes a repeated call a no-op either way.

Transactions and idempotency:
    The functions flush and never commit: the caller owns the transaction.
    Each function that changes units locks the account first
    (:func:`.journal.lock_account`) and is idempotent by its key — the
    action's ``(kind, key)``, the provider's line of a purchase, the
    provider's adjustment of a refund: a repeat changes nothing and returns
    what the first call did. An administrator's credit has no such key: each call is a
    new grant, and its audit entry says who made it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing import audit, benefit
from course_supporter.billing.errors import (
    BillingConflictError,
    BillingInvalidError,
    BillingNotFoundError,
)
from course_supporter.billing.journal import (
    VALIDITY_MONTHS,
    Action,
    Payment,
    add_months,
    allocation_parts,
    available,
    lock_account,
    positions,
    repay_differences,
    require_provider,
    write,
)
from course_supporter.billing.kinds import (
    AccountKind,
    ActionKind,
    AuditAction,
    BatchSource,
    DifferenceState,
    MovementKind,
    PayerSource,
    ReservationState,
)
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuthorSettings,
    BillingBatch,
    BillingDifference,
    BillingMovement,
    BillingReservation,
)

# ── Results ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Balance:
    """Where an account stands (model 1)."""

    balance: int
    """Remaining units of valid batches, plus the reserved and held parts of
    expired batches that still wait for their action or refund."""
    reserved: int
    held: int
    """Units blocked while the provider reviews a refund."""
    available: int
    """``balance - reserved - held``: what a new action can use."""
    difference: int
    """Units still owed (open differences)."""
    next_expiry: datetime | None
    """When the first valid batch with units left expires."""


@dataclass(frozen=True, slots=True)
class Credit:
    """What one credit did."""

    batch: BillingBatch
    repaid_units: int
    """Units of the new batch that repaid differences at once."""
    created: bool
    """False when the call repeated an earlier purchase (nothing changed)."""


@dataclass(frozen=True, slots=True)
class Settlement:
    """What an action's end did; the same on a repeated call."""

    reservation_id: uuid.UUID
    account_id: uuid.UUID
    fact_units: int
    reserved_units: int
    benefit_units: int
    """Covered by the author's benefit (no units taken)."""
    platform_units: int
    """Covered by the platform (re-processing; no units taken)."""
    charged_from_reserve: int
    charged_from_available: int
    released_units: int
    """The rest of the reserve, free again (or waiting to burn, model 7)."""
    difference_units: int
    difference_id: uuid.UUID | None
    repaid_units: int
    """Released units that repaid older differences (model 5)."""

    @property
    def charged_units(self) -> int:
        """Units taken from the payer's batches."""
        return self.charged_from_reserve + self.charged_from_available


@dataclass(frozen=True, slots=True)
class Release:
    """What a failure's release did; the same on a repeated call."""

    reservation_id: uuid.UUID
    account_id: uuid.UUID
    released_units: int
    repaid_units: int


# ── Balance and statement ──────────────────────────────────────────────


async def balance(
    session: AsyncSession, account_id: uuid.UUID, *, now: datetime | None = None
) -> Balance:
    """The account's balance, reserves, holds, availability, difference.

    Raises:
        BillingNotFoundError: no such account.
    """
    now = now or datetime.now(UTC)
    if await session.get(BillingAccount, account_id) is None:
        raise BillingNotFoundError(f"account {account_id} not found")
    found = await positions(session, account_id)
    valid = [p for p in found if p.valid(now)]
    expired = [p for p in found if not p.valid(now)]
    total = sum(p.remaining for p in valid) + sum(p.reserved + p.held for p in expired)
    reserved = sum(p.reserved for p in found)
    held = sum(p.held for p in found)
    owed = await session.scalar(
        select(func.coalesce(func.sum(BillingDifference.outstanding_units), 0)).where(
            BillingDifference.account_id == account_id,
            BillingDifference.state == DifferenceState.OPEN.value,
        )
    )
    return Balance(
        balance=total,
        reserved=reserved,
        held=held,
        available=total - reserved - held,
        difference=int(owed or 0),
        next_expiry=min((p.batch.expires_at for p in valid), default=None),
    )


async def statement(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[BillingMovement]:
    """The account's movements in ``[since, until)``, in the order they happened.

    Each carries its kind, units, batch, action key, the account it was for
    and the payer source.
    """
    stmt = select(BillingMovement).where(BillingMovement.account_id == account_id)
    if since is not None:
        stmt = stmt.where(BillingMovement.created_at >= since)
    if until is not None:
        stmt = stmt.where(BillingMovement.created_at < until)
    rows = await session.execute(
        stmt.order_by(BillingMovement.created_at, BillingMovement.id)
    )
    return list(rows.scalars())


# ── Credits ────────────────────────────────────────────────────────────


def _positive(units: int, what: str) -> None:
    if units <= 0:
        raise BillingInvalidError(f"{what} must be a positive number of units")


async def _new_batch(
    session: AsyncSession, batch: BillingBatch, *, now: datetime, actor: str | None
) -> int:
    session.add(batch)
    await session.flush()
    write(
        session,
        account_id=batch.account_id,
        kind=MovementKind.CREDIT,
        units=batch.units,
        now=now,
        batch_id=batch.id,
        actor=actor,
    )
    await session.flush()
    return sum(
        r.units for r in await repay_differences(session, batch.account_id, now=now)
    )


async def credit_purchase(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    units: int,
    amount_paid_minor: int,
    currency: str,
    provider: str,
    provider_transaction_id: str | None = None,
    provider_line_id: str | None = None,
    now: datetime | None = None,
) -> Credit:
    """A purchased package: a batch valid 6 months, then the difference repaid.

    ``provider`` is who took the payment: a payment provider's adapter name
    (``billing.payments``) or ``invoice`` for a contract invoice. Idempotent
    by the provider's line (``provider`` + ``provider_line_id``) when the ids
    are given: a repeat returns the batch there is.

    Raises:
        BillingInvalidError: not a positive number of units, a negative
            amount, a currency that is not three capital letters, not a
            provider's name (:func:`.journal.require_provider`), or only one
            of the two provider ids.
        BillingNotFoundError / BillingConflictError: no such account; a
            closed one; the provider's line already credited with other
            figures.
    """
    _positive(units, "a purchase")
    if amount_paid_minor < 0:
        raise BillingInvalidError("the amount paid cannot be negative")
    if len(currency) != 3 or not currency.isascii() or not currency.isupper():
        raise BillingInvalidError(f"currency {currency!r} is not an ISO 4217 code")
    provider = require_provider(provider)
    if (provider_transaction_id is None) != (provider_line_id is None):
        raise BillingInvalidError("give both provider ids, or neither")
    now = now or datetime.now(UTC)
    await lock_account(session, account_id, active=True)
    if provider_line_id is not None:
        existing = (
            await session.execute(
                select(BillingBatch).where(
                    BillingBatch.provider == provider,
                    BillingBatch.provider_line_id == provider_line_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            same = (
                existing.account_id,
                existing.units,
                existing.amount_paid_minor,
                existing.currency,
                existing.provider_transaction_id,
            ) == (
                account_id,
                units,
                amount_paid_minor,
                currency,
                provider_transaction_id,
            )
            if not same:
                raise BillingConflictError(
                    f"{provider} line {provider_line_id} is already credited "
                    f"as batch {existing.id} with other figures"
                )
            return Credit(batch=existing, repaid_units=0, created=False)
    batch = BillingBatch(
        account_id=account_id,
        source=BatchSource.PURCHASE.value,
        units=units,
        created_at=now,
        expires_at=add_months(now, VALIDITY_MONTHS),
        amount_paid_minor=amount_paid_minor,
        currency=currency,
        provider=provider,
        provider_transaction_id=provider_transaction_id,
        provider_line_id=provider_line_id,
    )
    repaid = await _new_batch(session, batch, now=now, actor=None)
    return Credit(batch=batch, repaid_units=repaid, created=True)


async def credit_admin(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    units: int,
    reason: str,
    actor: str,
    now: datetime | None = None,
) -> Credit:
    """An administrator's credit: a batch of its own (6 months, never refunded),
    its audit entry (``batch.admin_credit``), then the difference repaid.

    Raises:
        BillingInvalidError: no actor, no reason, not a positive number of units.
        BillingNotFoundError / BillingConflictError: no such account; a closed one.
    """
    actor = audit.require_actor(actor)
    reason = reason.strip()
    if not reason:
        raise BillingInvalidError("an administrator's credit needs a reason")
    _positive(units, "a credit")
    now = now or datetime.now(UTC)
    await lock_account(session, account_id, active=True)
    batch = BillingBatch(
        account_id=account_id,
        source=BatchSource.ADMIN_CREDIT.value,
        units=units,
        created_at=now,
        expires_at=add_months(now, VALIDITY_MONTHS),
        reason=reason,
        actor=actor,
    )
    repaid = await _new_batch(session, batch, now=now, actor=actor)
    await audit.record(
        session,
        actor=actor,
        action=AuditAction.BATCH_ADMIN_CREDIT,
        entity="batch",
        entity_id=batch.id,
        before=None,
        after=audit.batch_snapshot(batch),
    )
    return Credit(batch=batch, repaid_units=repaid, created=True)


# ── Reserve ────────────────────────────────────────────────────────────


async def _reservation(
    session: AsyncSession, action: Action, *, lock: bool = False
) -> BillingReservation | None:
    stmt = select(BillingReservation).where(
        BillingReservation.action_kind == action.kind.value,
        BillingReservation.action_key == action.key,
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def reserve(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    action_key: str,
    account_id: uuid.UUID,
    units: int,
    now: datetime | None = None,
) -> BillingReservation:
    """Hold units for an action at its start (model 2).

    ``units`` is what the caller asks — the smaller of 2E and the path's money
    ceiling, in units; the reserve is that or the available units, whichever
    is less (0 when nothing is available: the action may still run, and its
    cost then goes to the available units and the difference). Taken from
    the batch that expires first. A repeat with the same key returns the
    reservation there is, whatever its state.

    Raises:
        BillingInvalidError: a negative number of units.
        BillingNotFoundError / BillingConflictError: no such account; a closed
            one; the key already reserved on another account.
    """
    if units < 0:
        raise BillingInvalidError("a reserve cannot be negative")
    now = now or datetime.now(UTC)
    action = Action(action_kind, action_key)
    await lock_account(session, account_id, active=True)
    existing = await _reservation(session, action, lock=True)
    if existing is not None:
        if existing.account_id != account_id:
            raise BillingConflictError(
                f"{action_kind.value} {action_key} is reserved on another account"
            )
        return existing
    parts: list[tuple[uuid.UUID, int]] = []
    wanted = units
    for position in available(await positions(session, account_id), now):
        if wanted == 0:
            break
        take = min(position.free, wanted)
        parts.append((position.batch.id, take))
        wanted -= take
    reservation = BillingReservation(
        action_kind=action_kind.value,
        action_key=action_key,
        account_id=account_id,
        units=units - wanted,
        allocation=[{"batch_id": str(b), "units": n} for b, n in parts],
        state=ReservationState.ACTIVE.value,
        created_at=now,
    )
    session.add(reservation)
    await session.flush()
    for batch_id, take in parts:
        write(
            session,
            account_id=account_id,
            kind=MovementKind.RESERVE,
            units=take,
            now=now,
            batch_id=batch_id,
            action=action,
        )
    await session.flush()
    return reservation


# ── Settle and release ─────────────────────────────────────────────────


async def _sums(
    session: AsyncSession, account_id: uuid.UUID, action: Action
) -> dict[str, int]:
    rows = await session.execute(
        select(BillingMovement.kind, func.sum(BillingMovement.units))
        .where(
            BillingMovement.account_id == account_id,
            BillingMovement.action_kind == action.kind.value,
            BillingMovement.action_key == action.key,
        )
        .group_by(BillingMovement.kind)
    )
    return {kind: int(units) for kind, units in rows.tuples()}


async def _settlement_of(
    session: AsyncSession, reservation: BillingReservation
) -> Settlement:
    """The outcome of a settled action, read back from the journal."""
    action = Action(ActionKind(reservation.action_kind), reservation.action_key)
    sums = await _sums(session, reservation.account_id, action)
    charged = sums.get(MovementKind.DEBIT.value, 0)
    from_reserve = min(charged, reservation.units)
    difference = (
        await session.execute(
            select(BillingDifference.id).where(
                BillingDifference.action_kind == action.kind.value,
                BillingDifference.action_key == action.key,
            )
        )
    ).scalar_one_or_none()
    return Settlement(
        reservation_id=reservation.id,
        account_id=reservation.account_id,
        fact_units=reservation.fact_units or 0,
        reserved_units=reservation.units,
        benefit_units=sums.get(MovementKind.BENEFIT_COVERED.value, 0),
        platform_units=sums.get(MovementKind.PLATFORM_COVERED.value, 0),
        charged_from_reserve=from_reserve,
        charged_from_available=charged - from_reserve,
        released_units=sums.get(MovementKind.RESERVE_RELEASE.value, 0),
        difference_units=sums.get(MovementKind.DIFFERENCE_ARISEN.value, 0),
        difference_id=difference,
        repaid_units=sums.get(MovementKind.DIFFERENCE_REPAID.value, 0),
    )


async def _credit_allowed(session: AsyncSession, account: BillingAccount) -> bool:
    if account.kind != AccountKind.AUTHOR.value:
        return False
    settings = await session.get(BillingAuthorSettings, account.id)
    return settings is not None and settings.credit_allowed


async def _finish(
    session: AsyncSession,
    reservation: BillingReservation,
    state: ReservationState,
    *,
    now: datetime,
    fact_units: int | None,
) -> None:
    await session.execute(
        update(BillingReservation)
        .where(BillingReservation.id == reservation.id)
        .values(state=state.value, finished_at=now, fact_units=fact_units)
    )
    await session.refresh(reservation)


async def settle(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    action_key: str,
    fact_units: int,
    beneficiary_account_id: uuid.UUID,
    payer_source: PayerSource,
    rule_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> Settlement:
    """Charge an action that ended (models 3, 4, 5, 10).

    In order: re-processing by the platform (``payer_source=platform``) takes
    nothing — a ``platform_covered`` movement; otherwise the author's benefit
    covers what it covers (:mod:`.benefit`) — a ``benefit_covered`` movement;
    the rest is charged from the action's reserve, then from the available
    units; what is still missing is a difference against the payer; the rest
    of the reserve is released, and the released units repay older
    differences first.

    ``account_id`` (the payer) is needed only when the action has no reserve;
    with one, it must match. The movements carry ``beneficiary_account_id``
    ("за кого"), ``payer_source`` and ``rule_id`` as :func:`.payer.resolve_payer`
    gave them. A repeat with the same fact returns the first outcome.

    Raises:
        BillingInvalidError: a negative fact; ``payer_source=benefit`` (the
            benefit is decided here, not by the caller).
        BillingNotFoundError: no reserve and no ``account_id``; no such account.
        BillingConflictError: the action was released (it failed), was settled
            with another fact, or is reserved on another account.
    """
    if fact_units < 0:
        raise BillingInvalidError("an action cannot cost a negative number of units")
    if payer_source is PayerSource.BENEFIT:
        raise BillingInvalidError("the benefit is applied by settle, not passed to it")
    now = now or datetime.now(UTC)
    action = Action(action_kind, action_key)
    found = await _reservation(session, action)
    if found is not None and account_id is not None and found.account_id != account_id:
        raise BillingConflictError(
            f"{action_kind.value} {action_key} is reserved on another account"
        )
    payer_id = found.account_id if found is not None else account_id
    if payer_id is None:
        raise BillingNotFoundError(
            f"no reserve for {action_kind.value} {action_key}; "
            "give the payer's account_id to charge without one"
        )
    account = await lock_account(session, payer_id)
    reservation = await _reservation(session, action, lock=True)
    if reservation is None:
        reservation = BillingReservation(
            action_kind=action_kind.value,
            action_key=action_key,
            account_id=payer_id,
            units=0,
            allocation=[],
            state=ReservationState.ACTIVE.value,
            created_at=now,
        )
        session.add(reservation)
        await session.flush()
    elif reservation.state == ReservationState.SETTLED.value:
        if reservation.fact_units != fact_units:
            raise BillingConflictError(
                f"{action_kind.value} {action_key} was settled with "
                f"{reservation.fact_units} units, not {fact_units}"
            )
        return await _settlement_of(session, reservation)
    elif reservation.state == ReservationState.RELEASED.value:
        raise BillingConflictError(
            f"{action_kind.value} {action_key} was released (the action failed)"
        )

    payment = Payment(beneficiary_account_id, payer_source, rule_id)
    to_charge = fact_units
    if payer_source is PayerSource.PLATFORM:
        if fact_units:
            write(
                session,
                account_id=payer_id,
                kind=MovementKind.PLATFORM_COVERED,
                units=fact_units,
                now=now,
                action=action,
                payment=payment,
            )
        to_charge = 0
    else:
        covered = await benefit.covered_units(
            session, account, action_kind, fact_units, now=now
        )
        if covered:
            write(
                session,
                account_id=payer_id,
                kind=MovementKind.BENEFIT_COVERED,
                units=covered,
                now=now,
                action=action,
                payment=Payment(beneficiary_account_id, PayerSource.BENEFIT, rule_id),
            )
            to_charge -= covered

    found_positions = await positions(session, payer_id)
    for batch_id, units in allocation_parts(reservation):
        take = min(units, to_charge)
        to_charge -= take
        if take:
            write(
                session,
                account_id=payer_id,
                kind=MovementKind.DEBIT,
                units=take,
                now=now,
                batch_id=batch_id,
                action=action,
                payment=payment,
            )
        if units - take:
            write(
                session,
                account_id=payer_id,
                kind=MovementKind.RESERVE_RELEASE,
                units=units - take,
                now=now,
                batch_id=batch_id,
                action=action,
            )
    for position in available(found_positions, now):
        if to_charge == 0:
            break
        take = min(position.free, to_charge)
        write(
            session,
            account_id=payer_id,
            kind=MovementKind.DEBIT,
            units=take,
            now=now,
            batch_id=position.batch.id,
            action=action,
            payment=payment,
        )
        to_charge -= take
    if to_charge:
        difference = BillingDifference(
            account_id=payer_id,
            action_kind=action_kind.value,
            action_key=action_key,
            units=to_charge,
            outstanding_units=to_charge,
            state=DifferenceState.OPEN.value,
            created_at=now,
            annul_after=(
                None
                if await _credit_allowed(session, account)
                else add_months(now, VALIDITY_MONTHS)
            ),
        )
        session.add(difference)
        await session.flush()
        write(
            session,
            account_id=payer_id,
            kind=MovementKind.DIFFERENCE_ARISEN,
            units=to_charge,
            now=now,
            difference_id=difference.id,
            action=action,
            payment=payment,
        )
    await session.flush()
    await _finish(
        session, reservation, ReservationState.SETTLED, now=now, fact_units=fact_units
    )
    await repay_differences(session, payer_id, now=now, action=action)
    return await _settlement_of(session, reservation)


async def release(
    session: AsyncSession,
    *,
    action_kind: ActionKind,
    action_key: str,
    now: datetime | None = None,
) -> Release:
    """The action failed: release its whole reserve, charge nothing (model 6).

    The released units repay older differences first (model 5). A repeat
    returns the first outcome.

    Raises:
        BillingNotFoundError: the action has no reserve.
        BillingConflictError: the action was settled.
    """
    now = now or datetime.now(UTC)
    action = Action(action_kind, action_key)
    found = await _reservation(session, action)
    if found is None:
        raise BillingNotFoundError(f"no reserve for {action_kind.value} {action_key}")
    await lock_account(session, found.account_id)
    reservation = await _reservation(session, action, lock=True)
    assert reservation is not None  # a reservation is never deleted
    if reservation.state == ReservationState.SETTLED.value:
        raise BillingConflictError(
            f"{action_kind.value} {action_key} was settled; it cannot be released"
        )
    if reservation.state == ReservationState.ACTIVE.value:
        for batch_id, units in allocation_parts(reservation):
            write(
                session,
                account_id=reservation.account_id,
                kind=MovementKind.RESERVE_RELEASE,
                units=units,
                now=now,
                batch_id=batch_id,
                action=action,
            )
        await session.flush()
        await _finish(
            session, reservation, ReservationState.RELEASED, now=now, fact_units=None
        )
        await repay_differences(session, reservation.account_id, now=now, action=action)
    sums = await _sums(session, reservation.account_id, action)
    return Release(
        reservation_id=reservation.id,
        account_id=reservation.account_id,
        released_units=sums.get(MovementKind.RESERVE_RELEASE.value, 0),
        repaid_units=sums.get(MovementKind.DIFFERENCE_REPAID.value, 0),
    )
