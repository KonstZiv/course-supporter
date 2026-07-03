"""Integration tests for StudentCredentialTokenRepository (Phase 6 R2).

Requires ``docker compose up -d`` (PostgreSQL). Run with ``--run-db``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.auth.recovery_tokens import (
    PURPOSE_EMAIL_CONFIRM,
    PURPOSE_PASSWORD_RESET,
)
from course_supporter.storage.orm import StudentCredential, Tenant
from course_supporter.storage.student_credential_repository import (
    StudentCredentialRepository,
)
from course_supporter.storage.student_credential_token_repository import (
    StudentCredentialTokenRepository,
)
from course_supporter.storage.student_repository import StudentRepository

pytestmark = pytest.mark.requires_db


async def _make_credential(
    session: AsyncSession, tenant: Tenant, login: str
) -> StudentCredential:
    student = await StudentRepository(session).create(
        tenant_id=tenant.id, external_id=f"ext-{login}"
    )
    return await StudentCredentialRepository(session).create(
        student_id=student.id,
        tenant_id=tenant.id,
        login=login,
        password_hash="$argon2id$hash",
    )


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


def _past() -> datetime:
    return datetime.now(UTC) - timedelta(hours=1)


class TestIssue:
    async def test_issue_burns_unused_same_purpose_sibling(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        cred = await _make_credential(db_session, seed_tenant, "burn-1")
        repo = StudentCredentialTokenRepository(db_session)

        first = await repo.issue(
            credential_id=cred.id,
            purpose=PURPOSE_PASSWORD_RESET,
            token_hash="hash-old",
            expires_at=_future(),
        )
        await repo.issue(
            credential_id=cred.id,
            purpose=PURPOSE_PASSWORD_RESET,
            token_hash="hash-new",
            expires_at=_future(),
        )

        burned = await repo.get_by_hash("hash-old")
        assert burned is not None
        assert burned.id == first.id
        assert burned.used_at is not None  # the old sibling was burned

    async def test_issue_does_not_burn_other_purpose(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        cred = await _make_credential(db_session, seed_tenant, "burn-2")
        repo = StudentCredentialTokenRepository(db_session)
        await repo.issue(
            credential_id=cred.id,
            purpose=PURPOSE_EMAIL_CONFIRM,
            token_hash="confirm-keep",
            expires_at=_future(),
        )
        await repo.issue(
            credential_id=cred.id,
            purpose=PURPOSE_PASSWORD_RESET,
            token_hash="reset-new",
            expires_at=_future(),
        )
        confirm = await repo.get_by_hash("confirm-keep")
        assert confirm is not None
        assert confirm.used_at is None  # a different purpose is untouched


class TestRedeem:
    async def test_redeem_valid_returns_credential_id_and_burns(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        cred = await _make_credential(db_session, seed_tenant, "redeem-1")
        repo = StudentCredentialTokenRepository(db_session)
        await repo.issue(
            credential_id=cred.id,
            purpose=PURPOSE_PASSWORD_RESET,
            token_hash="valid-hash",
            expires_at=_future(),
        )
        got = await repo.redeem(token_hash="valid-hash", purpose=PURPOSE_PASSWORD_RESET)
        assert got == cred.id

        used = await repo.get_by_hash("valid-hash")
        assert used is not None
        assert used.used_at is not None

    async def test_redeem_is_single_use(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        cred = await _make_credential(db_session, seed_tenant, "redeem-2")
        repo = StudentCredentialTokenRepository(db_session)
        await repo.issue(
            credential_id=cred.id,
            purpose=PURPOSE_PASSWORD_RESET,
            token_hash="once-hash",
            expires_at=_future(),
        )
        assert (
            await repo.redeem(token_hash="once-hash", purpose=PURPOSE_PASSWORD_RESET)
            == cred.id
        )
        assert (
            await repo.redeem(token_hash="once-hash", purpose=PURPOSE_PASSWORD_RESET)
            is None
        )

    async def test_redeem_expired_returns_none(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        cred = await _make_credential(db_session, seed_tenant, "redeem-3")
        repo = StudentCredentialTokenRepository(db_session)
        await repo.issue(
            credential_id=cred.id,
            purpose=PURPOSE_PASSWORD_RESET,
            token_hash="expired-hash",
            expires_at=_past(),
        )
        assert (
            await repo.redeem(token_hash="expired-hash", purpose=PURPOSE_PASSWORD_RESET)
            is None
        )

    async def test_redeem_wrong_purpose_returns_none(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        cred = await _make_credential(db_session, seed_tenant, "redeem-4")
        repo = StudentCredentialTokenRepository(db_session)
        await repo.issue(
            credential_id=cred.id,
            purpose=PURPOSE_PASSWORD_RESET,
            token_hash="purpose-hash",
            expires_at=_future(),
        )
        # Right hash, wrong purpose → no redemption.
        assert (
            await repo.redeem(token_hash="purpose-hash", purpose=PURPOSE_EMAIL_CONFIRM)
            is None
        )

    async def test_redeem_unknown_hash_returns_none(
        self, db_session: AsyncSession, seed_tenant: Tenant
    ) -> None:
        repo = StudentCredentialTokenRepository(db_session)
        assert (
            await repo.redeem(token_hash="nope", purpose=PURPOSE_PASSWORD_RESET) is None
        )
