"""Lock (е) of Б-4: the migration ``b4_promotion_funnel`` up, down and up again.

On a database with enrollments already there, the upgrade writes one event per
enrollment (its id, time and source, the student's tenant, no tag); on a fresh,
empty database the whole chain goes up, one step down and up again. One head.
The events are insert-only.

Requires ``docker compose up -d`` (PostgreSQL), migrated to head; ``--run-db``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from course_supporter.config import get_settings

pytestmark = pytest.mark.requires_db

REVISION = "b4_promotion_funnel"
BELOW = "v1_author_interface_language"
AUTHOR_AT = datetime(2026, 11, 1, 9, tzinfo=UTC)
SELF_AT = datetime(2026, 11, 2, 9, tzinfo=UTC)


def test_one_head_and_the_revision_follows_the_interface_language() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    revision = script.get_revision(REVISION)
    assert len(heads) == 1
    assert revision is not None
    assert revision.down_revision == BELOW
    assert REVISION in {rev.revision for rev in script.walk_revisions("base", heads[0])}


def _added(conn: Connection) -> dict[str, bool]:
    inspector = inspect(conn)
    return {
        "path": "promotion_path"
        in {c["name"] for c in inspector.get_columns("tenants")},
        "tag": "source_tag"
        in {c["name"] for c in inspector.get_columns("portal_signups")},
        "events": "enrollment_events" in inspector.get_table_names(),
    }


def _seed_below(conn: Connection, tenant_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """A tenant, a course, two students and their enrollments, before Б-4."""
    ids = {name: uuid.uuid4() for name in ("course", "s1", "s2", "e1", "e2")}
    conn.execute(
        text("INSERT INTO tenants (id, name, is_active) VALUES (:t, :n, true)"),
        {"t": tenant_id, "n": f"b4-mig-{tenant_id.hex[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO course_nodes (id, tenant_id, title, default_language) "
            "VALUES (:c, :t, 'Course', 'ukr')"
        ),
        {"c": ids["course"], "t": tenant_id},
    )
    for student in ("s1", "s2"):
        conn.execute(
            text(
                "INSERT INTO students (id, tenant_id, external_id) VALUES (:s, :t, :e)"
            ),
            {"s": ids[student], "t": tenant_id, "e": f"b4-{student}"},
        )
    for enrollment, student, source, when in (
        ("e1", "s1", "author", AUTHOR_AT),
        ("e2", "s2", "self", SELF_AT),
    ):
        conn.execute(
            text(
                "INSERT INTO student_enrollments "
                "(id, student_id, course_node_id, source, created_at) "
                "VALUES (:e, :s, :c, :src, :at)"
            ),
            {
                "e": ids[enrollment],
                "s": ids[student],
                "c": ids["course"],
                "src": source,
                "at": when,
            },
        )
    return ids


def test_up_over_existing_enrollments_backfills_events_then_down_and_up() -> None:
    cfg = Config("alembic.ini")
    engine = create_engine(get_settings().database_url)
    tenant_id = uuid.uuid4()
    try:
        command.downgrade(cfg, BELOW)
        with engine.begin() as conn:
            assert not any(_added(conn).values())
            ids = _seed_below(conn, tenant_id)

        command.upgrade(cfg, REVISION)
        with engine.connect() as conn:
            assert all(_added(conn).values())
            events = conn.execute(
                text(
                    "SELECT id, tenant_id, course_node_id, student_id, source, "
                    "source_tag, created_at FROM enrollment_events "
                    "WHERE tenant_id = :t ORDER BY created_at"
                ),
                {"t": tenant_id},
            ).all()
            assert [tuple(e) for e in events] == [
                (
                    ids["e1"],
                    tenant_id,
                    ids["course"],
                    ids["s1"],
                    "author",
                    None,
                    AUTHOR_AT,
                ),
                (ids["e2"], tenant_id, ids["course"], ids["s2"], "self", None, SELF_AT),
            ]
            assert (
                conn.scalar(
                    text("SELECT promotion_path FROM tenants WHERE id = :t"),
                    {"t": tenant_id},
                )
                is None
            )
            comment = conn.scalar(
                text("SELECT obj_description('enrollment_events'::regclass)")
            )
            assert comment is not None and "Б-4" in comment
        with (
            pytest.raises(DBAPIError, match="insert-only"),
            engine.begin() as conn,
        ):
            conn.execute(
                text("UPDATE enrollment_events SET source_tag = 'x' WHERE id = :e"),
                {"e": ids["e1"]},
            )

        command.downgrade(cfg, BELOW)
        with engine.connect() as conn:
            assert not any(_added(conn).values())
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM student_enrollments "
                        "WHERE course_node_id = :c"
                    ),
                    {"c": ids["course"]},
                )
                == 2
            ), "the grants are untouched"
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
    with engine.connect() as conn:
        head = ScriptDirectory.from_config(cfg).get_current_head()
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == head
        assert all(_added(conn).values())
    engine.dispose()


def test_the_checks_refuse_a_bad_path_tag_and_source() -> None:
    engine = create_engine(get_settings().database_url)
    tenant_id = uuid.uuid4()
    try:
        with engine.begin() as conn:
            ids = _seed_below(conn, tenant_id)
        bad = {
            "ck_tenants_promotion_path": (
                "UPDATE tenants SET promotion_path = 'partner' WHERE id = :t"
            ),
            "ck_enrollment_events_source_tag": (
                "INSERT INTO enrollment_events (id, tenant_id, course_node_id, "
                "student_id, source, source_tag) "
                "VALUES (gen_random_uuid(), :t, :c, :s, 'self', 'Bad Tag!')"
            ),
            "ck_enrollment_events_source": (
                "INSERT INTO enrollment_events (id, tenant_id, course_node_id, "
                "student_id, source) VALUES (gen_random_uuid(), :t, :c, :s, 'other')"
            ),
        }
        for constraint, statement in bad.items():
            with pytest.raises(DBAPIError, match=constraint), engine.begin() as conn:
                conn.execute(
                    text(statement),
                    {"t": tenant_id, "c": ids["course"], "s": ids["s1"]},
                )
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        engine.dispose()


def _alembic(database: str, *args: str) -> None:
    """Run alembic against ``database`` (the settings read POSTGRES_DB)."""
    done = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        env={**os.environ, "POSTGRES_DB": database},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert done.returncode == 0, done.stderr[-2000:]


def test_up_down_up_on_an_empty_database() -> None:
    admin = create_engine(get_settings().database_url, isolation_level="AUTOCOMMIT")
    database = f"cs_b4_empty_{uuid.uuid4().hex[:8]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{database}"'))
    try:
        _alembic(database, "upgrade", "head")
        _alembic(database, "downgrade", "-1")
        _alembic(database, "upgrade", "head")
        settings = get_settings()
        url = settings.database_url.rsplit("/", 1)[0] + f"/{database}"
        fresh = create_engine(url)
        with fresh.connect() as conn:
            assert all(_added(conn).values())
            assert conn.scalar(text("SELECT count(*) FROM enrollment_events")) == 0
            assert (
                conn.scalar(text("SELECT version_num FROM alembic_version"))
                == ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
            )
        fresh.dispose()
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        admin.dispose()
