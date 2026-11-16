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
from typing import Any

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
        created, entry = session.scalars(
            select(BillingAuditEntry)
            .where(BillingAuditEntry.entity_id == account.id)
            .order_by(BillingAuditEntry.created_at, BillingAuditEntry.id)
        ).all()
        # BIL-E3: the change created the account first, audited by its --actor.
        assert (created.actor, created.action, created.before) == (
            "kostiantyn",
            "account.create",
            None,
        )
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


def test_credit_invoice_is_a_purchase_audited_once(
    tenant: Tenant, capsys: pytest.CaptureFixture[str]
) -> None:
    """``credit-invoice`` (BIL-PAY, B2B): a purchase of provider "invoice" on
    the tenant's author account, keyed by the invoice's number; the same
    number again changes nothing, with other figures it is refused."""
    number = f"INV-{uuid.uuid4().hex[:10]}"
    argv = [
        "credit-invoice",
        "--tenant",
        tenant.name,
        "--units",
        "10000",
        "--amount-minor",
        "50000",
        "--currency",
        "EUR",
        "--invoice",
        number,
        "--actor",
        "kostiantyn",
    ]
    assert billing_admin.main(argv) == 0
    out = capsys.readouterr().out
    assert f"credited invoice {number}" in out and "available=10000" in out

    engine = create_engine(get_settings().database_url)

    def written() -> tuple[BillingBatch, list[BillingAuditEntry], list[str]]:
        with Session(engine) as session:
            account = session.scalars(
                select(BillingAccount).where(BillingAccount.tenant_id == tenant.id)
            ).one()
            assert account.kind == "author"
            [batch] = session.scalars(
                select(BillingBatch).where(BillingBatch.account_id == account.id)
            ).all()
            entries = list(
                session.scalars(
                    select(BillingAuditEntry).where(
                        BillingAuditEntry.entity_id == batch.id
                    )
                )
            )
            kinds = list(
                session.scalars(
                    select(BillingMovement.kind).where(
                        BillingMovement.account_id == account.id
                    )
                )
            )
            return batch, entries, kinds

    batch, [entry], kinds = written()
    assert (batch.source, batch.provider, batch.units) == ("purchase", "invoice", 10000)
    assert (batch.provider_transaction_id, batch.provider_line_id) == (number, number)
    assert (batch.amount_paid_minor, batch.currency) == (50000, "EUR")
    assert (batch.reason, batch.actor) == (None, None)  # a purchase's row
    assert (entry.actor, entry.action, entry.entity) == (
        "kostiantyn",
        "batch.invoice_credit",
        "batch",
    )
    assert entry.after is not None
    assert (entry.after["provider"], entry.after["provider_line_id"]) == (
        "invoice",
        number,
    )
    assert entry.after["amount_paid_minor"] == 50000
    assert kinds == ["credit"]

    assert billing_admin.main(argv) == 0  # the same invoice again
    assert "nothing changed" in capsys.readouterr().out
    again, entries, kinds = written()
    assert (again.id, len(entries), kinds) == (batch.id, 1, ["credit"])

    other = [*argv]
    other[other.index("--units") + 1] = "20000"
    assert billing_admin.main(other) == 1
    assert "other figures" in capsys.readouterr().err
    assert written()[0].id == batch.id

    missing = [*argv]
    missing[missing.index("--tenant") + 1] = f"no-such-{uuid.uuid4().hex[:6]}"
    assert billing_admin.main(missing) == 1
    assert "not found" in capsys.readouterr().err
    engine.dispose()


# ── test-purchase (BIL-PAY, operator 2026-11-15) ───────────────────────


Entry = tuple[str, dict[str, Any], dict[str, Any]]


def _marks(tenant: Tenant) -> list[tuple[str, bool, list[Entry]]]:
    """Each account of the tenant: its kind, its mark, its mark's audit."""
    engine = create_engine(get_settings().database_url)
    with Session(engine) as session:
        accounts = session.scalars(
            select(BillingAccount)
            .where(BillingAccount.tenant_id == tenant.id)
            .order_by(BillingAccount.kind)
        ).all()
        out = []
        for account in accounts:
            entries = session.scalars(
                select(BillingAuditEntry)
                .where(
                    BillingAuditEntry.entity_id == account.id,
                    BillingAuditEntry.action == "account.test_purchase",
                )
                .order_by(BillingAuditEntry.created_at, BillingAuditEntry.id)
            ).all()
            out.append(
                (
                    account.kind,
                    account.test_purchase_allowed,
                    [(e.actor, e.before or {}, e.after or {}) for e in entries],
                )
            )
    engine.dispose()
    return out


class TestTestPurchase:
    def test_allow_creates_the_author_account_marked_and_audits(
        self, tenant: Tenant
    ) -> None:
        argv = ["test-purchase", "--tenant", tenant.name, "--allow"]
        assert billing_admin.main([*argv, "--actor", "kostiantyn"]) == 0
        [(kind, marked, entries)] = _marks(tenant)
        assert (kind, marked) == ("author", True)
        [(actor, before, after)] = entries
        assert actor == "kostiantyn"
        assert before["test_purchase_allowed"] is False
        assert after["test_purchase_allowed"] is True
        assert "tenant_id" not in after  # the owner never enters the log

    def test_the_same_mark_again_writes_nothing_and_deny_clears_it(
        self, tenant: Tenant
    ) -> None:
        for flag in ("--allow", "--allow", "--deny", "--deny"):
            argv = ["test-purchase", "--tenant", str(tenant.id), flag]
            assert billing_admin.main([*argv, "--actor", "a"]) == 0
        [(_, marked, entries)] = _marks(tenant)
        assert marked is False
        assert [after["test_purchase_allowed"] for _, _, after in entries] == [
            True,
            False,
        ]

    def test_a_students_account_is_marked_apart_from_the_authors(
        self, tenant: Tenant
    ) -> None:
        engine = create_engine(get_settings().database_url)
        with Session(engine, expire_on_commit=False) as session:
            student = Student(tenant_id=tenant.id, external_id="reviewer-demo")
            session.add(student)
            session.commit()
        engine.dispose()
        argv = ["test-purchase", "--tenant", tenant.name]
        argv += ["--student", str(student.id), "--allow", "--actor", "a"]
        assert billing_admin.main(argv) == 0
        [(kind, marked, entries)] = _marks(tenant)
        assert (kind, marked, len(entries)) == ("student", True, 1)

    def test_without_an_actor_or_a_choice_it_is_a_usage_error(
        self, tenant: Tenant
    ) -> None:
        with pytest.raises(SystemExit) as no_actor:
            billing_admin.main(["test-purchase", "--tenant", tenant.name, "--allow"])
        with pytest.raises(SystemExit) as no_choice:
            billing_admin.main(
                ["test-purchase", "--tenant", tenant.name, "--actor", "a"]
            )
        with pytest.raises(SystemExit) as both:
            billing_admin.main(
                [
                    *("test-purchase", "--tenant", tenant.name),
                    *("--allow", "--deny", "--actor", "a"),
                ]
            )
        assert no_actor.value.code == no_choice.value.code == both.value.code == 2
        assert _marks(tenant) == []

    def test_an_unknown_student_is_refused_and_nothing_is_written(
        self, tenant: Tenant
    ) -> None:
        argv = ["test-purchase", "--tenant", tenant.name]
        argv += ["--student", str(uuid.uuid4()), "--allow", "--actor", "a"]
        assert billing_admin.main(argv) == 1
        assert _marks(tenant) == []
