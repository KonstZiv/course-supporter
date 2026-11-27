"""Orphaned objects of the materials bucket: find them, queue them for cleanup (Б-2).

Before №735 a deleted course section did not hand the archives and snapshots of
its project bases to the cleanup, so their objects stayed in the bucket with no
reference from the database. :func:`run_orphans` finds such objects once and
puts their keys into the existing cleanup (``enqueue_s3_cleanup`` → the
``s3_cleanup_task`` worker). It deletes nothing itself. The command page, with
what counts as an orphan and why: ``orphans.md`` next to this file.

An object is an **orphan** when all three hold:

* (a) no live row refers to it — a document that is not soft-deleted (its
  ``source_url`` or ``slide_keys``), or a base of such a document
  (``project_bases.archive_key`` / ``snapshot_key``);
* (b) the course node named by its key is soft-deleted or does not exist;
* (c) it is older than the minimum age, so an upload that has not yet written
  its row is never touched.

A key that names no node (a root object, a foreign layout) is **undetermined**:
counted, never an orphan. The ``homework/`` prefix — students' work — is not
even listed. No key is written to the log or to the report: ``Orphan.key`` is
kept out of ``repr`` and out of every count.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog
from arq.connections import ArqRedis
from sqlalchemy import Row, Text, or_, select, text
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.services.s3_cleanup_orchestration import enqueue_s3_cleanup
from course_supporter.storage.orm import AuthoredDocument, CourseNode, ProjectBase
from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger(__name__)

HOMEWORK_PREFIX = "homework/"
"""Students' work. Never listed, never counted, never touched by this command."""

DEFAULT_MIN_AGE = timedelta(hours=48)
ENQUEUE_CHUNK = 500
"""Keys per ``s3_cleanup`` job: bounds the job row and the ARQ message."""
_REFERENCE_CHUNK = 200
"""Candidate keys per reference query: bounds the query's size."""

# Key layouts the application writes (see the command page):
#   tenants/{t}/nodes/{n}/{uuid}/{name}                      presigned upload
#   tenants/{t}/nodes/{n}/slides/{doc}/{NNNN}.webp           slide render
#   tenants/{t}/nodes/{n}/bases/{doc}/{uuid}/original.{ext}  base archive
#   tenants/{t}/nodes/{n}/bases/{doc}/{uuid}/snapshot.zip    base snapshot
#   {n}/{uuid}/{name}                                        upload_smart
_TENANT_KEY_MIN_PARTS = 5
_BASE_KEY_PARTS = 8
_LEGACY_KEY_PARTS = 3


class Category(StrEnum):
    """What an orphan was — the grouping of the report."""

    ARCHIVE = "archive"
    SNAPSHOT = "snapshot"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class KeyOrigin:
    """The course node a key names, and the tenant when the key carries one."""

    node_id: uuid.UUID
    tenant_id: uuid.UUID | None


def _canonical_uuid(part: str) -> uuid.UUID | None:
    """The UUID a key segment spells in the canonical form, else ``None``."""
    try:
        value = uuid.UUID(part)
    except ValueError:
        return None
    return value if str(value) == part.lower() else None


def origin_of(key: str) -> KeyOrigin | None:
    """The node a key names, or ``None`` when the layout is not one we write."""
    parts = key.split("/")
    if not parts[-1]:  # a "folder" marker, not an object of ours
        return None
    if len(parts) >= _TENANT_KEY_MIN_PARTS and parts[0] == "tenants":
        tenant, node = _canonical_uuid(parts[1]), _canonical_uuid(parts[3])
        if parts[2] == "nodes" and tenant is not None and node is not None:
            return KeyOrigin(node_id=node, tenant_id=tenant)
        return None
    if len(parts) == _LEGACY_KEY_PARTS:
        node = _canonical_uuid(parts[0])
        if node is not None and _canonical_uuid(parts[1]) is not None:
            return KeyOrigin(node_id=node, tenant_id=None)
    return None


def category_of(key: str) -> Category:
    """A project base's archive or snapshot by its place in the key, else other."""
    parts = key.split("/")
    if len(parts) == _BASE_KEY_PARTS and parts[0] == "tenants" and parts[4] == "bases":
        if parts[-1] == "snapshot.zip":
            return Category.SNAPSHOT
        if parts[-1].startswith("original."):
            return Category.ARCHIVE
    return Category.OTHER


@dataclass(frozen=True, slots=True)
class Orphan:
    """One orphaned object. The key is not shown in ``repr``."""

    key: str = field(repr=False)
    size: int
    category: Category
    tenant_id: uuid.UUID | None


@dataclass(slots=True)
class CategoryTotals:
    """Objects and bytes of one category."""

    objects: int = 0
    bytes: int = 0


@dataclass(slots=True)
class OrphanReport:
    """What a scan found: counts and sums only, never a key.

    The first four counters partition the scanned objects that are not orphans,
    in the order the checks run.
    """

    scanned: int = 0
    unresolved: int = 0
    too_young: int = 0
    live_node: int = 0
    referenced: int = 0
    by_category: dict[Category, CategoryTotals] = field(
        default_factory=lambda: {c: CategoryTotals() for c in Category}
    )
    tenants: set[uuid.UUID] = field(default_factory=set)
    without_tenant: int = 0
    jobs: int = 0
    jobs_not_dispatched: int = 0

    @property
    def orphans(self) -> int:
        """All orphans found."""
        return sum(t.objects for t in self.by_category.values())

    @property
    def orphan_bytes(self) -> int:
        """Their total size."""
        return sum(t.bytes for t in self.by_category.values())

    def add(self, orphans: Sequence[Orphan]) -> None:
        """Count a batch of orphans."""
        for orphan in orphans:
            totals = self.by_category[orphan.category]
            totals.objects += 1
            totals.bytes += orphan.size
            if orphan.tenant_id is None:
                self.without_tenant += 1
            else:
                self.tenants.add(orphan.tenant_id)


# ── Scan ───────────────────────────────────────────────────────────────


async def scan_orphans(
    factory: async_sessionmaker[AsyncSession],
    s3: S3Client,
    *,
    min_age: timedelta,
    report: OrphanReport,
    now: datetime | None = None,
) -> AsyncIterator[list[Orphan]]:
    """Walk the bucket and yield the orphans of each page; ``report`` is filled.

    Reads only: every query runs in a ``READ ONLY`` transaction.
    """
    cutoff = (now or datetime.now(UTC)) - min_age
    async for page in s3.iter_object_pages(skip_top_level={HOMEWORK_PREFIX}):
        orphans = await _orphans_of_page(factory, page, cutoff, report)
        if orphans:
            yield orphans


async def _orphans_of_page(
    factory: async_sessionmaker[AsyncSession],
    page: list[dict[str, Any]],
    cutoff: datetime,
    report: OrphanReport,
) -> list[Orphan]:
    report.scanned += len(page)
    aged: list[tuple[dict[str, Any], KeyOrigin]] = []
    for obj in page:
        origin = origin_of(obj["key"])
        if origin is None:
            report.unresolved += 1
        elif obj["last_modified"] > cutoff:
            report.too_young += 1
        else:
            aged.append((obj, origin))
    if not aged:
        return []

    async with factory() as session:
        await session.execute(text("SET TRANSACTION READ ONLY"))
        nodes = await _node_rows(session, {origin.node_id for _, origin in aged})
        gone: list[tuple[dict[str, Any], KeyOrigin]] = []
        for obj, origin in aged:
            node = nodes.get(origin.node_id)
            if node is not None and node.deleted_at is None:
                report.live_node += 1
            else:
                gone.append((obj, origin))
        referenced = await _live_references(session, [obj["key"] for obj, _ in gone])

    orphans: list[Orphan] = []
    for obj, origin in gone:
        if obj["key"] in referenced:
            report.referenced += 1
            continue
        node = nodes.get(origin.node_id)
        orphans.append(
            Orphan(
                key=obj["key"],
                size=obj["size"],
                category=category_of(obj["key"]),
                tenant_id=origin.tenant_id or (node.tenant_id if node else None),
            )
        )
    return orphans


async def _node_rows(
    session: AsyncSession, node_ids: set[uuid.UUID]
) -> dict[uuid.UUID, Row[Any]]:
    """The rows of the named nodes, soft-deleted ones included, by id."""
    stmt = select(CourseNode.id, CourseNode.tenant_id, CourseNode.deleted_at).where(
        CourseNode.id.in_(node_ids)
    )
    return {row.id: row for row in (await session.execute(stmt)).all()}


async def _live_references(session: AsyncSession, keys: Sequence[str]) -> set[str]:
    """The keys among ``keys`` that a live row still refers to (a)."""
    found: set[str] = set()
    for start in range(0, len(keys), _REFERENCE_CHUNK):
        chunk = keys[start : start + _REFERENCE_CHUNK]
        found |= await _referenced_by_documents(session, chunk)
        found |= await _referenced_by_bases(session, chunk)
    return found


async def _referenced_by_documents(
    session: AsyncSession, chunk: Sequence[str]
) -> set[str]:
    """Keys in ``source_url`` or ``slide_keys`` of a document not soft-deleted.

    ``source_url`` holds a URL, not a key, and the host part may differ, so it
    is matched by the ``/<key>`` it ends with; the match may be too wide
    (another key ending the same way), never too narrow.
    """
    stmt = select(AuthoredDocument.source_url, AuthoredDocument.slide_keys).where(
        AuthoredDocument.deleted_at.is_(None),
        or_(
            *(
                AuthoredDocument.source_url.endswith(f"/{key}", autoescape=True)
                for key in chunk
            ),
            AuthoredDocument.slide_keys.has_any(array(chunk, type_=Text)),
        ),
    )
    wanted = set(chunk)
    found: set[str] = set()
    for source_url, slide_keys in (await session.execute(stmt)).all():
        found.update(key for key in chunk if source_url.endswith(f"/{key}"))
        found.update(wanted.intersection(slide_keys or ()))
    return found


async def _referenced_by_bases(session: AsyncSession, chunk: Sequence[str]) -> set[str]:
    """Keys in a base of a document that is not soft-deleted.

    ``project_bases`` has no ``deleted_at`` and soft-delete does not remove its
    rows, so a base is live exactly when its document is.
    """
    stmt = (
        select(ProjectBase.archive_key, ProjectBase.snapshot_key)
        .join(AuthoredDocument, AuthoredDocument.id == ProjectBase.authored_document_id)
        .where(
            AuthoredDocument.deleted_at.is_(None),
            or_(
                ProjectBase.archive_key.in_(chunk),
                ProjectBase.snapshot_key.in_(chunk),
            ),
        )
    )
    wanted = set(chunk)
    return {
        key
        for row in (await session.execute(stmt)).all()
        for key in row
        if key in wanted
    }


# ── Run ────────────────────────────────────────────────────────────────


async def run_orphans(
    factory: async_sessionmaker[AsyncSession],
    s3: S3Client,
    arq: ArqRedis | None,
    *,
    actor: str,
    min_age: timedelta = DEFAULT_MIN_AGE,
    now: datetime | None = None,
) -> OrphanReport:
    """Find the orphans; with ``arq`` given, put their keys into the cleanup.

    ``arq=None`` is the dry run: nothing is written or queued — the report
    counts the orphans and sums their sizes. Otherwise the keys go, a chunk at
    a time, through :func:`enqueue_s3_cleanup`; the worker deletes them.
    """
    report = OrphanReport()
    pending: list[str] = []
    async for orphans in scan_orphans(
        factory, s3, min_age=min_age, report=report, now=now
    ):
        report.add(orphans)
        if arq is None:
            continue
        pending.extend(orphan.key for orphan in orphans)
        while len(pending) >= ENQUEUE_CHUNK:
            await _enqueue(factory, arq, pending[:ENQUEUE_CHUNK], report)
            del pending[:ENQUEUE_CHUNK]
    if pending and arq is not None:
        await _enqueue(factory, arq, pending, report)

    logger.info(
        "storage_orphans_dry_run" if arq is None else "storage_orphans_enqueued",
        actor=actor,
        min_age_hours=min_age.total_seconds() / 3600,
        scanned=report.scanned,
        orphans=report.orphans,
        archives=report.by_category[Category.ARCHIVE].objects,
        snapshots=report.by_category[Category.SNAPSHOT].objects,
        other=report.by_category[Category.OTHER].objects,
        bytes=report.orphan_bytes,
        tenants=len(report.tenants),
        unresolved=report.unresolved,
        jobs=report.jobs,
        jobs_not_dispatched=report.jobs_not_dispatched,
    )
    return report


async def _enqueue(
    factory: async_sessionmaker[AsyncSession],
    arq: ArqRedis,
    keys: list[str],
    report: OrphanReport,
) -> None:
    """One ``s3_cleanup`` job for ``keys``; counts it, and whether ARQ took it."""
    async with factory() as session:
        job = await enqueue_s3_cleanup(session=session, arq=arq, file_keys=keys)
        await session.refresh(job)
    report.jobs += 1
    if job.arq_job_id is None:
        report.jobs_not_dispatched += 1
