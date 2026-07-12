"""Integration tests for DocumentSummaryRepository (Phase 2.1 C5).

Requires ``docker compose up -d`` (PostgreSQL).
Run with ``uv run pytest --run-db -v`` against this file.

Covers Phase 2.1 C5 (KD-2.1-A + KD-2.1-F):

* ``create`` materialises a DocumentSummary with status='ready' and
  derives ``course_root_id`` from the parent AuthoredDocument.
* ``create`` invokes ``ContentHashService.invalidate_up`` so the
  parent chain receives a fresh ``content_hash`` value (KD-2.1-F).
* ``get_by_id`` / ``get_by_authored_document_id`` round-trip
  correctly.
* ``create`` rejects unknown ``authored_document_id`` with
  ``ValueError`` (defence-in-depth check).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.storage.document_summary_repository import (
    DocumentSummaryRepository,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
)

pytestmark = pytest.mark.requires_db


class TestCreate:
    """``create`` materialisation + cascade discipline."""

    async def test_create_persists_summary_with_ready_status(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        repo = DocumentSummaryRepository(db_session)

        summary = await repo.create(
            authored_document_id=seed_material_entry.id,
            title="Intro to Compilers",
            description="Five-week module on parser construction.",
            main_concepts=["lexer", "parser"],
            secondary_concepts=["AST"],
            content_char_count=1234,
        )

        assert summary.id is not None
        assert summary.authored_document_id == seed_material_entry.id
        assert summary.course_root_id == seed_material_entry.course_root_id
        assert summary.title == "Intro to Compilers"
        assert summary.description == "Five-week module on parser construction."
        assert summary.main_concepts == ["lexer", "parser"]
        assert summary.secondary_concepts == ["AST"]
        assert summary.content_char_count == 1234
        assert summary.status == "ready"
        # task-code-materials: structure defaults to None for non-code.
        assert summary.structure is None

    async def test_create_persists_code_structure(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """task-code-materials: the code project tree persists as JSONB."""
        repo = DocumentSummaryRepository(db_session)
        structure = {
            "entries": [
                {"path": "src/app.py", "size": 42, "cls": "included", "reason": None},
                {
                    "path": "vendor.zip",
                    "size": 7,
                    "cls": "excluded",
                    "reason": "nested_archive",
                },
            ]
        }

        summary = await repo.create(
            authored_document_id=seed_material_entry.id,
            title="Lesson project",
            description="Code material.",
            main_concepts=[],
            secondary_concepts=[],
            content_char_count=42,
            structure=structure,
        )
        await db_session.flush()
        await db_session.refresh(summary)
        assert summary.structure == structure

    async def test_create_invalidates_parent_chain_content_hash(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """KD-2.1-F: create() cascades content_hash up to the root.

        Before create: parent CourseNode + AuthoredDocument may or may not
        have a content_hash. After create: both are materialised with a
        non-null value reflecting the new summary's contribution.
        """
        repo = DocumentSummaryRepository(db_session)

        await repo.create(
            authored_document_id=seed_material_entry.id,
            title="t",
            description="d",
            main_concepts=[],
            secondary_concepts=[],
            content_char_count=10,
        )

        # Refresh parents to read the post-cascade hash values.
        await db_session.refresh(seed_material_entry)
        await db_session.refresh(seed_root_node)
        assert seed_material_entry.content_hash is not None
        assert seed_root_node.content_hash is not None

    async def test_create_raises_on_unknown_authored_document(
        self,
        db_session: AsyncSession,
    ) -> None:
        repo = DocumentSummaryRepository(db_session)
        ghost = uuid.uuid4()

        with pytest.raises(ValueError, match="AuthoredDocument not found"):
            await repo.create(
                authored_document_id=ghost,
                title="t",
                description="d",
                main_concepts=[],
                secondary_concepts=[],
                content_char_count=0,
            )


class TestGetByIdAndAuthoredDocument:
    """Round-trip lookups via the two read methods."""

    async def test_get_by_id_returns_inserted_row(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        repo = DocumentSummaryRepository(db_session)
        created = await repo.create(
            authored_document_id=seed_material_entry.id,
            title="t",
            description="d",
            main_concepts=[],
            secondary_concepts=[],
            content_char_count=5,
        )

        fetched = await repo.get_by_id(created.id)
        assert fetched is not None
        assert fetched.id == created.id

    async def test_get_by_id_returns_none_for_missing(
        self,
        db_session: AsyncSession,
    ) -> None:
        repo = DocumentSummaryRepository(db_session)
        assert await repo.get_by_id(uuid.uuid4()) is None

    async def test_get_by_authored_document_id_returns_unique_row(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        repo = DocumentSummaryRepository(db_session)
        created = await repo.create(
            authored_document_id=seed_material_entry.id,
            title="t",
            description="d",
            main_concepts=[],
            secondary_concepts=[],
            content_char_count=5,
        )

        fetched = await repo.get_by_authored_document_id(seed_material_entry.id)
        assert fetched is not None
        assert fetched.id == created.id


class TestReprocessActiveUniqueness:
    """Task 3.2.6 Finding 2: reprocess soft-deletes the old summary + cascades
    its segments, the new one persists, and the partial-active unique lets the
    soft-deleted predecessor coexist with the single live row.
    """

    async def test_reprocess_soft_deletes_old_and_inserts_new(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        repo = DocumentSummaryRepository(db_session)

        old = await repo.create(
            authored_document_id=seed_material_entry.id,
            title="legacy",
            description="d",
            main_concepts=["змінна"],
            secondary_concepts=[],
            content_char_count=10,
        )
        # Reprocess — must NOT raise UniqueViolation; releases the old slot.
        new = await repo.create(
            authored_document_id=seed_material_entry.id,
            title="reprocessed",
            description="d",
            main_concepts=["variable", "змінна"],
            secondary_concepts=[],
            content_char_count=20,
        )

        await db_session.refresh(old)
        assert old.deleted_at is not None, "old summary must be soft-deleted"
        assert new.deleted_at is None, "new summary must be active"
        assert new.id != old.id

        # get_by_authored_document_id returns the active one only.
        active = await repo.get_by_authored_document_id(seed_material_entry.id)
        assert active is not None
        assert active.id == new.id

        # Both rows coexist: one active + one soft-deleted (partial unique).
        total = await db_session.scalar(
            select(func.count())
            .select_from(DocumentSummary)
            .where(DocumentSummary.authored_document_id == seed_material_entry.id)
        )
        assert total == 2

    async def test_reprocess_cascades_old_segments(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        repo = DocumentSummaryRepository(db_session)

        old = await repo.create(
            authored_document_id=seed_material_entry.id,
            title="legacy",
            description="d",
            main_concepts=[],
            secondary_concepts=[],
            content_char_count=10,
        )
        segment = DocumentSegment(
            document_summary_id=old.id,
            course_root_id=old.course_root_id,
            order=0,
            start_pos=0,
            end_pos=5,
            content="hello",
        )
        db_session.add(segment)
        await db_session.flush()

        # Reprocess — the release cascades the old summary's segments.
        await repo.create(
            authored_document_id=seed_material_entry.id,
            title="reprocessed",
            description="d",
            main_concepts=[],
            secondary_concepts=[],
            content_char_count=20,
        )

        await db_session.refresh(segment)
        assert segment.deleted_at is not None, (
            "old segment must be soft-deleted via the cascade"
        )

    async def test_first_create_skips_release_no_op(
        self,
        db_session: AsyncSession,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """First-time create has no active prior summary — release is skipped
        (no crash on the None lookup).
        """
        repo = DocumentSummaryRepository(db_session)
        created = await repo.create(
            authored_document_id=seed_material_entry.id,
            title="first",
            description="d",
            main_concepts=[],
            secondary_concepts=[],
            content_char_count=10,
        )
        assert created.deleted_at is None
