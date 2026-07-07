"""Integration tests for the KD18 P3 snapshot persistence on HomeworkRepository.

Real PostgreSQL (``requires_db``): the ``base_id`` FK (submit-time) and the
three worker-time snapshot columns (``store_snapshot``), plus the manifest JSONB
round-trip through the normalizer serde. Requires ``docker compose up -d``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.normalizer import manifest_from_jsonb, manifest_to_jsonb
from course_supporter.normalizer.models import (
    EntryClass,
    ExcludedEntry,
    ExcludedReason,
    Manifest,
    ManifestEntry,
)
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    Student,
    Tenant,
)
from course_supporter.storage.project_base_repository import ProjectBaseRepository
from course_supporter.storage.student_repository import StudentRepository

pytestmark = pytest.mark.requires_db


def _manifest() -> Manifest:
    return Manifest(
        schema=1,
        aggregate_hash="a" * 64,
        included=(
            ManifestEntry(path="main.py", size=15, hash="b" * 64, cls=EntryClass.TEXT),
        ),
        excluded=(
            ExcludedEntry(
                path=".venv/", reason=ExcludedReason.DENYLIST_DIR, entries=5, size=0
            ),
        ),
        total_files=6,
        total_bytes=15,
    )


async def _student(session: AsyncSession, tenant: Tenant) -> Student:
    return await StudentRepository(session).create(
        tenant_id=tenant.id, external_id="ext-p3-snapshot"
    )


class TestCreateWithBaseId:
    async def test_persists_base_id(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """A project submission carries base_id (echo-matched at submit)."""
        base = await ProjectBaseRepository(db_session).create_version(
            authored_document_id=seed_material_entry.id, archive_key="k/v1/original.zip"
        )
        student = await _student(db_session, seed_tenant)
        repo = HomeworkRepository(db_session)
        sub = await repo.create(
            tenant_id=seed_tenant.id,
            student_id=student.id,
            course_node_id=seed_root_node.id,
            node_id=seed_root_node.id,
            authored_document_id=seed_material_entry.id,
            file_url="s3://bucket/proj.zip",
            file_type="application/zip",
            delivery_mode="in_app",
            base_id=base.id,
        )
        read = await repo.get_by_id(sub.id)
        assert read is not None
        assert read.base_id == base.id
        # Worker-time columns still empty until store_snapshot runs.
        assert read.snapshot_key is None
        assert read.snapshot_hash is None
        assert read.snapshot_manifest is None

    async def test_base_id_defaults_none(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """A single-file / non-project submission leaves base_id NULL."""
        student = await _student(db_session, seed_tenant)
        repo = HomeworkRepository(db_session)
        sub = await repo.create(
            tenant_id=seed_tenant.id,
            student_id=student.id,
            course_node_id=seed_root_node.id,
            node_id=seed_root_node.id,
            authored_document_id=seed_material_entry.id,
            file_url="s3://bucket/hw.py",
            file_type="text/plain",
            delivery_mode="in_app",
        )
        read = await repo.get_by_id(sub.id)
        assert read is not None
        assert read.base_id is None


class TestStoreSnapshot:
    async def test_round_trips_through_serde(
        self,
        db_session: AsyncSession,
        seed_tenant: Tenant,
        seed_root_node: CourseNode,
        seed_material_entry: AuthoredDocument,
    ) -> None:
        """store_snapshot persists the 3 worker-time columns; the manifest
        survives the serde inverse (manifest_from_jsonb == the original)."""
        student = await _student(db_session, seed_tenant)
        repo = HomeworkRepository(db_session)
        sub = await repo.create(
            tenant_id=seed_tenant.id,
            student_id=student.id,
            course_node_id=seed_root_node.id,
            node_id=seed_root_node.id,
            authored_document_id=seed_material_entry.id,
            file_url="s3://bucket/proj.zip",
            file_type="application/zip",
            delivery_mode="in_app",
        )

        manifest = _manifest()
        await repo.store_snapshot(
            sub.id,
            snapshot_key="homework/t/s/snapshot.zip",
            snapshot_hash="a" * 64,
            snapshot_manifest=manifest_to_jsonb(manifest),
        )

        # store_snapshot is a core UPDATE (mirror of store_safety_result); refresh
        # the identity-map copy (async-safe) so the assertions see the DB row.
        await db_session.refresh(sub)
        assert sub.snapshot_key == "homework/t/s/snapshot.zip"
        assert sub.snapshot_hash == "a" * 64
        assert sub.snapshot_manifest is not None
        assert manifest_from_jsonb(sub.snapshot_manifest) == manifest
