"""Repository for StudentCredential operations (Phase 6 T1, KD17)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import StudentCredential


class StudentCredentialRepository:
    """Repository for native portal login credentials.

    A credential is 1:1-optional with a Student; ``login`` is unique per
    tenant. Tenant isolation is enforced at the API layer.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        student_id: uuid.UUID,
        tenant_id: uuid.UUID,
        login: str,
        password_hash: str,
    ) -> StudentCredential:
        """Create a credential.

        Raises ``IntegrityError`` on a duplicate ``(tenant_id, login)`` or a
        student that already has a credential (UNIQUE ``student_id``) — the
        route maps either to 409.
        """
        credential = StudentCredential(
            student_id=student_id,
            tenant_id=tenant_id,
            login=login,
            password_hash=password_hash,
        )
        self._session.add(credential)
        await self._session.flush()
        return credential

    async def get_active_by_tenant_login(
        self,
        tenant_id: uuid.UUID,
        login: str,
    ) -> StudentCredential | None:
        """Get an ACTIVE credential by (tenant_id, login) — the login lookup.

        Returns None for an unknown login OR a revoked (``is_active=False``)
        credential; the login route returns the same generic 401 for both so
        existence is not leaked.
        """
        stmt = select(StudentCredential).where(
            StudentCredential.tenant_id == tenant_id,
            StudentCredential.login == login,
            StudentCredential.is_active.is_(True),
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_student_id(
        self,
        student_id: uuid.UUID,
    ) -> StudentCredential | None:
        """Get a student's credential (1:1), active or not."""
        stmt = select(StudentCredential).where(
            StudentCredential.student_id == student_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def set_active(
        self,
        student_id: uuid.UUID,
        *,
        is_active: bool,
    ) -> bool:
        """Toggle a credential's ``is_active`` (revoke / restore access).

        Returns True if a credential row was updated, False if the student has
        none. Revoke (``is_active=False``) keeps the Student and history
        (DD-6-A); existing bearer tokens are rejected on their next request.
        """
        stmt = (
            update(StudentCredential)
            .where(StudentCredential.student_id == student_id)
            .values(is_active=is_active)
        )
        result: CursorResult[Any] = await self._session.execute(stmt)  # type: ignore[assignment]
        await self._session.flush()
        return result.rowcount > 0

    async def get_by_id(
        self,
        credential_id: uuid.UUID,
    ) -> StudentCredential | None:
        """Get a credential by its own id (Phase 6 R2).

        Used by the token flows, which carry the ``credential_id`` from the
        redeemed token: the reset route loads the credential to live-check
        ``is_active`` (the redemption gate) before rotating the secret.
        """
        stmt = select(StudentCredential).where(StudentCredential.id == credential_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def set_password(
        self,
        student_id: uuid.UUID,
        *,
        password_hash: str,
    ) -> bool:
        """Rotate a credential's ``password_hash`` (Phase 6 R2).

        Returns True if a credential row was updated, False if the student has
        none. Backs both the author-side reset (DD-6-J, by ``student_id``) and
        the self-service reset (the route passes the token credential's
        ``student_id``). Rotating the secret never touches ``is_active`` — a
        reset does not revive revoked access.
        """
        stmt = (
            update(StudentCredential)
            .where(StudentCredential.student_id == student_id)
            .values(password_hash=password_hash)
        )
        result: CursorResult[Any] = await self._session.execute(stmt)  # type: ignore[assignment]
        await self._session.flush()
        return result.rowcount > 0

    async def set_recovery_email(
        self,
        student_id: uuid.UUID,
        *,
        recovery_email: str,
    ) -> StudentCredential | None:
        """Set / change the student-owned recovery email (Phase 6 R2).

        Changing the address resets ``recovery_email_confirmed_at`` to NULL —
        the new address is unverified until confirmed. Returns the credential
        (the route needs its id to issue the email-confirm token), or None if
        the student has no credential.
        """
        credential = await self.get_by_student_id(student_id)
        if credential is None:
            return None
        credential.recovery_email = recovery_email
        credential.recovery_email_confirmed_at = None
        await self._session.flush()
        return credential

    async def confirm_recovery_email(
        self,
        credential_id: uuid.UUID,
    ) -> bool:
        """Mark a credential's recovery email confirmed (Phase 6 R2).

        Called after a valid ``email_confirm`` token is redeemed. Stamps
        ``recovery_email_confirmed_at`` with the DB clock. Returns True if a
        row was updated. Because changing the address burns outstanding confirm
        tokens, a redeemed token always confirms the current address.
        """
        stmt = (
            update(StudentCredential)
            .where(StudentCredential.id == credential_id)
            .values(recovery_email_confirmed_at=func.now())
        )
        result: CursorResult[Any] = await self._session.execute(stmt)  # type: ignore[assignment]
        await self._session.flush()
        return result.rowcount > 0
