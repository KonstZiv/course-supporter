"""Payer accounts: lazy creation, and anonymization after the owner's deletion.

Purpose:
    A student has one active account per tenant they belong to; an author (the
    tenant) has one. Neither is created up front: the first ACTION THAT
    CHANGES STATE and needs an account creates it
    (:func:`get_or_create_student_account`,
    :func:`get_or_create_author_account`) — a consent, a checkout, the
    administrator's mark, settings, rule or credit, the start of a paid
    action. A read never creates one (BIL-E3): it asks
    :func:`find_active_account` and, finding none, answers as for an empty
    account (:func:`.payer.find_payer` is the read side of
    :func:`.payer.resolve_payer`).

    The creation writes one audit entry in the same transaction
    (``account.create``, :mod:`.audit`): the account without its owner, by the
    administrator's ``--actor`` or by :data:`.audit.SYSTEM_ACTOR`. Two
    concurrent first calls create one account and one entry — the insert
    yields to the partial unique index, only the call whose insert returned
    the row writes the entry, and both read the row that won.

Deletion:
    Deleting a tenant or a student never touches an account and never fails
    on one: the account names its owner by plain ids, without foreign keys.
    What remains is to cut the account from the deleted owner:
    :func:`anonymize_student_accounts` / :func:`anonymize_tenant_accounts`
    close the account, erase ``tenant_id`` / ``student_id`` and stamp
    ``anonymized_at``; in the same transaction they END the active payment
    rules about the deleted owner (a student's student rules; every rule of
    a tenant), each with a ``payment_rule.end`` audit entry, the way the
    administrator ends one (:func:`.rules.end_rule`). The account itself, its
    consents, its rules and the audit log stay, pointing at its opaque id; the
    target ids inside an ended rule stay too (the rule's check constraint
    needs them, and they name rows that no longer exist). ``anonymized_at``
    starts the retention period; destroying the account after it is BIL-B.

    A student's account is deleted at the student's request by
    ``scripts/student_admin.py delete`` (Б-2, ``student_deletion``), which
    calls :func:`anonymize_student_accounts` in its own transaction. A tenant
    has no deletion flow yet: after deleting one by hand the administrator
    runs ``scripts/billing_admin.py anonymize-tenant``
    (``anonymize-student`` stays for a student deleted by other means).
    Nothing else closes an active account.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing import audit
from course_supporter.billing.errors import BillingConflictError
from course_supporter.billing.kinds import (
    AccountKind,
    AccountStatus,
    AuditAction,
    RuleScope,
)
from course_supporter.billing.rules import end_rule
from course_supporter.storage.orm import (
    BillingAccount,
    BillingPaymentRule,
    Student,
    Tenant,
)

_ACTIVE_AUTHOR = text("kind = 'author' AND status = 'active'")
_ACTIVE_STUDENT = text("kind = 'student' AND status = 'active'")


@dataclass(frozen=True)
class Anonymization:
    """What one anonymization changed; both empty when it was done before."""

    account_ids: list[uuid.UUID]
    """Accounts closed and anonymized."""
    ended_rule_ids: list[uuid.UUID]
    """Active payment rules about the deleted owner, now ended."""


async def find_active_account(
    session: AsyncSession,
    *,
    kind: AccountKind,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID | None = None,
) -> BillingAccount | None:
    """The owner's active account, or None; creates nothing."""
    stmt = select(BillingAccount).where(
        BillingAccount.kind == kind.value,
        BillingAccount.status == AccountStatus.ACTIVE.value,
        BillingAccount.tenant_id == tenant_id,
    )
    if kind is AccountKind.STUDENT:
        stmt = stmt.where(BillingAccount.student_id == student_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _get_or_create(
    session: AsyncSession,
    *,
    kind: AccountKind,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID | None,
    actor: str,
) -> BillingAccount:
    found = await find_active_account(
        session, kind=kind, tenant_id=tenant_id, student_id=student_id
    )
    if found is not None:
        return found
    actor = audit.require_actor(actor)
    if kind is AccountKind.AUTHOR:
        elements, where = ["tenant_id"], _ACTIVE_AUTHOR
    else:
        elements, where = ["tenant_id", "student_id"], _ACTIVE_STUDENT
    inserted = await session.execute(
        insert(BillingAccount)
        .values(kind=kind.value, tenant_id=tenant_id, student_id=student_id)
        .on_conflict_do_nothing(index_elements=elements, index_where=where)
        .returning(BillingAccount.id)
    )
    inserted_id = inserted.scalar_one_or_none()
    created = await find_active_account(
        session, kind=kind, tenant_id=tenant_id, student_id=student_id
    )
    assert created is not None  # inserted, or the concurrent winner's row
    if inserted_id is not None:  # this call inserted it; the loser writes nothing
        assert inserted_id == created.id
        await audit.record(
            session,
            actor=actor,
            action=AuditAction.ACCOUNT_CREATE,
            entity="account",
            entity_id=created.id,
            before=None,
            after=audit.account_snapshot(created),
        )
    return created


async def get_or_create_student_account(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID,
    actor: str = audit.SYSTEM_ACTOR,
) -> BillingAccount:
    """The student's active account in the tenant, created on first need.

    For an action that changes state only; a read uses
    :func:`find_active_account`. The caller vouches that the student belongs
    to the tenant (it comes from an authenticated context or a loaded row);
    nothing is looked up here. ``actor`` — the administrator's ``--actor``
    when the administrator acts; else :data:`.audit.SYSTEM_ACTOR`. A creation
    is audited (``account.create``).
    """
    return await _get_or_create(
        session,
        kind=AccountKind.STUDENT,
        tenant_id=tenant_id,
        student_id=student_id,
        actor=actor,
    )


async def get_or_create_author_account(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor: str = audit.SYSTEM_ACTOR
) -> BillingAccount:
    """The author's (the tenant's) active account, created on first need.

    As :func:`get_or_create_student_account`: for an action that changes
    state; a creation is audited by ``actor``.
    """
    return await _get_or_create(
        session,
        kind=AccountKind.AUTHOR,
        tenant_id=tenant_id,
        student_id=None,
        actor=actor,
    )


async def _anonymize(
    session: AsyncSession,
    accounts: list[BillingAccount],
    *,
    actor: str,
    now: datetime,
) -> None:
    # A Core UPDATE, not an attribute assignment: the repository allows no
    # ``<row>.status = ...`` anywhere in src (tests/unit/test_l1a_invariants).
    for account in accounts:
        before = audit.account_snapshot(account)
        await session.execute(
            update(BillingAccount)
            .where(BillingAccount.id == account.id)
            .values(
                status=AccountStatus.CLOSED.value,
                closed_at=func.coalesce(BillingAccount.closed_at, now),
                tenant_id=None,
                student_id=None,
                anonymized_at=now,
            )
        )
        await session.refresh(account)
        await audit.record(
            session,
            actor=actor,
            action=AuditAction.ACCOUNT_ANONYMIZE,
            entity="account",
            entity_id=account.id,
            before=before,
            after=audit.account_snapshot(account),
        )


async def _anonymize_and_end(
    session: AsyncSession,
    accounts: list[BillingAccount],
    rules: list[BillingPaymentRule],
    *,
    actor: str,
    now: datetime,
) -> Anonymization:
    await _anonymize(session, accounts, actor=actor, now=now)
    for rule in rules:
        await end_rule(session, rule, actor=actor, now=now)
    return Anonymization(
        account_ids=[account.id for account in accounts],
        ended_rule_ids=[rule.id for rule in rules],
    )


async def anonymize_student_accounts(
    session: AsyncSession,
    student_id: uuid.UUID,
    *,
    actor: str,
    now: datetime | None = None,
) -> Anonymization:
    """Close and anonymize the accounts of a DELETED student and end the
    active student rules for them (``scope = student``), in one transaction.

    Called by the student deletion (``student_deletion.delete_student``,
    module docstring). Refuses (:class:`BillingConflictError`) while the
    student still exists and is not soft-deleted, so a live student's account
    is never closed. Rules about other students and course rules stay active.
    Idempotent: a second call finds nothing to change and writes nothing.
    """
    actor = audit.require_actor(actor)
    student = await session.get(Student, student_id)
    if student is not None and student.deleted_at is None:
        raise BillingConflictError(
            f"student {student_id} is not deleted; delete the student first"
        )
    accounts = await session.execute(
        select(BillingAccount)
        .where(
            BillingAccount.kind == AccountKind.STUDENT.value,
            BillingAccount.student_id == student_id,
            BillingAccount.anonymized_at.is_(None),
        )
        .with_for_update()
    )
    rules = await session.execute(
        select(BillingPaymentRule)
        .where(
            BillingPaymentRule.active.is_(True),
            BillingPaymentRule.scope == RuleScope.STUDENT.value,
            BillingPaymentRule.student_id == student_id,
        )
        .with_for_update()
    )
    return await _anonymize_and_end(
        session,
        list(accounts.scalars()),
        list(rules.scalars()),
        actor=actor,
        now=now or datetime.now(UTC),
    )


async def anonymize_tenant_accounts(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    actor: str,
    now: datetime | None = None,
) -> Anonymization:
    """Close and anonymize every account of a DELETED tenant — the author's
    and its students' — and end every active rule of the tenant (course and
    student rules), in one transaction.

    TO DO: call from the account-deletion flow when it appears (module
    docstring). Refuses (:class:`BillingConflictError`) while the tenant still
    exists and is not soft-deleted (a deactivated tenant is not deleted).
    Idempotent like :func:`anonymize_student_accounts`.
    """
    actor = audit.require_actor(actor)
    tenant = await session.get(Tenant, tenant_id)
    if tenant is not None and tenant.deleted_at is None:
        raise BillingConflictError(
            f"tenant {tenant_id} is not deleted; delete the tenant first"
        )
    accounts = await session.execute(
        select(BillingAccount)
        .where(
            BillingAccount.tenant_id == tenant_id,
            BillingAccount.anonymized_at.is_(None),
        )
        .with_for_update()
    )
    rules = await session.execute(
        select(BillingPaymentRule)
        .where(
            BillingPaymentRule.active.is_(True),
            BillingPaymentRule.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    return await _anonymize_and_end(
        session,
        list(accounts.scalars()),
        list(rules.scalars()),
        actor=actor,
        now=now or datetime.now(UTC),
    )
