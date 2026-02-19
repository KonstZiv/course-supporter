"""Verify that Alembic migrations produce a schema matching ORM metadata.

This test catches the class of bugs where a new ORM model is added but
the corresponding Alembic migration is missing or incomplete.

Requires a running PostgreSQL instance (docker compose up).
"""

import pytest
from sqlalchemy import create_engine, inspect, text

from course_supporter.config import get_settings
from course_supporter.storage.orm import Base

pytestmark = pytest.mark.requires_db


def _get_sync_url() -> str:
    """Build a sync DB URL from settings."""
    return get_settings().database_url


@pytest.fixture()
def db_engine():
    """Create a sync engine for schema inspection."""
    engine = create_engine(_get_sync_url())
    yield engine
    engine.dispose()


class TestSchemaSync:
    """Ensure ORM metadata and actual DB schema are in sync."""

    def test_all_orm_tables_exist_in_db(self, db_engine) -> None:  # type: ignore[no-untyped-def]
        """Every table in Base.metadata must exist in PostgreSQL."""
        inspector = inspect(db_engine)
        db_tables = set(inspector.get_table_names())
        orm_tables = set(Base.metadata.tables.keys())

        missing = orm_tables - db_tables
        assert not missing, f"ORM tables missing from DB (migration needed?): {missing}"

    def test_all_orm_columns_exist_in_db(self, db_engine) -> None:  # type: ignore[no-untyped-def]
        """Every column defined in ORM must exist in the DB table."""
        inspector = inspect(db_engine)
        db_tables = set(inspector.get_table_names())

        for table_name, table in Base.metadata.tables.items():
            if table_name not in db_tables:
                continue  # caught by test_all_orm_tables_exist_in_db

            db_columns = {col["name"] for col in inspector.get_columns(table_name)}
            orm_columns = {col.name for col in table.columns}
            missing = orm_columns - db_columns
            assert not missing, (
                f"Table '{table_name}': columns missing from DB: {missing}"
            )

    def test_alembic_head_matches_current(self, db_engine) -> None:  # type: ignore[no-untyped-def]
        """Alembic current revision must be at head (no unapplied migrations)."""
        with db_engine.connect() as conn:
            result = conn.execute(text("SELECT version_num FROM alembic_version"))
            current = result.scalar_one_or_none()

        assert current is not None, "No alembic_version found — migrations not applied"

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        alembic_cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(alembic_cfg)
        head = script.get_current_head()

        assert current == head, (
            f"DB at revision {current}, but head is {head}. Run: alembic upgrade head"
        )
