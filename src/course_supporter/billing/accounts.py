"""Payer accounts: lazy creation, and anonymization after the owner's deletion.

Purpose:
    A student has one active account per tenant they belong to; an author (the
    tenant) has one. Neither is created up front: the first call that needs an
    account creates it (:func:`get_or_create_student_account`,
    :func:`get_or_create_author_account`). Two concurrent first calls create
    one account — the insert yields to the partial unique index and both read
    the row that won.

Deletion:
    Deleting a tenant or a student never touches an account and never fails
    on one: the account names its owner by plain ids, without foreign keys.
    What remains is to cut the account from the deleted owner:
    :func:`anonymize_student_accounts` / :func:`anonymize_tenant_accounts`
    close the account, erase ``tenant_id`` / ``student_id`` and stamp
    ``anonymized_at``. The account itself, its consents, its rules and the
    audit log stay, pointing at its opaque id. ``anonymized_at`` starts the
    retention period; destroying the account after it is BIL-B.

    TO DO: wire both functions into the account-deletion flow when it
    appears. Today the system has none — a tenant or a student is deleted by
    hand — so the administrator runs ``scripts/billing_admin.py
    anonymize-student`` / ``anonymize-tenant`` after the deletion. Nothing
    calls them automatically, and nothing else closes an active account.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing import audit
from course_supporter.billing.errors import BillingConflictError
from course_supporter.billing.kinds import AccountKind, AccountStatus, AuditAction
from course_supporter.storage.orm import BillingAccount, Student, Tenant

_ACTIVE_AUTHOR = text("kind = 'author' AND status = 'active'")
_ACTIVE_STUDENT = text("kind = 'student' AND status = 'active'")


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
) -> BillingAccount:
    found = await find_active_account(
        session, kind=kind, tenant_id=tenant_id, student_id=student_id
    )
    if found is not None:
        return found
    if kind is AccountKind.AUTHOR:
        elements, where = ["tenant_id"], _ACTIVE_AUTHOR
    else:
        elements, where = ["tenant_id", "student_id"], _ACTIVE_STUDENT
    await session.execute(
        insert(BillingAccount)
        .values(kind=kind.value, tenant_id=tenant_id, student_id=student_id)
        .on_conflict_do_nothing(index_elements=elements, index_where=where)
    )
    created = await find_active_account(
        session, kind=kind, tenant_id=tenant_id, student_id=student_id
    )
    assert created is not None  # inserted, or the concurrent winner's row
    return created


async def get_or_create_student_account(
    session: AsyncSession, *, tenant_id: uuid.UUID, student_id: uuid.UUID
) -> BillingAccount:
    """The student's active account in the tenant, created on first need.

    The caller vouches that the student belongs to the tenant (it comes from
    an authenticated context or a loaded row); nothing is looked up here.
    """
    return await _get_or_create(
        session, kind=AccountKind.STUDENT, tenant_id=tenant_id, student_id=student_id
    )


async def get_or_create_author_account(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> BillingAccount:
    """The author's (the tenant's) active account, created on first need."""
    return await _get_or_create(
        session, kind=AccountKind.AUTHOR, tenant_id=tenant_id, student_id=None
    )


async def _anonymize(
    session: AsyncSession,
    accounts: list[BillingAccount],
    *,
    actor: str,
    now: datetime,
) -> list[uuid.UUID]:
    for account in accounts:
        before = audit.account_snapshot(account)
        account.status = AccountStatus.CLOSED.value
        account.closed_at = account.closed_at or now
        account.tenant_id = None
        account.student_id = None
        account.anonymized_at = now
        await session.flush()
        await audit.record(
            session,
            actor=actor,
            action=AuditAction.ACCOUNT_ANONYMIZE,
            entity="account",
            entity_id=account.id,
            before=before,
            after=audit.account_snapshot(account),
        )
    return [account.id for account in accounts]


async def anonymize_student_accounts(
    session: AsyncSession,
    student_id: uuid.UUID,
    *,
    actor: str,
    now: datetime | None = None,
) -> list[uuid.UUID]:
    """Close and anonymize the accounts of a DELETED student; return their ids.

    TO DO: call from the account-deletion flow when it appears (module
    docstring). Refuses (:class:`BillingConflictError`) while the student
    still exists and is not soft-deleted, so a live student's account is never
    closed. Idempotent: an already anonymized account is not touched again.
    """
    actor = audit.require_actor(actor)
    student = await session.get(Student, student_id)
    if student is not None and student.deleted_at is None:
        raise BillingConflictError(
            f"student {student_id} is not deleted; delete the student first"
        )
    rows = await session.execute(
        select(BillingAccount)
        .where(
            BillingAccount.kind == AccountKind.STUDENT.value,
            BillingAccount.student_id == student_id,
            BillingAccount.anonymized_at.is_(None),
        )
        .with_for_update()
    )
    return await _anonymize(
        session, list(rows.scalars()), actor=actor, now=now or datetime.now(UTC)
    )


async def anonymize_tenant_accounts(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    actor: str,
    now: datetime | None = None,
) -> list[uuid.UUID]:
    """Close and anonymize every account of a DELETED tenant — the author's
    and its students' — and return their ids.

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
    rows = await session.execute(
        select(BillingAccount)
        .where(
            BillingAccount.tenant_id == tenant_id,
            BillingAccount.anonymized_at.is_(None),
        )
        .with_for_update()
    )
    return await _anonymize(
        session, list(rows.scalars()), actor=actor, now=now or datetime.now(UTC)
    )
