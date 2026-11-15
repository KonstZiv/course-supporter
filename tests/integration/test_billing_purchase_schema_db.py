"""The schema of the purchase screens on a live database (BIL-PAY).

``billing_accounts.test_purchase_allowed`` and ``billing_checkouts``: the
comments are the ORM's, a checkout takes one change only — its batch, once —
and the migration goes down and up, refusing while a mark or a checkout
exists.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, Table, create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from course_supporter.config import get_settings
from course_supporter.storage.orm import BillingAccount, BillingCheckout

pytestmark = pytest.mark.requires_db

REVISION = "bilpay_purchase_screens"
CHANGES = {
    name: f"UPDATE billing_checkouts SET batch_id = :b, {change} WHERE id = :c"  # noqa: S608
    for name, change in (
        ("package", "package_id = 'other'"),
        ("provider", "provider = 'creem'"),
        ("provider's id", "provider_checkout_id = 'x'"),
        ("time", "created_at = now() - interval '1 day'"),
    )
}
"""Linking a batch while changing something else (constants, no input)."""
BELOW = "bilpay_creem_disputes"


@pytest.fixture()
def engine() -> Iterator[Engine]:
    made = create_engine(get_settings().database_url)
    yield made
    made.dispose()


@pytest.fixture()
def account(engine: Engine) -> Iterator[uuid.UUID]:
    """An author account with one purchase batch; removed afterwards."""
    account_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO billing_accounts (id, kind, tenant_id) "
                "VALUES (:a, 'author', :t)"
            ),
            {"a": account_id, "t": uuid.uuid4()},
        )
    yield account_id
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM billing_checkouts WHERE account_id = :a"),
            {"a": account_id},
        )
        conn.execute(
            text("DELETE FROM billing_batches WHERE account_id = :a"),
            {"a": account_id},
        )
        conn.execute(
            text("DELETE FROM billing_accounts WHERE id = :a"), {"a": account_id}
        )


def _batch(engine: Engine, account_id: uuid.UUID) -> uuid.UUID:
    batch_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO billing_batches (id, account_id, source, units, "
                "expires_at, amount_paid_minor, currency, provider, "
                "provider_transaction_id, provider_line_id) VALUES (:b, :a, "
                "'purchase', 10, now() + interval '6 months', 500, 'EUR', "
                "'fake', :n, :n)"
            ),
            {"b": batch_id, "a": account_id, "n": f"txn_{batch_id.hex[:12]}"},
        )
    return batch_id


def _checkout(engine: Engine, account_id: uuid.UUID) -> uuid.UUID:
    checkout_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO billing_checkouts (id, account_id, package_id, "
                "provider, provider_checkout_id) VALUES (:c, :a, 'starter', "
                "'fake', 'fake_chk_1')"
            ),
            {"c": checkout_id, "a": account_id},
        )
    return checkout_id


def _comments(engine: Engine, table: str) -> tuple[dict[str, str | None], str | None]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.attname, col_description(a.attrelid, a.attnum) FROM "
                "pg_attribute a WHERE a.attrelid = CAST(:t AS regclass) "
                "AND a.attnum > 0 AND NOT a.attisdropped"
            ),
            {"t": table},
        ).all()
        comment = conn.scalar(
            text("SELECT obj_description(CAST(:t AS regclass))"), {"t": table}
        )
    return {name: value for name, value in rows}, comment


@pytest.mark.parametrize("model", [BillingAccount, BillingCheckout])
def test_comments_are_the_orms(engine: Engine, model: type) -> None:
    orm: Table = model.__table__  # type: ignore[attr-defined]
    columns, comment = _comments(engine, orm.name)
    assert columns == {c.name: c.comment for c in orm.c}
    assert comment == orm.comment


def test_an_account_is_not_marked_by_default(
    engine: Engine, account: uuid.UUID
) -> None:
    with engine.connect() as conn:
        marked = conn.scalar(
            text("SELECT test_purchase_allowed FROM billing_accounts WHERE id = :a"),
            {"a": account},
        )
    assert marked is False


class TestCheckoutLink:
    def test_the_batch_is_set_once(self, engine: Engine, account: uuid.UUID) -> None:
        checkout = _checkout(engine, account)
        first, second = _batch(engine, account), _batch(engine, account)
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE billing_checkouts SET batch_id = :b WHERE id = :c"),
                {"b": first, "c": checkout},
            )
        with (
            pytest.raises(DBAPIError, match="only batch_id may be set, and once"),
            engine.begin() as conn,
        ):
            conn.execute(
                text("UPDATE billing_checkouts SET batch_id = :b WHERE id = :c"),
                {"b": second, "c": checkout},
            )

    @pytest.mark.parametrize("change", sorted(CHANGES))
    def test_nothing_else_moves(
        self, engine: Engine, account: uuid.UUID, change: str
    ) -> None:
        checkout = _checkout(engine, account)
        batch = _batch(engine, account)
        with (
            pytest.raises(DBAPIError, match="only batch_id may be set"),
            engine.begin() as conn,
        ):
            conn.execute(text(CHANGES[change]), {"b": batch, "c": checkout})

    def test_one_batch_answers_one_checkout(
        self, engine: Engine, account: uuid.UUID
    ) -> None:
        one, other = _checkout(engine, account), _checkout(engine, account)
        batch = _batch(engine, account)
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE billing_checkouts SET batch_id = :b WHERE id = :c"),
                {"b": batch, "c": one},
            )
        with (
            pytest.raises(DBAPIError, match="uq_billing_checkouts_batch"),
            engine.begin() as conn,
        ):
            conn.execute(
                text("UPDATE billing_checkouts SET batch_id = :b WHERE id = :c"),
                {"b": batch, "c": other},
            )


def test_the_migration_goes_down_and_up(engine: Engine) -> None:
    cfg = Config("alembic.ini")
    head = ScriptDirectory.from_config(cfg).get_current_head()
    try:
        command.downgrade(cfg, BELOW)
        with engine.connect() as conn:
            assert "billing_checkouts" not in inspect(conn).get_table_names()
            columns = {c["name"] for c in inspect(conn).get_columns("billing_accounts")}
            assert "test_purchase_allowed" not in columns
            assert (
                conn.scalar(
                    text("SELECT count(*) FROM pg_proc WHERE proname = :f"),
                    {"f": "billing_checkouts_link_once"},
                )
                == 0
            )
    finally:
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == head
        assert "billing_checkouts" in inspect(conn).get_table_names()


@pytest.mark.parametrize("what", ["a mark", "a checkout"])
def test_the_downgrade_is_refused_while_a_mark_or_a_checkout_exists(
    engine: Engine, account: uuid.UUID, what: str
) -> None:
    if what == "a mark":
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE billing_accounts SET test_purchase_allowed = true "
                    "WHERE id = :a"
                ),
                {"a": account},
            )
    else:
        _checkout(engine, account)
    cfg = Config("alembic.ini")
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with pytest.raises(RuntimeError, match="cannot hold them"):
        command.downgrade(cfg, BELOW)
    with engine.connect() as conn:
        current = conn.scalar(text("SELECT version_num FROM alembic_version"))
    assert current == head  # nothing went down


def test_the_revision_follows_the_disputes() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    revision = script.get_revision(REVISION)
    assert revision is not None
    assert revision.down_revision == BELOW
    assert len(script.get_heads()) == 1
