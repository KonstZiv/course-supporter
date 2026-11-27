"""Storage administration CLI (Б-2): clear the objects no row refers to any more.

Before №735 deleting a course section (``DELETE /nodes/{id}``) did not hand the
archives and snapshots of its project bases to the cleanup, so their objects
stayed in the bucket with no reference from the database. This command finds
them once and puts them into the existing background cleanup. What counts as an
orphan, and why ``homework/`` is never touched:
``src/course_supporter/storage/orphans.md``.

Usage::

    docker exec -it course-supporter-app python -m scripts.storage_admin orphans ...
    uv run python -m scripts.storage_admin orphans ...                 # locally

On production ALWAYS first ``--dry-run`` (counts and sizes only); the real run
is a separate decision of the administrator:

    orphans --actor <who> [--min-age-hours 48] [--dry-run]

The real run deletes nothing itself: it queues the keys of the orphans through
``enqueue_s3_cleanup`` and the worker deletes them. Keys are never printed or
logged here. Running it again after the cleanup finds 0.

Exit status: 0 on success; 2 usage error; 3 some cleanup job was created but
ARQ did not take it (see the count in the output; run the command again).
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from course_supporter.config import get_settings
from course_supporter.storage.orphans import (
    DEFAULT_MIN_AGE,
    Category,
    OrphanReport,
    run_orphans,
)
from course_supporter.storage.s3 import S3Client

EXIT_NOT_DISPATCHED = 3

Infrastructure = Callable[
    [bool],
    AbstractAsyncContextManager[
        tuple[async_sessionmaker[AsyncSession], S3Client, ArqRedis | None]
    ],
]


# ── Output ─────────────────────────────────────────────────────────────


def _size(total: int) -> str:
    return f"{total} B ({total / 1024 / 1024:.1f} MiB)"


def render(report: OrphanReport, *, dry_run: bool, min_age: timedelta) -> str:
    """The report as the administrator reads it: counts and sizes, no keys."""
    hours = min_age.total_seconds() / 3600
    head = "dry run: nothing written, nothing queued" if dry_run else "queued"
    lines = [
        f"storage orphans  {head}",
        f"scanned {report.scanned} object(s), homework/ not listed; "
        f"minimum age {hours:g} h",
        f"not orphans: {report.referenced} referenced by a live row, "
        f"{report.live_node} of a live node, {report.too_young} too young, "
        f"{report.unresolved} undetermined (the key names no node)",
        "orphans (no live reference; the node of the key deleted or missing):",
    ]
    for category in Category:
        totals = report.by_category[category]
        lines.append(
            f"  {category.value:<9} {totals.objects:>6} object(s)  "
            f"{_size(totals.bytes)}"
        )
    lines.append(
        f"  {'total':<9} {report.orphans:>6} object(s)  {_size(report.orphan_bytes)}"
    )
    tenants = f"tenants: {len(report.tenants)}"
    if report.without_tenant:
        tenants += f"  (+ {report.without_tenant} object(s) with no tenant known)"
    lines.append(tenants)
    if not dry_run:
        lines.append(
            f"cleanup jobs created: {report.jobs}"
            + (
                f"  NOT DISPATCHED: {report.jobs_not_dispatched} "
                "(run the command again)"
                if report.jobs_not_dispatched
                else ""
            )
        )
    return "\n".join(lines)


# ── Running ────────────────────────────────────────────────────────────


@asynccontextmanager
async def _infrastructure(
    with_arq: bool,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], S3Client, ArqRedis | None]]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    arq = (
        await create_pool(RedisSettings.from_dsn(settings.redis_url))
        if with_arq
        else None
    )
    try:
        async with S3Client(
            endpoint_url=settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key.get_secret_value(),
            bucket=settings.s3_bucket,
        ) as s3:
            yield async_sessionmaker(engine, expire_on_commit=False), s3, arq
    finally:
        if arq is not None:
            await arq.aclose()
        await engine.dispose()


async def run(
    args: argparse.Namespace, infrastructure: Infrastructure = _infrastructure
) -> tuple[str, int]:
    """Run one command; its output and exit status.

    ``infrastructure`` is the seam the tests use: it is given ``with_arq`` (False
    on a dry run, which opens no queue connection at all) and yields the session
    factory, the S3 client and the queue.
    """
    min_age = timedelta(hours=args.min_age_hours)
    async with infrastructure(not args.dry_run) as (factory, s3, arq):
        report = await run_orphans(factory, s3, arq, actor=args.actor, min_age=min_age)
    code = EXIT_NOT_DISPATCHED if report.jobs_not_dispatched else 0
    return render(report, dry_run=args.dry_run, min_age=min_age), code


def _non_negative_hours(value: str) -> float:
    try:
        hours = float(value)
    except ValueError:
        hours = -1.0
    if not (math.isfinite(hours) and hours >= 0):
        msg = f"{value!r} is not a number of hours, 0 or more"
        raise argparse.ArgumentTypeError(msg)
    return hours


def build_parser() -> argparse.ArgumentParser:
    """The command line (module docstring)."""
    parser = argparse.ArgumentParser(description="Storage administration CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "orphans", help="queue the objects no row refers to for the cleanup"
    )
    p.add_argument("--actor", required=True, help="who runs it")
    p.add_argument(
        "--min-age-hours",
        type=_non_negative_hours,
        default=DEFAULT_MIN_AGE.total_seconds() / 3600,
        help="leave out objects younger than this (default: %(default)g)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="count and sum only; write nothing, queue nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the command, print its result."""
    args = build_parser().parse_args(argv)
    out, code = asyncio.run(run(args))
    sys.stdout.write(out + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
