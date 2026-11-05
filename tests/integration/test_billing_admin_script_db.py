"""``scripts/billing_admin.py`` end to end on a live database (BIL-A).

The script's ``main`` runs as an operator would run it — its own engine, its
own committed transaction — and the test reads back what the service wrote:
the settings and their audit entry. A refusal writes nothing.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from scripts import billing_admin
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from course_supporter.config import get_settings
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuditEntry,
    BillingAuthorSettings,
    Tenant,
)

pytestmark = pytest.mark.requires_db


@pytest.fixture()
def tenant() -> Iterator[Tenant]:
    engine = create_engine(get_settings().database_url)
    with Session(engine, expire_on_commit=False) as session:
        row = Tenant(name=f"bil-a-script-{uuid.uuid4().hex[:8]}")
        session.add(row)
        session.commit()

    yield row

    with Session(engine) as session:
        account_ids = list(
            session.scalars(
                select(BillingAccount.id).where(BillingAccount.tenant_id == row.id)
            )
        )
        session.execute(
            delete(BillingAuditEntry).where(
                BillingAuditEntry.entity_id.in_(account_ids)
            )
        )
        session.execute(
            delete(BillingAuthorSettings).where(
                BillingAuthorSettings.account_id.in_(account_ids)
            )
        )
        session.execute(
            delete(BillingAccount).where(BillingAccount.id.in_(account_ids))
        )
        session.execute(delete(Tenant).where(Tenant.id == row.id))
        session.commit()
    engine.dispose()


def test_set_benefit_writes_the_settings_and_the_audit(tenant: Tenant) -> None:
    code = billing_admin.main(
        [
            "set-benefit",
            "--tenant",
            tenant.name,
            "--benefit",
            "monthly_limit",
            "--limit",
            "300",
            "--actor",
            "kostiantyn",
        ]
    )
    assert code == 0
    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        account = session.scalars(
            select(BillingAccount).where(BillingAccount.tenant_id == tenant.id)
        ).one()
        settings = session.get(BillingAuthorSettings, account.id)
        assert settings is not None
        assert (settings.benefit, settings.monthly_limit_units) == (
            "monthly_limit",
            300,
        )
        [entry] = session.scalars(
            select(BillingAuditEntry).where(BillingAuditEntry.entity_id == account.id)
        ).all()
        assert (entry.actor, entry.action, entry.before) == (
            "kostiantyn",
            "author_settings.update",
            None,
        )
        assert entry.after is not None and entry.after["monthly_limit_units"] == 300
    engine.dispose()


def test_a_refusal_writes_nothing(
    tenant: Tenant, capsys: pytest.CaptureFixture[str]
) -> None:
    code = billing_admin.main(
        [
            "set-benefit",
            "--tenant",
            tenant.name,
            "--benefit",
            "monthly_limit",
            "--actor",
            "kostiantyn",
        ]
    )
    assert code == 1
    assert "monthly_limit_units > 0" in capsys.readouterr().err
    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        # The author account the refused call created is rolled back too.
        assert (
            session.scalars(
                select(BillingAccount).where(BillingAccount.tenant_id == tenant.id)
            ).first()
            is None
        )
    engine.dispose()
