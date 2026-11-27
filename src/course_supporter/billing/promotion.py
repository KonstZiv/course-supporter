"""The promotion path of a tenant (Б-4): which way its students come.

``tenants.promotion_path`` — the platform's own courses, a partner school (a
legal person) or an author who is a natural person; NULL = not set. The
administrator sets it (``scripts/billing_admin.py set-promotion-path``); the
funnel (:mod:`course_supporter.billing.funnel`) groups by it. The same value
again writes nothing. A change is a structured log line naming the actor
(``tenant_promotion_path_set``), not a billing audit entry: the path is on
the tenant, not on a billing account, like the test-student mark.

The function flushes and never commits: the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.billing.audit import require_actor
from course_supporter.billing.errors import BillingNotFoundError
from course_supporter.storage.orm import Tenant

logger = structlog.get_logger(__name__)


class PromotionPath(StrEnum):
    """The values ``ck_tenants_promotion_path`` allows; a new one is a migration."""

    OWN_COURSE = "own_course"
    """The platform's own courses."""
    SCHOOL = "school"
    """A partner school — a legal person."""
    AUTHOR = "author"
    """An author who is a natural person."""


@dataclass(frozen=True, slots=True)
class PathChange:
    """The path before and after; ``changed`` is False for the same value."""

    tenant_id: uuid.UUID
    before: PromotionPath | None
    after: PromotionPath | None

    @property
    def changed(self) -> bool:
        return self.before != self.after


async def set_promotion_path(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    path: PromotionPath | None,
    actor: str,
) -> PathChange:
    """Set (or clear, ``None``) the tenant's path.

    Raises:
        BillingInvalidError: ``actor`` is empty.
        BillingNotFoundError: no such tenant, or it is deleted.
    """
    actor = require_actor(actor)
    tenant = await session.get(Tenant, tenant_id, with_for_update=True)
    if tenant is None or tenant.deleted_at is not None:
        raise BillingNotFoundError(f"tenant {tenant_id} not found")
    before = PromotionPath(tenant.promotion_path) if tenant.promotion_path else None
    change = PathChange(tenant_id=tenant.id, before=before, after=path)
    if not change.changed:
        return change
    tenant.promotion_path = path.value if path is not None else None
    await session.flush()
    logger.info(
        "tenant_promotion_path_set",
        actor=actor,
        tenant_id=str(tenant.id),
        before=before.value if before else None,
        after=path.value if path else None,
    )
    return change
