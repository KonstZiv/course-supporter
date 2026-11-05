"""``scripts/billing_admin.py``: each command calls its service function (BIL-A).

The services are replaced by mocks; what is checked is the mapping from the
command line to the call, the required ``--actor`` on every change, and the
exit status of a refusal. The script against a live database is
``tests/integration/test_billing_admin_script_db.py``.
"""

from __future__ import annotations

import argparse
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from scripts import billing_admin

from course_supporter.billing.admin import AuthorSettingsView
from course_supporter.billing.errors import BillingConflictError
from course_supporter.billing.kinds import ActionKind, Benefit, RuleScope

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
    ],
)
def test_every_change_needs_an_actor(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        _args(*argv)
    assert exc.value.code == 2


def test_every_command_has_a_handler() -> None:
    parser = billing_admin.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) == set(billing_admin.COMMANDS)


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
    owner = uuid.uuid4()
    with (
        patch.object(
            billing_admin.accounts,
            "anonymize_student_accounts",
            AsyncMock(return_value=[owner]),
        ) as student,
        patch.object(
            billing_admin.accounts,
            "anonymize_tenant_accounts",
            AsyncMock(return_value=[]),
        ) as tenant,
    ):
        out = await _run("anonymize-student", "--student", str(owner), "--actor", "k")
        await _run("anonymize-tenant", "--tenant", str(owner), "--actor", "k")
    assert student.await_args.args[1] == owner
    assert tenant.await_args.kwargs == {"actor": "k"}
    assert out == f"anonymized 1 account(s): {owner}"


def test_a_refusal_exits_with_1(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(
        billing_admin, "run", AsyncMock(side_effect=BillingConflictError("no"))
    ):
        code = billing_admin.main(["end-rule", "--rule", str(TENANT), "--actor", "k"])
    assert code == 1
    assert capsys.readouterr().err == "refused: no\n"
