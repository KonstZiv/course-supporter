"""The public numbers of a test's own world (``billing.public_numbers``).

The test base is shared, so a test computes the numbers of its own tenants
only. The routes read them through ``current_numbers``; a test overrides it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.billing.public_numbers import (
    PublicNumbers,
    Published,
    compute_public_numbers,
    dump,
    published,
)

WINDOW_DAYS = 90


async def numbers_of(
    session_factory: async_sessionmaker[AsyncSession], *tenant_ids: uuid.UUID
) -> PublicNumbers:
    """The numbers of these tenants, now."""
    async with session_factory() as session:
        return await compute_public_numbers(
            session, window_days=WINDOW_DAYS, tenant_ids=tenant_ids
        )


def serve_numbers_of(
    session_factory: async_sessionmaker[AsyncSession], *tenant_ids: uuid.UUID
) -> Callable[[], Awaitable[Published]]:
    """An override of ``current_numbers``: the numbers of these tenants,
    computed again on every request (the test's world changes between them)."""

    async def current() -> Published:
        return published(dump(await numbers_of(session_factory, *tenant_ids)))

    return current
