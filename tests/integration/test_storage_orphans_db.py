"""Orphan objects of the materials bucket — the locks, on a real database (Б-2).

``python -m scripts.storage_admin orphans`` finds the objects of course sections
deleted before №735 and puts their keys into the existing cleanup. Real
PostgreSQL; the bucket is the S3-compatible storage of the run (``moto_server``
or SeaweedFS at ``s3_endpoint``) — a bucket made for each test, so the counts
are the test's own and nothing else in the shared bucket is scanned. ARQ is a
mock: only what the command asks it to queue matters, and the cleanup task is
run here by hand.

Locks — the task's seven letters, transliterated: a b v h g d e:

* (a) an object a live row refers to stays;
* (b) an object of a deleted node with no reference is an orphan and is queued;
* (v) an object of a live node with no reference is not;
* (h) an object younger than the minimum age is not;
* (g) ``homework/`` is neither listed nor touched;
* (d) ``--dry-run`` changes nothing and queues nothing;
* (e) a second run, after the cleanup, finds 0.

Mutation: drop condition (b) from ``_orphans_of_page`` and (v) goes red.
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiobotocore.session import get_session
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from scripts import storage_admin
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.config import get_settings
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    Job,
    ProjectBase,
    Tenant,
)
from course_supporter.storage.orphans import Category, OrphanReport, run_orphans
from course_supporter.storage.s3 import S3Client
from course_supporter.workers.s3_cleanup import s3_cleanup_task
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db

LONG_AGO = timedelta(hours=49)
ONE_MIB = 1_048_576


# ── The bucket: one per test ───────────────────────────────────────────


@pytest.fixture()
async def s3() -> AsyncGenerator[S3Client]:
    """An S3 client on a bucket made for this test and removed after it."""
    s = get_settings()
    name = f"orphans-{uuid.uuid4().hex[:12]}"
    access = {
        "endpoint_url": s.s3_endpoint,
        "aws_access_key_id": s.s3_access_key,
        "aws_secret_access_key": s.s3_secret_key.get_secret_value(),
        "config": BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    }
    async with get_session().create_client("s3", **access) as admin:
        await admin.create_bucket(Bucket=name)
        client = S3Client(
            endpoint_url=s.s3_endpoint,
            access_key=s.s3_access_key,
            secret_key=s.s3_secret_key.get_secret_value(),
            bucket=name,
        )
        await client.open()
        try:
            yield client
        finally:
            for obj in await client.list_objects(""):
                await admin.delete_object(Bucket=name, Key=obj["key"])
            await admin.delete_bucket(Bucket=name)
            await client.close()


async def _put(s3: S3Client, key: str, size: int = 10) -> str:
    await s3.upload_file(key, b"x" * size, "application/octet-stream")
    return key


async def _put_base(
    s3: S3Client, tenant: uuid.UUID, node: uuid.UUID
) -> tuple[str, str]:
    """A base's archive and snapshot, put in the bucket."""
    archive, snapshot = _base_keys(tenant, node)
    return await _put(s3, archive), await _put(s3, snapshot)


async def _keys_in(s3: S3Client) -> set[str]:
    return {obj["key"] for obj in await s3.list_objects("")}


# ── The database: tenants, nodes, documents ────────────────────────────


@dataclass
class World:
    """Two tenants, each with a live and a deleted course, and what to clean up."""

    tenant: uuid.UUID
    live: uuid.UUID
    dead: uuid.UUID
    other_tenant: uuid.UUID
    other_dead: uuid.UUID
    job_ids: list[uuid.UUID]


async def _make_course(
    session: AsyncSession, tenant_id: uuid.UUID, *, deleted: bool
) -> uuid.UUID:
    node = make_root_course_node(tenant_id=tenant_id, title="Orphans", order=0)
    session.add(node)
    await session.flush()
    if deleted:
        # What the delete route leaves: the node soft-deleted.
        node.deleted_at = datetime.now(UTC)
    return node.id


@pytest.fixture()
async def world(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[World]:
    async with session_factory() as session:
        tenants = [Tenant(name=f"orphans-{uuid.uuid4().hex[:8]}") for _ in range(2)]
        session.add_all(tenants)
        await session.flush()
        w = World(
            tenant=tenants[0].id,
            live=await _make_course(session, tenants[0].id, deleted=False),
            dead=await _make_course(session, tenants[0].id, deleted=True),
            other_tenant=tenants[1].id,
            other_dead=await _make_course(session, tenants[1].id, deleted=True),
            job_ids=[],
        )
        await session.commit()

    yield w

    async with session_factory() as session:
        nodes = select(CourseNode.id).where(
            CourseNode.tenant_id.in_([w.tenant, w.other_tenant])
        )
        docs = select(AuthoredDocument.id).where(
            AuthoredDocument.course_node_id.in_(nodes)
        )
        await session.execute(
            delete(ProjectBase).where(ProjectBase.authored_document_id.in_(docs))
        )
        await session.execute(
            delete(AuthoredDocument).where(AuthoredDocument.course_node_id.in_(nodes))
        )
        await session.execute(
            delete(CourseNode).where(
                CourseNode.tenant_id.in_([w.tenant, w.other_tenant])
            )
        )
        await session.execute(
            delete(Tenant).where(Tenant.id.in_([w.tenant, w.other_tenant]))
        )
        if w.job_ids:
            await session.execute(delete(Job).where(Job.id.in_(w.job_ids)))
        await session.commit()


def _key(tenant: uuid.UUID, node: uuid.UUID, tail: str) -> str:
    return f"tenants/{tenant}/nodes/{node}/{tail}"


def _upload_key(tenant: uuid.UUID, node: uuid.UUID, name: str = "lecture.pdf") -> str:
    return _key(tenant, node, f"{uuid.uuid4()}/{name}")


def _base_keys(tenant: uuid.UUID, node: uuid.UUID) -> tuple[str, str]:
    folder = _key(tenant, node, f"bases/{uuid.uuid4()}/{uuid.uuid4()}")
    return f"{folder}/original.zip", f"{folder}/snapshot.zip"


async def _add_document(
    session_factory: async_sessionmaker[AsyncSession],
    s3: S3Client,
    node: uuid.UUID,
    *,
    key: str | None = None,
    slide_keys: list[str] | None = None,
    deleted: bool = False,
) -> uuid.UUID:
    """A document of ``node``; ``deleted`` leaves it as the delete route does."""
    async with session_factory() as session:
        doc = AuthoredDocument(
            course_node_id=node,
            course_root_id=node,
            source_type="presentation" if slide_keys else "text",
            source_url=s3.get_object_url(key) if key else "https://example.org/a",
            slide_keys=slide_keys,
        )
        session.add(doc)
        await session.flush()
        if deleted:
            doc.deleted_at = datetime.now(UTC)
            doc.source_url = "інформація видалена автором 01-01-2026 00:00:00"
        await session.commit()
        return doc.id


async def _add_base(
    session_factory: async_sessionmaker[AsyncSession],
    document: uuid.UUID,
    archive: str,
    snapshot: str,
) -> None:
    async with session_factory() as session:
        session.add(
            ProjectBase(
                authored_document_id=document,
                version=1,
                archive_key=archive,
                snapshot_key=snapshot,
                state="ready",
            )
        )
        await session.commit()


async def _count_cleanup_jobs(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    async with session_factory() as session:
        stmt = select(func.count()).select_from(Job).where(Job.job_type == "s3_cleanup")
        return int((await session.execute(stmt)).scalar_one())


# ── The queue: an ARQ mock that remembers what it was asked ────────────


def _arq() -> AsyncMock:
    arq = AsyncMock()
    arq.enqueue_job = AsyncMock(return_value=MagicMock(job_id="arq-job"))
    return arq


def _queued(arq: AsyncMock) -> set[str]:
    return {
        key
        for call in arq.enqueue_job.call_args_list
        for key in call.kwargs["file_keys"]
    }


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    s3: S3Client,
    arq: AsyncMock | None,
    world: World,
    *,
    min_age: timedelta = timedelta(hours=48),
    old: bool = True,
) -> OrphanReport:
    """One run; ``old`` lets the objects just put count as 49 hours old."""
    report = await run_orphans(
        session_factory,
        s3,
        arq,
        actor="test",
        min_age=min_age,
        now=datetime.now(UTC) + LONG_AGO if old else None,
    )
    if arq is not None:
        world.job_ids.extend(
            uuid.UUID(call.kwargs["job_id"]) for call in arq.enqueue_job.call_args_list
        )
    return report


# ── The locks ──────────────────────────────────────────────────────────


class TestAnObjectALiveRowRefersToStays:
    """(a)"""

    async def test_a_live_reference_keeps_the_object(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3: S3Client,
        world: World,
    ) -> None:
        # Four objects keyed under the DELETED node, each still referred to by
        # a live document (one that was moved out of it, or a base re-pointed).
        by_url = await _put(s3, _upload_key(world.tenant, world.dead))
        slide = await _put(
            s3, _key(world.tenant, world.dead, f"slides/{uuid.uuid4()}/0001.webp")
        )
        archive, snapshot = await _put_base(s3, world.tenant, world.dead)
        await _add_document(session_factory, s3, world.live, key=by_url)
        await _add_document(session_factory, s3, world.live, slide_keys=[slide])
        project = await _add_document(session_factory, s3, world.live)
        await _add_base(session_factory, project, archive, snapshot)
        # The same node, no reference: the one orphan, so the test can tell.
        loose = await _put(s3, _upload_key(world.tenant, world.dead))

        arq = _arq()
        report = await _run(session_factory, s3, arq, world)

        assert _queued(arq) == {loose}
        assert report.referenced == 4
        assert report.orphans == 1


class TestAnObjectOfADeletedNodeIsAnOrphan:
    """(b)"""

    async def test_it_is_queued_for_the_cleanup_and_not_deleted_here(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3: S3Client,
        world: World,
    ) -> None:
        in_deleted = await _put(s3, _upload_key(world.tenant, world.dead))
        legacy = await _put(s3, f"{world.dead}/{uuid.uuid4()}/old.pdf")
        in_missing = await _put(s3, _upload_key(world.tenant, uuid.uuid4()))

        arq = _arq()
        report = await _run(session_factory, s3, arq, world)

        assert _queued(arq) == {in_deleted, legacy, in_missing}
        assert report.orphans == 3
        # The command only queues: the worker deletes.
        assert await _keys_in(s3) == {in_deleted, legacy, in_missing}
        # …through the existing cleanup: a Job per chunk, holding the keys.
        async with session_factory() as session:
            jobs = (
                (await session.execute(select(Job).where(Job.id.in_(world.job_ids))))
                .scalars()
                .all()
            )
        assert [j.job_type for j in jobs] == ["s3_cleanup"]
        assert jobs[0].input_params is not None
        assert set(jobs[0].input_params["file_keys"]) == _queued(arq)

    async def test_the_archives_of_a_deleted_document_are_found_though_its_bases_remain(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3: S3Client,
        world: World,
    ) -> None:
        # Soft-delete does not remove ``project_bases`` rows: a base of a
        # deleted document is no live reference. Its slides stay in the
        # document's ``slide_keys`` too — the same.
        archive, snapshot = await _put_base(s3, world.tenant, world.dead)
        slide = await _put(
            s3, _key(world.tenant, world.dead, f"slides/{uuid.uuid4()}/0001.webp")
        )
        document = await _add_document(
            session_factory, s3, world.dead, slide_keys=[slide], deleted=True
        )
        await _add_base(session_factory, document, archive, snapshot)

        arq = _arq()
        report = await _run(session_factory, s3, arq, world)

        assert _queued(arq) == {archive, snapshot, slide}
        assert report.by_category[Category.ARCHIVE].objects == 1
        assert report.by_category[Category.SNAPSHOT].objects == 1
        assert report.by_category[Category.OTHER].objects == 1


class TestAnObjectOfALiveNodeIsNotAnOrphan:
    """(v)"""

    async def test_no_reference_is_not_enough(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3: S3Client,
        world: World,
    ) -> None:
        # An upload that wrote no row, a stray file: the node is alive, so the
        # object is left for a human to look at.
        stray = await _put(s3, _upload_key(world.tenant, world.live))
        legacy = await _put(s3, f"{world.live}/{uuid.uuid4()}/old.pdf")

        arq = _arq()
        report = await _run(session_factory, s3, arq, world)

        assert _queued(arq) == set()
        arq.enqueue_job.assert_not_called()
        assert report.live_node == 2
        assert report.orphans == 0
        assert await _keys_in(s3) == {stray, legacy}


class TestAnObjectYoungerThanTheMinimumAgeIsNotAnOrphan:
    """(h)"""

    async def test_a_fresh_object_waits(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3: S3Client,
        world: World,
    ) -> None:
        fresh = await _put(s3, _upload_key(world.tenant, world.dead))

        arq = _arq()
        report = await _run(session_factory, s3, arq, world, old=False)
        assert _queued(arq) == set()
        assert report.too_young == 1
        assert report.orphans == 0

        # The same object, once it is older than the minimum.
        report = await _run(session_factory, s3, arq, world, old=True)
        assert _queued(arq) == {fresh}
        assert report.too_young == 0


class TestHomeworkIsNeverTouched:
    """(g)"""

    async def test_it_is_not_listed_and_not_queued(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3: S3Client,
        world: World,
    ) -> None:
        # Everything that would make an orphan elsewhere: a deleted node's id in
        # the key, no row, old.
        work = [
            await _put(s3, f"homework/{world.tenant}/{uuid.uuid4()}/work.py"),
            await _put(s3, f"homework/{world.dead}/{uuid.uuid4()}/work.py"),
            await _put(s3, f"homework/{world.tenant}/{uuid.uuid4()}/snapshot.zip"),
        ]
        material = await _put(s3, _upload_key(world.tenant, world.dead))

        arq = _arq()
        report = await _run(session_factory, s3, arq, world)

        assert _queued(arq) == {material}
        assert report.scanned == 1  # the three homework objects were not listed
        assert set(work) <= await _keys_in(s3)

    async def test_the_listing_never_enters_the_prefix(self, s3: S3Client) -> None:
        await _put(s3, "homework/a/b/work.py")
        await _put(s3, "root-object.txt")
        await _put(s3, f"{uuid.uuid4()}/{uuid.uuid4()}/m.pdf")
        pages: list[list[dict[str, Any]]] = [
            page async for page in s3.iter_object_pages(skip_top_level={"homework/"})
        ]
        listed = {obj["key"] for page in pages for obj in page}
        assert "root-object.txt" in listed
        assert not any(key.startswith("homework/") for key in listed)
        assert len(listed) == 2


class TestADryRunChangesNothing:
    """(d)"""

    async def test_nothing_is_written_or_queued(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3: S3Client,
        world: World,
    ) -> None:
        orphan = await _put(s3, _upload_key(world.tenant, world.dead), size=ONE_MIB)
        before = await _count_cleanup_jobs(session_factory)

        report = await _run(session_factory, s3, None, world)

        assert report.orphans == 1
        assert report.orphan_bytes == ONE_MIB
        assert report.jobs == 0
        assert await _count_cleanup_jobs(session_factory) == before
        assert await _keys_in(s3) == {orphan}

    async def test_the_command_opens_no_queue_and_prints_no_key(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3: S3Client,
        world: World,
    ) -> None:
        """The example of the command page: numbers of this test."""
        for size in (ONE_MIB, 2 * ONE_MIB):
            await _put(s3, _base_keys(world.tenant, world.dead)[0], size)
        await _put(s3, _base_keys(world.tenant, world.dead)[1], ONE_MIB // 2)
        await _put(s3, _base_keys(world.other_tenant, world.other_dead)[1], 700_000)
        await _put(s3, _upload_key(world.tenant, world.dead), 3 * ONE_MIB)
        # A section that is gone from the database: its tenant is not known.
        await _put(s3, f"{uuid.uuid4()}/{uuid.uuid4()}/old.pdf", 100_000)
        await _put(s3, _upload_key(world.tenant, world.live))  # a live node's
        await _put(s3, "README.txt")  # no node in the key
        await _put(s3, f"homework/{world.tenant}/{uuid.uuid4()}/work.py")
        before = await _count_cleanup_jobs(session_factory)
        asked: list[bool] = []

        @asynccontextmanager
        async def infrastructure(
            with_arq: bool,
        ) -> AsyncIterator[
            tuple[async_sessionmaker[AsyncSession], S3Client, AsyncMock | None]
        ]:
            asked.append(with_arq)
            yield session_factory, s3, None

        args = argparse.Namespace(actor="test", min_age_hours=0.0, dry_run=True)
        out, code = await storage_admin.run(args, infrastructure)

        assert (code, asked) == (0, [False])
        assert out == (
            "storage orphans  dry run: nothing written, nothing queued\n"
            "scanned 8 object(s), homework/ not listed; minimum age 0 h\n"
            "not orphans: 0 referenced by a live row, 1 of a live node, "
            "0 too young, 1 undetermined (the key names no node)\n"
            "orphans (no live reference; the node of the key deleted or missing):\n"
            "  archive        2 object(s)  3145728 B (3.0 MiB)\n"
            "  snapshot       2 object(s)  1224288 B (1.2 MiB)\n"
            "  other          2 object(s)  3245728 B (3.1 MiB)\n"
            "  total          6 object(s)  7615744 B (7.3 MiB)\n"
            "tenants: 2  (+ 1 object(s) with no tenant known)"
        )
        assert str(world.tenant) not in out
        assert await _count_cleanup_jobs(session_factory) == before


class TestASecondRunFindsNothing:
    """(e)"""

    async def test_after_the_cleanup_nothing_is_left(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3: S3Client,
        world: World,
    ) -> None:
        keep = await _put(s3, _upload_key(world.tenant, world.live))
        await _put(s3, _upload_key(world.tenant, world.dead))
        await _put(s3, _base_keys(world.tenant, world.dead)[0])
        arq = _arq()
        first = await _run(session_factory, s3, arq, world)
        assert first.orphans == 2

        # The worker's part, run by hand on what the command queued.
        ctx = {"s3_client": s3, "session_factory": session_factory}
        for call in arq.enqueue_job.call_args_list:
            await s3_cleanup_task(
                ctx, call.kwargs["job_id"], file_keys=call.kwargs["file_keys"]
            )
        assert await _keys_in(s3) == {keep}

        again = _arq()
        second = await _run(session_factory, s3, again, world)
        assert second.orphans == 0
        assert second.jobs == 0
        again.enqueue_job.assert_not_called()
        with pytest.raises(ClientError):
            await s3.get_object(next(iter(_queued(arq))))


class TestALargeRunIsQueuedInChunks:
    async def test_the_keys_go_in_jobs_of_a_bounded_size(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3: S3Client,
        world: World,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("course_supporter.storage.orphans.ENQUEUE_CHUNK", 2)
        keys = {await _put(s3, _upload_key(world.tenant, world.dead)) for _ in range(5)}

        arq = _arq()
        report = await _run(session_factory, s3, arq, world)

        sizes = sorted(
            len(c.kwargs["file_keys"]) for c in arq.enqueue_job.call_args_list
        )
        assert sizes == [1, 2, 2]
        assert _queued(arq) == keys
        assert report.jobs == 3
