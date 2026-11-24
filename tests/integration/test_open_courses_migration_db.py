"""Lock (а) of O-1: the migration ``o1_open_enrollment`` up, down and up again.

Rows already there before the upgrade keep working: an enrollment reads source
'author', a course has no key (closed), a student has no adult mark. Downgrade
removes exactly what the upgrade added.

Requires ``docker compose up -d`` (PostgreSQL), migrated to head; run with
``--run-db``.
"""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from course_supporter.config import get_settings

pytestmark = pytest.mark.requires_db

REVISION = "o1_open_enrollment"
BELOW = "biltest_student_flag"


def test_one_head_and_the_revision_follows_the_test_student_mark() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    revision = script.get_revision(REVISION)
    assert len(heads) == 1
    assert revision is not None
    assert revision.down_revision == BELOW
    assert REVISION in {rev.revision for rev in script.walk_revisions("base", heads[0])}


def _columns(conn: Connection, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


def _added(conn: Connection) -> dict[str, bool]:
    inspector = inspect(conn)
    credential_indexes = {
        i["name"] for i in inspector.get_indexes("student_credentials")
    }
    return {
        "enroll_key": "enroll_key" in _columns(conn, "course_nodes"),
        "source": "source" in _columns(conn, "student_enrollments"),
        "adult": "adult_confirmed_at" in _columns(conn, "students"),
        "signups": "portal_signups" in inspector.get_table_names(),
        "email_index": "ix_student_credentials_confirmed_email" in credential_indexes,
    }


def test_up_over_existing_rows_then_down_and_up() -> None:
    cfg = Config("alembic.ini")
    engine = create_engine(get_settings().database_url)
    tenant_id, course_id, student_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        command.downgrade(cfg, BELOW)
        with engine.begin() as conn:
            assert not any(_added(conn).values())
            conn.execute(
                text("INSERT INTO tenants (id, name, is_active) VALUES (:t, :n, true)"),
                {"t": tenant_id, "n": f"o1-mig-{tenant_id.hex[:8]}"},
            )
            conn.execute(
                text(
                    "INSERT INTO course_nodes (id, tenant_id, title, default_language) "
                    "VALUES (:c, :t, 'Course', 'ukr')"
                ),
                {"c": course_id, "t": tenant_id},
            )
            conn.execute(
                text(
                    "INSERT INTO students (id, tenant_id, external_id) "
                    "VALUES (:s, :t, 'before')"
                ),
                {"s": student_id, "t": tenant_id},
            )
            conn.execute(
                text(
                    "INSERT INTO student_enrollments (id, student_id, course_node_id) "
                    "VALUES (:e, :s, :c)"
                ),
                {"e": uuid.uuid4(), "s": student_id, "c": course_id},
            )

        command.upgrade(cfg, REVISION)
        with engine.connect() as conn:
            assert all(_added(conn).values())
            assert (
                conn.scalar(
                    text(
                        "SELECT source FROM student_enrollments WHERE student_id = :s"
                    ),
                    {"s": student_id},
                )
                == "author"
            )
            assert (
                conn.scalar(
                    text("SELECT enroll_key FROM course_nodes WHERE id = :c"),
                    {"c": course_id},
                )
                is None
            )
            assert (
                conn.scalar(
                    text("SELECT adult_confirmed_at FROM students WHERE id = :s"),
                    {"s": student_id},
                )
                is None
            )
            comment = conn.scalar(
                text("SELECT obj_description('portal_signups'::regclass)")
            )
            assert comment is not None and "O-1" in comment

        command.downgrade(cfg, BELOW)
        with engine.connect() as conn:
            assert not any(_added(conn).values())
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM student_enrollments WHERE student_id = :s"
                    ),
                    {"s": student_id},
                )
                == 1
            )
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
    with engine.connect() as conn:
        head = ScriptDirectory.from_config(cfg).get_current_head()
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == head
        assert all(_added(conn).values())
    engine.dispose()


def test_a_key_only_on_a_root_and_a_source_only_author_or_self() -> None:
    engine = create_engine(get_settings().database_url)
    tenant_id, root_id = uuid.uuid4(), uuid.uuid4()
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO tenants (id, name, is_active) VALUES (:t, :n, true)"),
                {"t": tenant_id, "n": f"o1-ck-{tenant_id.hex[:8]}"},
            )
            conn.execute(
                text(
                    "INSERT INTO course_nodes (id, tenant_id, title, default_language) "
                    "VALUES (:c, :t, 'Course', 'ukr')"
                ),
                {"c": root_id, "t": tenant_id},
            )
        with (
            pytest.raises(DBAPIError, match="ck_course_nodes_enroll_key_root"),
            engine.begin() as conn,
        ):
            conn.execute(
                text(
                    "INSERT INTO course_nodes (id, tenant_id, parent_id, title, "
                    "enroll_key) VALUES (:c, :t, :p, 'Child', 'k')"
                ),
                {"c": uuid.uuid4(), "t": tenant_id, "p": root_id},
            )
        with (
            pytest.raises(DBAPIError, match="ck_student_enrollments_source"),
            engine.begin() as conn,
        ):
            student_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO students (id, tenant_id, external_id) "
                    "VALUES (:s, :t, 'x')"
                ),
                {"s": student_id, "t": tenant_id},
            )
            conn.execute(
                text(
                    "INSERT INTO student_enrollments "
                    "(id, student_id, course_node_id, source) "
                    "VALUES (:e, :s, :c, 'other')"
                ),
                {"e": uuid.uuid4(), "s": student_id, "c": root_id},
            )
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        engine.dispose()
