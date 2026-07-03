"""Repository for StudentCredentialToken operations (Phase 6 R2)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.orm import StudentCredentialToken


class StudentCredentialTokenRepository:
    """Repository for single-use recovery tokens.

    Two flows share the table, keyed by ``purpose`` (``password_reset`` /
    ``email_confirm``). Only the SHA-256 hash of the raw token is stored;
    redemption looks up by hash. Single-use and the burn-unused-siblings rule
    are enforced here with the DB clock (``func.now()``).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def issue(
        self,
        *,
        credential_id: uuid.UUID,
        purpose: str,
        token_hash: str,
        expires_at: datetime,
    ) -> StudentCredentialToken:
        """Issue a token, first burning any unused sibling of the same purpose.

        Burning (stamping ``used_at`` on outstanding same-purpose tokens for the
        credential) guarantees only the latest link works — re-requesting a
        reset / re-confirming a changed address invalidates the previous mail.
        """
        await self._session.execute(
            update(StudentCredentialToken)
            .where(
                StudentCredentialToken.credential_id == credential_id,
                StudentCredentialToken.purpose == purpose,
                StudentCredentialToken.used_at.is_(None),
            )
            .values(used_at=func.now())
        )
        token = StudentCredentialToken(
            credential_id=credential_id,
            purpose=purpose,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self._session.add(token)
        await self._session.flush()
        return token

    async def redeem(
        self,
        *,
        token_hash: str,
        purpose: str,
    ) -> uuid.UUID | None:
        """Atomically consume a valid token; return its ``credential_id``.

        The single UPDATE stamps ``used_at`` only where the token is unused and
        unexpired, so single-use is DB-enforced even under concurrency. Returns
        the owning ``credential_id`` on success, or None when the token is
        unknown / already used / expired / of the wrong purpose. The caller
        still live-checks ``credential.is_active`` (the redemption gate) — a
        revoked credential is rejected even with a valid token.
        """
        stmt = (
            update(StudentCredentialToken)
            .where(
                StudentCredentialToken.token_hash == token_hash,
                StudentCredentialToken.purpose == purpose,
                StudentCredentialToken.used_at.is_(None),
                StudentCredentialToken.expires_at > func.now(),
            )
            .values(used_at=func.now())
            .returning(StudentCredentialToken.credential_id)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one_or_none()

    async def get_by_hash(
        self,
        token_hash: str,
    ) -> StudentCredentialToken | None:
        """Get a token by its hash (introspection / tests)."""
        stmt = select(StudentCredentialToken).where(
            StudentCredentialToken.token_hash == token_hash,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()
