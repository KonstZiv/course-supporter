"""Consent to the billing terms in force (BIL-A).

Purpose:
    Before a student or an author pays for anything, they agree to the billing
    terms — once per revision, by the same mechanism (operator 2026-11-12): a
    student in the portal, an author through the API with the tenant's key.
    The revision in force is ``settings.billing_terms_version`` (the date it
    took effect). Consent is required when the account has no consent to THAT
    revision — so raising the setting asks everyone again.

Records:
    One ``billing_consents`` row per (account, revision), insert-only (a
    trigger refuses UPDATE). Agreeing again to the same revision writes
    nothing and returns the first record. ``text_sha256`` is the fingerprint of
    the text the holder was shown, as the client reports it: the server keeps
    no copy of the terms text, so it checks the form of the fingerprint only.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing.accounts import (
    find_active_account,
    get_or_create_author_account,
    get_or_create_student_account,
)
from course_supporter.billing.errors import BillingConflictError
from course_supporter.billing.kinds import AccountKind, ConsentChannel
from course_supporter.storage.orm import BillingAccount, BillingConsent


@dataclass(frozen=True)
class TermsStatus:
    """The revision in force and where the holder stands with it."""

    terms_version: str
    consent_required: bool
    consented_at: datetime | None


async def _consent(
    session: AsyncSession, account_id: uuid.UUID, terms_version: str
) -> BillingConsent | None:
    return (
        await session.execute(
            select(BillingConsent).where(
                BillingConsent.account_id == account_id,
                BillingConsent.terms_version == terms_version,
            )
        )
    ).scalar_one_or_none()


async def consent_required(
    session: AsyncSession, *, account_id: uuid.UUID, terms_version: str
) -> bool:
    """True when the account has no consent to ``terms_version``."""
    return await _consent(session, account_id, terms_version) is None


async def _status(
    session: AsyncSession, account: BillingAccount | None, terms_version: str
) -> TermsStatus:
    """An account without a row yet has not agreed to anything."""
    found = (
        await _consent(session, account.id, terms_version)
        if account is not None
        else None
    )
    return TermsStatus(
        terms_version=terms_version,
        consent_required=found is None,
        consented_at=found.consented_at if found is not None else None,
    )


async def student_terms_status(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID,
    terms_version: str,
) -> TermsStatus:
    """Where the student stands with the revision in force; creates nothing."""
    account = await find_active_account(
        session, kind=AccountKind.STUDENT, tenant_id=tenant_id, student_id=student_id
    )
    return await _status(session, account, terms_version)


async def author_terms_status(
    session: AsyncSession, *, tenant_id: uuid.UUID, terms_version: str
) -> TermsStatus:
    """Where the author (the tenant) stands with the revision in force;
    creates nothing."""
    account = await find_active_account(
        session, kind=AccountKind.AUTHOR, tenant_id=tenant_id
    )
    return await _status(session, account, terms_version)


def _require_current(terms_version: str, current_version: str) -> None:
    if terms_version != current_version:
        raise BillingConflictError(
            f"terms version {terms_version} is not the current {current_version}"
        )


async def record_student_consent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID,
    terms_version: str,
    current_version: str,
    text_sha256: str,
    channel: ConsentChannel,
) -> tuple[BillingConsent, bool]:
    """Record the student's consent; return the record and whether it is new.

    Creates the student's account on first need.

    Raises:
        BillingConflictError: ``terms_version`` is not the revision in force
            (the holder was shown another text).
    """
    _require_current(terms_version, current_version)
    account = await get_or_create_student_account(
        session, tenant_id=tenant_id, student_id=student_id
    )
    return await _record(session, account, terms_version, text_sha256, channel)


async def record_author_consent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    terms_version: str,
    current_version: str,
    text_sha256: str,
) -> tuple[BillingConsent, bool]:
    """Record the author's consent (channel ``api``); return the record and
    whether it is new.

    Creates the tenant's author account on first need.

    Raises:
        BillingConflictError: ``terms_version`` is not the revision in force.
    """
    _require_current(terms_version, current_version)
    account = await get_or_create_author_account(session, tenant_id=tenant_id)
    return await _record(
        session, account, terms_version, text_sha256, ConsentChannel.API
    )


async def _record(
    session: AsyncSession,
    account: BillingAccount,
    terms_version: str,
    text_sha256: str,
    channel: ConsentChannel,
) -> tuple[BillingConsent, bool]:
    inserted = await session.execute(
        insert(BillingConsent)
        .values(
            account_id=account.id,
            terms_version=terms_version,
            text_sha256=text_sha256,
            channel=channel.value,
        )
        .on_conflict_do_nothing(constraint="uq_billing_consents_version")
        .returning(BillingConsent.id)
    )
    created = inserted.scalar_one_or_none() is not None
    record = await _consent(session, account.id, terms_version)
    assert record is not None  # inserted, or the earlier consent
    return record, created
