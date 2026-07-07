"""Schema contract for KD18 P2 (project_bases + submission snapshot columns).

Inspector-based (no row inserts, no FK parents) so it asserts the migrated
schema shape cheaply and non-destructively: the ``project_bases`` table, its
CHECK / unique / index / FK, and the four NULLABLE submission carriers that P3
must be able to populate WITHOUT a second migration (rule #9 — a green upgrade
does not by itself prove the nullable contract).

Requires a running PostgreSQL instance with migrations applied
(``make db-upgrade``).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, inspect

from course_supporter.config import get_settings

pytestmark = pytest.mark.requires_db


@pytest.fixture()
def db_engine():  # type: ignore[no-untyped-def]
    """Sync engine for schema inspection."""
    engine = create_engine(get_settings().database_url)
    yield engine
    engine.dispose()


def _columns(engine: Any, table: str) -> dict[str, dict[str, Any]]:
    return {col["name"]: col for col in inspect(engine).get_columns(table)}


class TestProjectBasesTable:
    def test_table_exists(self, db_engine) -> None:  # type: ignore[no-untyped-def]
        assert "project_bases" in inspect(db_engine).get_table_names()

    def test_columns_and_nullability(self, db_engine) -> None:  # type: ignore[no-untyped-def]
        cols = _columns(db_engine, "project_bases")
        # NOT NULL carriers present from creation.
        for name in ("id", "authored_document_id", "version", "archive_key"):
            assert name in cols, f"missing column {name}"
            assert cols[name]["nullable"] is False, f"{name} should be NOT NULL"
        assert cols["state"]["nullable"] is False
        # NULL-until-READY carriers.
        for name in ("snapshot_key", "snapshot_hash", "manifest", "failure_reason"):
            assert name in cols, f"missing column {name}"
            assert cols[name]["nullable"] is True, f"{name} should be nullable"

    def test_state_default_pending(self, db_engine) -> None:  # type: ignore[no-untyped-def]
        cols = _columns(db_engine, "project_bases")
        default = cols["state"].get("default") or ""
        assert "pending" in str(default)

    def test_state_check_constraint(self, db_engine) -> None:  # type: ignore[no-untyped-def]
        checks = {
            c["name"] for c in inspect(db_engine).get_check_constraints("project_bases")
        }
        assert "ck_project_bases_state" in checks

    def test_snapshot_hash_index(self, db_engine) -> None:  # type: ignore[no-untyped-def]
        idx = {i["name"]: i for i in inspect(db_engine).get_indexes("project_bases")}
        assert "ix_project_bases_snapshot_hash" in idx
        assert idx["ix_project_bases_snapshot_hash"]["unique"] is False

    def test_document_version_unique(self, db_engine) -> None:  # type: ignore[no-untyped-def]
        idx = {i["name"]: i for i in inspect(db_engine).get_indexes("project_bases")}
        assert "uq_project_base_document_version" in idx
        entry = idx["uq_project_base_document_version"]
        assert entry["unique"] is True
        assert entry["column_names"] == ["authored_document_id", "version"]

    def test_document_fk_cascade(self, db_engine) -> None:  # type: ignore[no-untyped-def]
        fks = inspect(db_engine).get_foreign_keys("project_bases")
        doc_fk = next(fk for fk in fks if fk["referred_table"] == "authored_documents")
        assert doc_fk["constrained_columns"] == ["authored_document_id"]
        assert doc_fk["options"].get("ondelete") == "CASCADE"


class TestHomeworkSubmissionSnapshotColumns:
    """The four P2 carriers must exist and be nullable (P3 no-migration contract)."""

    def test_four_nullable_columns(self, db_engine) -> None:  # type: ignore[no-untyped-def]
        cols = _columns(db_engine, "homework_submissions")
        for name in ("base_id", "snapshot_key", "snapshot_hash", "snapshot_manifest"):
            assert name in cols, f"missing column {name}"
            assert cols[name]["nullable"] is True, (
                f"{name} must be nullable so P3 populates without a 2nd migration"
            )

    def test_base_id_fk_set_null(self, db_engine) -> None:  # type: ignore[no-untyped-def]
        fks = inspect(db_engine).get_foreign_keys("homework_submissions")
        base_fk = next(fk for fk in fks if fk["referred_table"] == "project_bases")
        assert base_fk["constrained_columns"] == ["base_id"]
        assert base_fk["options"].get("ondelete") == "SET NULL"
