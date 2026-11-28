"""Remove what a test committed to billing for one tenant.

The migration tests of the billing modules walk the schema down, which some
revisions refuse while rows they cannot represent exist (a test provider's
purchase, a hold by order). A test that commits billing rows removes them.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuditEntry,
    BillingAuthorSettings,
    BillingBatch,
    BillingBatchProviderFigures,
    BillingConsent,
    BillingDifference,
    BillingMovement,
    BillingPaymentRule,
    BillingRefund,
    BillingReservation,
    BillingTestBatch,
)


async def drop_billing_of_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    also_accounts: Sequence[uuid.UUID] = (),
) -> None:
    """Delete every billing row of the tenant's accounts, then the accounts.

    ``also_accounts`` — accounts the tenant no longer names: an anonymized
    account has lost its ``tenant_id``, so the test that anonymized it passes
    the ids it kept.
    """
    async with session_factory() as session:
        accounts = list(
            (
                await session.execute(
                    select(BillingAccount.id).where(
                        BillingAccount.tenant_id == tenant_id
                    )
                )
            ).scalars()
        )
        accounts += [a for a in also_accounts if a not in accounts]
        if not accounts:
            return
        rules = select(BillingPaymentRule.id).where(
            BillingPaymentRule.tenant_id == tenant_id
        )
        refunds = select(BillingRefund.id).where(BillingRefund.account_id.in_(accounts))
        batches = select(BillingBatch.id).where(BillingBatch.account_id.in_(accounts))
        await session.execute(
            delete(BillingAuditEntry).where(
                BillingAuditEntry.entity_id.in_(accounts)
                | BillingAuditEntry.entity_id.in_(rules)
                | BillingAuditEntry.entity_id.in_(refunds)
                | BillingAuditEntry.entity_id.in_(batches)
            )
        )
        await session.execute(
            delete(BillingTestBatch).where(BillingTestBatch.batch_id.in_(batches))
        )
        await session.execute(
            delete(BillingBatchProviderFigures).where(
                BillingBatchProviderFigures.batch_id.in_(batches)
            )
        )
        for table in (
            BillingMovement,
            BillingDifference,
            BillingReservation,
            BillingRefund,
            BillingBatch,
            BillingAuthorSettings,
            BillingConsent,
        ):
            await session.execute(delete(table).where(table.account_id.in_(accounts)))
        await session.execute(
            delete(BillingPaymentRule).where(BillingPaymentRule.tenant_id == tenant_id)
        )
        await session.execute(
            delete(BillingAccount).where(BillingAccount.id.in_(accounts))
        )
        await session.commit()
