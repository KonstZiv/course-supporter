"""``scripts/billing_admin.py``: each command calls its service function (BIL-A, BIL-B).

The services are replaced by mocks; what is checked is the mapping from the
command line to the call, the required ``--actor`` on every change, and the
exit status of a refusal. The script against a live database is
``tests/integration/test_billing_admin_script_db.py``.
"""

from __future__ import annotations

import argparse
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from scripts import billing_admin

from course_supporter.billing.accounts import Anonymization
from course_supporter.billing.admin import AuthorSettingsView
from course_supporter.billing.errors import BillingConflictError
from course_supporter.billing.kinds import ActionKind, Benefit, RuleScope
from course_supporter.billing.ledger import Balance
from course_supporter.periodic.runner import TaskRun

TENANT = uuid.uuid4()
VIEW = AuthorSettingsView(
    account_id=uuid.uuid4(),
    benefit=Benefit.MONTHLY_LIMIT,
    monthly_limit_units=500,
    benefit_started_at=None,
    credit_allowed=False,
    updated_at=None,
)


def _args(*argv: str) -> argparse.Namespace:
    return billing_admin.build_parser().parse_args(list(argv))


async def _run(*argv: str) -> str:
    args = _args(*argv)
    return await billing_admin.COMMANDS[args.command](MagicMock(), args)


@pytest.mark.parametrize(
    "argv",
    [
        ["set-benefit", "--tenant", "t", "--benefit", "unlimited"],
        ["set-credit", "--tenant", "t", "--allowed", "yes"],
        ["create-rule", "--tenant", "t", "--scope", "course", "--action", "review"],
        ["end-rule", "--rule", str(TENANT)],
        ["anonymize-student", "--student", str(TENANT)],
        ["anonymize-tenant", "--tenant", str(TENANT)],
        ["credit", "--tenant", "t", "--units", "5", "--reason", "pilot"],
        [
            "credit-invoice",
            "--tenant",
            "t",
            "--units",
            "5",
            "--amount-minor",
            "100",
            "--currency",
            "EUR",
            "--invoice",
            "INV-1",
        ],
    ],
)
def test_every_change_needs_an_actor(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        _args(*argv)
    assert exc.value.code == 2


def test_every_command_has_a_handler() -> None:
    parser = billing_admin.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert not set(billing_admin.COMMANDS) & set(billing_admin.SELF_TRANSACTED)
    assert set(sub.choices) == set(billing_admin.COMMANDS) | set(
        billing_admin.SELF_TRANSACTED
    )


async def test_set_benefit_calls_the_service() -> None:
    with patch.object(
        billing_admin.admin, "update_author_settings", AsyncMock(return_value=VIEW)
    ) as service:
        out = await _run(
            "set-benefit",
            "--tenant",
            str(TENANT),
            "--benefit",
            "monthly_limit",
            "--limit",
            "500",
            "--actor",
            "kostiantyn",
        )
    service.assert_awaited_once()
    _, tenant_id = service.await_args.args
    assert tenant_id == TENANT
    assert service.await_args.kwargs == {
        "actor": "kostiantyn",
        "benefit": Benefit.MONTHLY_LIMIT,
        "monthly_limit_units": 500,
    }
    assert "500 units/month" in out


async def test_set_credit_calls_the_service() -> None:
    with patch.object(
        billing_admin.admin, "update_author_settings", AsyncMock(return_value=VIEW)
    ) as service:
        await _run(
            "set-credit", "--tenant", str(TENANT), "--allowed", "no", "--actor", "k"
        )
    assert service.await_args.kwargs == {"actor": "k", "credit_allowed": False}


async def test_create_rule_calls_the_service() -> None:
    course = uuid.uuid4()
    rule = MagicMock(scope="course", course_node_id=course, active=True)
    with patch.object(
        billing_admin.admin, "create_payment_rule", AsyncMock(return_value=rule)
    ) as service:
        out = await _run(
            "create-rule",
            "--tenant",
            str(TENANT),
            "--scope",
            "course",
            "--course",
            str(course),
            "--action",
            "review",
            "--actor",
            "k",
        )
    assert service.await_args.kwargs == {
        "actor": "k",
        "tenant_id": TENANT,
        "scope": RuleScope.COURSE,
        "action_kind": ActionKind.REVIEW,
        "course_node_id": course,
        "student_id": None,
        "payer_account_id": None,
    }
    assert out.startswith("created rule")


async def test_anonymize_commands_call_the_service() -> None:
    owner, rule = uuid.uuid4(), uuid.uuid4()
    with (
        patch.object(
            billing_admin.accounts,
            "anonymize_student_accounts",
            AsyncMock(return_value=Anonymization([owner], [rule])),
        ) as student,
        patch.object(
            billing_admin.accounts,
            "anonymize_tenant_accounts",
            AsyncMock(return_value=Anonymization([], [])),
        ) as tenant,
    ):
        out = await _run("anonymize-student", "--student", str(owner), "--actor", "k")
        again = await _run("anonymize-tenant", "--tenant", str(owner), "--actor", "k")
    assert student.await_args.args[1] == owner
    assert tenant.await_args.kwargs == {"actor": "k"}
    assert out == f"anonymized 1 account(s): {owner}\nended 1 rule(s): {rule}"
    assert again == "anonymized 0 account(s): -\nended 0 rule(s): -"


def test_a_refusal_exits_with_1(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(
        billing_admin, "run", AsyncMock(side_effect=BillingConflictError("no"))
    ):
        code = billing_admin.main(["end-rule", "--rule", str(TENANT), "--actor", "k"])
    assert code == 1
    assert capsys.readouterr().err == "refused: no\n"


BALANCE = Balance(
    balance=500,
    reserved=0,
    held=0,
    available=500,
    difference=0,
    next_expiry=datetime(2027, 5, 5, tzinfo=UTC),
)


def test_a_credit_needs_a_reason() -> None:
    with pytest.raises(SystemExit) as exc:
        _args("credit", "--tenant", "t", "--units", "5", "--actor", "k")
    assert exc.value.code == 2


async def test_credit_calls_the_services() -> None:
    student, account = uuid.uuid4(), MagicMock(id=uuid.uuid4())
    batch = MagicMock(
        id=uuid.uuid4(), units=100, expires_at=datetime(2027, 5, 5, tzinfo=UTC)
    )
    with (
        patch.object(
            billing_admin.admin, "owner_account", AsyncMock(return_value=account)
        ) as owner,
        patch.object(
            billing_admin.ledger,
            "credit_admin",
            AsyncMock(return_value=MagicMock(batch=batch, repaid_units=30)),
        ) as service,
        patch.object(billing_admin.ledger, "balance", AsyncMock(return_value=BALANCE)),
    ):
        out = await _run(
            "credit",
            "--tenant",
            str(TENANT),
            "--student",
            str(student),
            "--units",
            "100",
            "--reason",
            "a review lost to an outage",
            "--actor",
            "k",
        )
    assert owner.await_args.kwargs == {
        "tenant_id": TENANT,
        "student_id": student,
        "create": True,
    }
    assert service.await_args.kwargs == {
        "account_id": account.id,
        "units": 100,
        "reason": "a review lost to an outage",
        "actor": "k",
    }
    assert "100 units" in out and "repaid_difference=30" in out
    assert "available=500" in out


@pytest.mark.parametrize("missing", ["--amount-minor", "--currency", "--invoice"])
def test_an_invoice_names_its_amount_currency_and_number(missing: str) -> None:
    argv = {
        "--tenant": "t",
        "--units": "5",
        "--amount-minor": "100",
        "--currency": "EUR",
        "--invoice": "INV-1",
        "--actor": "k",
    }
    del argv[missing]
    with pytest.raises(SystemExit) as exc:
        _args("credit-invoice", *(x for pair in argv.items() for x in pair))
    assert exc.value.code == 2


@pytest.mark.parametrize("created", [True, False])
async def test_credit_invoice_calls_the_service(created: bool) -> None:
    account = uuid.uuid4()
    batch = MagicMock(
        id=uuid.uuid4(),
        account_id=account,
        units=10000,
        amount_paid_minor=50000,
        currency="EUR",
        provider_transaction_id="INV-2026-0042",
        expires_at=datetime(2027, 5, 5, tzinfo=UTC),
    )
    with (
        patch.object(
            billing_admin.invoice,
            "credit_invoice",
            AsyncMock(
                return_value=MagicMock(batch=batch, repaid_units=0, created=created)
            ),
        ) as service,
        patch.object(billing_admin.ledger, "balance", AsyncMock(return_value=BALANCE)),
    ):
        out = await _run(
            "credit-invoice",
            "--tenant",
            str(TENANT),
            "--units",
            "10000",
            "--amount-minor",
            "50000",
            "--currency",
            "EUR",
            "--invoice",
            "INV-2026-0042",
            "--actor",
            "k",
        )
    assert service.await_args.kwargs == {
        "tenant_id": TENANT,
        "units": 10000,
        "amount_minor": 50000,
        "currency": "EUR",
        "invoice": "INV-2026-0042",
        "actor": "k",
    }
    if created:
        assert "credited invoice INV-2026-0042" in out
        assert "10000 units" in out and "amount=50000 EUR" in out
    else:
        assert "was credited before" in out and "nothing changed" in out
    assert f"account {account}" in out and "available=500" in out


async def test_balance_of_an_owner_without_an_account() -> None:
    with patch.object(
        billing_admin.admin, "owner_account", AsyncMock(return_value=None)
    ) as owner:
        out = await _run("balance", "--tenant", str(TENANT))
    assert owner.await_args.kwargs == {
        "tenant_id": TENANT,
        "student_id": None,
        "create": False,
    }
    assert out.startswith("no account yet")


async def test_statement_reads_a_period_in_utc() -> None:
    account = uuid.uuid4()
    args = _args(
        "statement",
        "--account",
        str(account),
        "--from",
        "2026-11-01",
        "--to",
        "2026-12-01T00:00:00+02:00",
    )
    session = MagicMock(get=AsyncMock(return_value=object()))
    with patch.object(
        billing_admin.ledger, "statement", AsyncMock(return_value=[])
    ) as service:
        out = await billing_admin.COMMANDS["statement"](session, args)
    assert service.await_args.args[1] == account
    since, until = (
        service.await_args.kwargs["since"],
        service.await_args.kwargs["until"],
    )
    assert since == datetime(2026, 11, 1, tzinfo=UTC)
    assert until == datetime(2026, 11, 30, 22, 0, tzinfo=UTC)
    assert out == "no movements"


def test_statement_refuses_a_bad_date() -> None:
    with pytest.raises(SystemExit):
        _args("statement", "--account", str(TENANT), "--from", "yesterday")


# ── run-periodic (BIL-B part 2) ────────────────────────────────────────


def _periodic_run(**overrides: object) -> TaskRun:
    fields: dict[str, object] = {
        "task": "billing_purge",
        "processed": 12,
        "details": {"accounts": 1, "blocked": 0},
        "duration_ms": 7,
        "dry_run": True,
    }
    fields.update(overrides)
    return TaskRun(**fields)  # type: ignore[arg-type]


def test_run_periodic_names_one_of_the_four_tasks() -> None:
    for name in (
        "billing_expire",
        "billing_annul",
        "billing_stale_reserves",
        "billing_purge",
    ):
        args = _args("run-periodic", "--task", name)
        assert (args.task, args.dry_run) == (name, False)
    assert _args("run-periodic", "--task", "billing_purge", "--dry-run").dry_run
    with pytest.raises(SystemExit) as exc:
        _args("run-periodic", "--task", "s3_cleanup")
    assert exc.value.code == 2


async def test_run_periodic_runs_the_task_as_the_scheduler_would() -> None:
    with (
        patch.object(billing_admin, "create_async_engine") as engine,
        patch.object(
            billing_admin, "run_task", AsyncMock(return_value=_periodic_run())
        ) as run_task,
    ):
        engine.return_value.dispose = AsyncMock()
        out = await billing_admin.run(
            _args("run-periodic", "--task", "billing_purge", "--dry-run")
        )
    task = run_task.await_args.args[0]
    assert task.name == "billing_purge"
    assert run_task.await_args.kwargs["dry_run"] is True
    assert out == (
        "billing_purge: processed 12, accounts=1, blocked=0 in 7 ms "
        "(dry run: nothing changed)"
    )
    engine.return_value.dispose.assert_awaited_once()


@pytest.mark.parametrize(
    ("run", "message"),
    [
        (
            _periodic_run(skipped="busy"),
            "billing_purge: another run holds its lock; try later\n",
        ),
        (
            _periodic_run(error="RuntimeError: boom"),
            "billing_purge failed: RuntimeError: boom\n",
        ),
    ],
)
def test_a_busy_or_failed_periodic_run_exits_with_1(
    run: TaskRun, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch.object(billing_admin, "create_async_engine") as engine,
        patch.object(billing_admin, "run_task", AsyncMock(return_value=run)),
    ):
        engine.return_value.dispose = AsyncMock()
        code = billing_admin.main(["run-periodic", "--task", "billing_purge"])
    assert code == 1
    assert capsys.readouterr().err == message
