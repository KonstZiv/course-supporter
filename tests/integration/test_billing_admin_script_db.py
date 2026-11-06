"""``scripts/billing_admin.py`` end to end on a live database (BIL-A, BIL-B).

The script's ``main`` runs as an operator would run it — its own engine, its
own committed transaction — and the test reads back what the service wrote:
the settings and their audit entry. A refusal writes nothing.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from scripts import billing_admin
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from course_supporter.config import get_settings
from course_supporter.storage.orm import (
    BillingAccount,
    BillingAuditEntry,
    BillingAuthorSettings,
    BillingBatch,
    BillingMovement,
    Student,
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
        batch_ids = list(
            session.scalars(
                select(BillingBatch.id).where(BillingBatch.account_id.in_(account_ids))
            )
        )
        session.execute(
            delete(BillingAuditEntry).where(
                BillingAuditEntry.entity_id.in_(account_ids + batch_ids)
            )
        )
        session.execute(
            delete(BillingMovement).where(BillingMovement.account_id.in_(account_ids))
        )
        session.execute(delete(BillingBatch).where(BillingBatch.id.in_(batch_ids)))
        session.execute(
            delete(BillingAuthorSettings).where(
                BillingAuthorSettings.account_id.in_(account_ids)
            )
        )
        session.execute(
            delete(BillingAccount).where(BillingAccount.id.in_(account_ids))
        )
        session.execute(delete(Student).where(Student.tenant_id == row.id))
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


def test_credit_balance_statement(
    tenant: Tenant, capsys: pytest.CaptureFixture[str]
) -> None:
    """A credit to a student's account: a batch, its audit entry, the balance
    and the statement — through the script, in committed transactions."""
    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        student = Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:8]}")
        session.add(student)
        session.commit()
        student_id = student.id

    owner = ["--tenant", tenant.name, "--student", str(student_id)]
    assert billing_admin.main(["balance", *owner]) == 0
    assert capsys.readouterr().out.startswith("no account yet")

    code = billing_admin.main(
        ["credit", *owner, "--units", "250", "--reason", "pilot", "--actor", "k"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "250 units" in out and "available=250" in out

    with Session(engine) as session:
        account = session.scalars(
            select(BillingAccount).where(BillingAccount.student_id == student_id)
        ).one()
        batch = session.scalars(
            select(BillingBatch).where(BillingBatch.account_id == account.id)
        ).one()
        assert (batch.source, batch.units, batch.reason, batch.actor) == (
            "admin_credit",
            250,
            "pilot",
            "k",
        )
        [entry] = session.scalars(
            select(BillingAuditEntry).where(BillingAuditEntry.entity_id == batch.id)
        ).all()
        assert (entry.actor, entry.action) == ("k", "batch.admin_credit")

    assert billing_admin.main(["balance", *owner]) == 0
    assert "balance=250" in capsys.readouterr().out
    assert billing_admin.main(["statement", "--account", str(account.id)]) == 0
    [line] = capsys.readouterr().out.splitlines()
    assert "credit" in line and "250" in line and "actor=k" in line

    # A refusal (no reason) writes nothing.
    code = billing_admin.main(
        ["credit", *owner, "--units", "5", "--reason", " ", "--actor", "k"]
    )
    assert code == 1
    assert "needs a reason" in capsys.readouterr().err
    with Session(engine) as session:
        assert (
            len(
                session.scalars(
                    select(BillingBatch).where(BillingBatch.account_id == account.id)
                ).all()
            )
            == 1
        )
    engine.dispose()


def test_run_periodic_dry_run_then_run(
    tenant: Tenant, capsys: pytest.CaptureFixture[str]
) -> None:
    """``run-periodic`` (BIL-B part 2): a dry run counts and changes nothing;
    the run burns the expired batch, as the scheduler would."""
    engine = create_engine(get_settings().database_url)
    now = datetime.now(UTC)
    with Session(engine) as session:
        account = BillingAccount(kind="author", tenant_id=tenant.id)
        session.add(account)
        session.flush()
        session.add(
            BillingBatch(
                account_id=account.id,
                source="admin_credit",
                units=100,
                created_at=now - timedelta(days=200),
                expires_at=now - timedelta(days=17),
                reason="pilot",
                actor="k",
            )
        )
        session.commit()
        account_id = account.id

    def burned() -> list[int]:
        with Session(engine) as session:
            return list(
                session.scalars(
                    select(BillingMovement.units).where(
                        BillingMovement.account_id == account_id,
                        BillingMovement.kind == "batch_expired",
                    )
                )
            )

    run = ["run-periodic", "--task", "billing_expire"]
    # stdout: the run's log line (periodic_task_run), then the script's answer.
    assert billing_admin.main([*run, "--dry-run"]) == 0
    *_, answer = capsys.readouterr().out.splitlines()
    assert answer.startswith("billing_expire: processed ")
    assert "units=" in answer and answer.endswith("(dry run: nothing changed)")
    assert burned() == []

    assert billing_admin.main(run) == 0
    *_, answer = capsys.readouterr().out.splitlines()
    assert answer.startswith("billing_expire: processed ")
    assert "dry run" not in answer
    assert burned() == [100]
    engine.dispose()
