"""Student administration CLI (Б-2): delete a student's account on request.

Terms and privacy policy 1.2 (in force from 2026-12-09): a student asks
support to delete their account; within 30 days the platform deletes the
login, the name and the e-mail, and keeps the work and its reviews without a
link to the person. The request arrives by e-mail; the administrator runs
this command in the application container. What it erases and what it keeps:
``src/course_supporter/student_deletion/README.md``.

Usage::

    docker exec -it course-supporter-app python -m scripts.student_admin <command> ...
    uv run python -m scripts.student_admin <command> ...              # locally

On production ALWAYS first ``--dry-run`` (counts and sums only); the real run
is a separate decision of the administrator:

    delete --tenant <id|name> --student <id> --actor <who> \\
        --reason "<ground, no personal data>" [--dry-run]

    finish-storage --tenant <id|name> --student <id> --actor <who>
        # after a delete whose storage step failed (exit 3): removes the old
        # objects left in the student's submission folders

No money is returned here, and no file's content changes: before the deletion
support offers the student a refund of the unused units through the payment
provider; ``--dry-run`` prints, batch by batch, the unused units and the sum.

Exit status: 0 on success; 1 refused (the message says why; nothing written);
2 usage error; 3 the deletion is committed but an after-commit step (storage or
Redis) failed after its retries — see the ERROR line, run ``finish-storage``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from course_supporter.config import get_settings
from course_supporter.storage.s3 import S3Client
from course_supporter.student_deletion import (
    DeletionRefusedError,
    DeletionReport,
    delete_student,
    finish_storage,
)

EXIT_REFUSED = 1
EXIT_AFTER_COMMIT_FAILED = 3


# ── Output ─────────────────────────────────────────────────────────────


def render(report: DeletionReport) -> str:
    """The report as the administrator reads it: ids, counts and sums only."""
    head = "dry run: nothing written" if report.dry_run else "deleted"
    lines = [f"student {report.student_id}  tenant {report.tenant_id}  {head}"]
    lines.append("would change:" if report.dry_run else "changed:")
    for p in report.places:
        lines.append(f"  {p.place:<30} {p.rows:>5} row(s)  [{p.fields}]")
    if not report.redis_checked:
        lines.append("  (redis not reachable: its counters were not checked)")
    if report.dry_run:
        lines.append(
            f"in progress: {report.unfinished_submissions} unfinished "
            f"submission(s), {report.active_reserves} active reserve(s)"
            + (
                "  -> the real run refuses until they finish"
                if report.unfinished_submissions or report.active_reserves
                else ""
            )
        )
    lines.append(
        "units (refund = amount paid x unused / batch units, rounded down; "
        "through the payment provider, by support):"
    )
    if not report.batches:
        lines.append("  no valid batch")
    for b in report.batches:
        if b.kind == "purchase":
            money = (
                f"paid {b.amount_paid_minor} {b.currency}  "
                f"refund {b.refund_minor} {b.currency} (minor units)"
            )
        else:
            label = "gift (administrator's credit)" if b.kind == "gift" else "test"
            money = f"{label}  refund 0"
        held = f"  held {b.held}" if b.held else ""
        lines.append(
            f"  batch {b.batch_id}  {b.kind:<8} unused {b.unused}/{b.units}"
            f"{held}  {money}"
        )
    totals: dict[str, int] = {}
    for b in report.batches:
        if b.kind == "purchase" and b.currency:
            totals[b.currency] = totals.get(b.currency, 0) + b.refund_minor
    for currency, total in sorted(totals.items()):
        lines.append(f"  total refund {total} {currency} (minor units)")
    if report.same_email_elsewhere:
        lines.append("same confirmed e-mail in other tenants (support decides):")
        lines.extend(
            f"  student {sid}  tenant {tid}" for sid, tid in report.same_email_elsewhere
        )
    else:
        lines.append("same confirmed e-mail in other tenants: none")
    if report.failed_steps:
        lines.append(
            "FAILED after the commit: "
            + ", ".join(report.failed_steps)
            + " (see the ERROR line; storage: run finish-storage)"
        )
    return "\n".join(lines)


# ── Running ────────────────────────────────────────────────────────────


@asynccontextmanager
async def _infrastructure(
    *, with_redis: bool
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], S3Client, Redis | None]]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    redis: Redis | None = Redis.from_url(settings.redis_url) if with_redis else None
    try:
        async with S3Client(
            endpoint_url=settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key.get_secret_value(),
            bucket=settings.s3_bucket,
        ) as s3:
            yield async_sessionmaker(engine, expire_on_commit=False), s3, redis
    finally:
        if redis is not None:
            await redis.aclose()
        await engine.dispose()


async def run(args: argparse.Namespace) -> tuple[str, int]:
    """Run one command; its output and exit status."""
    if args.command == "delete":
        async with _infrastructure(with_redis=True) as (factory, s3, redis):
            report = await delete_student(
                factory,
                s3,
                redis,
                tenant=args.tenant,
                student_id=args.student,
                actor=args.actor,
                reason=args.reason,
                dry_run=args.dry_run,
            )
        code = EXIT_AFTER_COMMIT_FAILED if report.failed_steps else 0
        return render(report), code
    async with _infrastructure(with_redis=False) as (factory, s3, _):
        done = await finish_storage(
            factory, s3, tenant=args.tenant, student_id=args.student, actor=args.actor
        )
    out = f"student {args.student}  removed {done.removed} old object(s)"
    if done.failed_submission_ids:
        failed = ", ".join(str(i) for i in done.failed_submission_ids)
        return f"{out}\nFAILED for submission(s): {failed}", EXIT_AFTER_COMMIT_FAILED
    return out, 0


def build_parser() -> argparse.ArgumentParser:
    """The command line (module docstring)."""
    parser = argparse.ArgumentParser(description="Student administration CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("delete", help="delete a student's account on request")
    p.add_argument("--tenant", required=True, help="tenant id or name")
    p.add_argument("--student", required=True, type=uuid.UUID)
    p.add_argument("--actor", required=True, help="who runs it")
    p.add_argument(
        "--reason",
        required=True,
        help="the ground, e.g. a ticket number; no e-mail address",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="count and sum only; write nothing"
    )

    p = sub.add_parser(
        "finish-storage", help="remove old objects a failed storage step left"
    )
    p.add_argument("--tenant", required=True, help="tenant id or name")
    p.add_argument("--student", required=True, type=uuid.UUID)
    p.add_argument("--actor", required=True, help="who runs it")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the command, print its result."""
    args = build_parser().parse_args(argv)
    try:
        out, code = asyncio.run(run(args))
    except DeletionRefusedError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return EXIT_REFUSED
    sys.stdout.write(out + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
