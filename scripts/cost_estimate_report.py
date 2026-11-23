"""Calibration report: shadow estimates against the call register (BIL-D).

Read-only. For every action with a shadow estimate (``action_cost_estimates``)
it prints E against the fact from ``external_service_calls``, the deviation per
step and the share of calls whose cost is unknown. The fact is the payer's —
the rows of ``billing.charging.PAID_BY_PAYER``, the definition the journal
charges by. The arithmetic and the rule of which rows belong to which action
are in ``course_supporter.cost_estimate.report``.

Test students (BIL-5, DD-BIL-CAL): the review of a submission whose student is
marked as a service student (``students.is_test_student``; the mark is set by
``scripts/billing_admin.py test-student``) is left out by default;
``--include-test-students`` brings it back. The estimates of the other actions
are not filtered. After the report a line says how many review estimates of
test students were excluded (or included) and how many distinct non-test
students the review sample has. ``--limit`` counts the estimates before the
filter.

Usage::

    uv run python scripts/cost_estimate_report.py              # table
    uv run python scripts/cost_estimate_report.py --csv out.csv
    uv run python scripts/cost_estimate_report.py --kind review --limit 50
    uv run python scripts/cost_estimate_report.py --include-test-students

Reads ``DATABASE_URL`` (the app settings) and the catalog
(``config/external_services.yaml``). Opens a READ ONLY transaction.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import uuid
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from course_supporter.billing.charging import PAID_BY_PAYER
from course_supporter.config import get_settings
from course_supporter.cost_estimate.report import (
    CallRow,
    EstimateRow,
    ReportRow,
    ReviewSample,
    compare,
    rows_of,
    select_reviews,
)
from course_supporter.llm.registry import load_registry
from course_supporter.storage.orm import (
    ActionCostEstimate,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    Student,
)

_COLUMNS = (
    "action_kind",
    "job_type",
    "action_key",
    "e_usd",
    "fact_usd",
    "deviation_usd",
    "calls",
    "unknown_calls",
    "unknown_share",
    "step_deviation",
)


async def _review_students(
    session: AsyncSession, estimates: list[EstimateRow]
) -> dict[str, tuple[uuid.UUID, bool]]:
    """Each reviewed submission's student and the student's test mark."""
    reviewed = [uuid.UUID(e.action_key) for e in estimates if e.action_kind == "review"]
    if not reviewed:
        return {}
    rows = await session.execute(
        select(HomeworkSubmission.id, Student.id, Student.is_test_student)
        .join(Student, Student.id == HomeworkSubmission.student_id)
        .where(HomeworkSubmission.id.in_(reviewed))
    )
    return {str(sub): (student, is_test) for sub, student, is_test in rows}


async def _load(
    session: AsyncSession,
    kind: str | None,
    limit: int | None,
    *,
    include_test_students: bool = False,
) -> tuple[list[ReportRow], ReviewSample]:
    query = select(ActionCostEstimate).order_by(ActionCostEstimate.created_at)
    if kind is not None:
        query = query.where(ActionCostEstimate.action_kind == kind)
    if limit is not None:
        query = query.limit(limit)
    stored = list((await session.execute(query)).scalars())
    # What kind of job ran each action: a processing estimate is a material's
    # pipeline or a regeneration of node summaries (``Job.job_type``).
    job_types: dict[uuid.UUID, str] = {}
    estimated_jobs = {e.job_id for e in stored if e.job_id is not None}
    if estimated_jobs:
        rows = await session.execute(
            select(Job.id, Job.job_type).where(Job.id.in_(estimated_jobs))
        )
        job_types = {job_id: job_type for job_id, job_type in rows}
    estimates = [
        EstimateRow(
            action_kind=e.action_kind,
            action_key=e.action_key,
            job_id=e.job_id,
            e_usd=e.e_usd,
            breakdown=e.breakdown,
            created_at=e.created_at,
            job_type=job_types.get(e.job_id) if e.job_id is not None else None,
        )
        for e in stored
    ]
    estimates, sample = select_reviews(
        estimates,
        await _review_students(session, estimates),
        include_test_students=include_test_students,
    )

    # The jobs of each reviewed submission (their subject is the submission).
    submission_jobs: dict[str, list[uuid.UUID]] = defaultdict(list)
    reviewed = [uuid.UUID(e.action_key) for e in estimates if e.action_kind == "review"]
    if reviewed:
        jobs = await session.execute(
            select(Job.id, Job.subject_id).where(
                Job.subject_type == "homework_submission",
                Job.subject_id.in_(reviewed),
            )
        )
        for job_id, subject_id in jobs:
            submission_jobs[str(subject_id)].append(job_id)

    job_ids = {e.job_id for e in estimates if e.job_id is not None}
    job_ids |= {j for jobs in submission_jobs.values() for j in jobs}
    by_job: dict[uuid.UUID, list[CallRow]] = defaultdict(list)
    if job_ids:
        esc = ExternalServiceCall
        # The payer's rows only: the fact E is calibrated against is the one
        # the journal charges (operator 2026-11-17).
        calls = await session.execute(
            select(esc).where(
                esc.job_id.in_(job_ids), esc.provider.is_not(None), PAID_BY_PAYER
            )
        )
        for c in calls.scalars():
            by_job[c.job_id].append(
                CallRow(
                    job_id=c.job_id,
                    action=c.action,
                    provider=c.provider,
                    model_id=c.model_id,
                    cost_usd=c.cost_usd,
                    cost_kind=c.cost_kind,
                    unit_in=c.unit_in,
                    unit_in_estimate=c.unit_in_estimate,
                    unit_out=c.unit_out,
                    unit_out_estimate=c.unit_out_estimate,
                    call_started_at=c.call_started_at,
                )
            )

    models = load_registry(get_settings().external_services_path).models
    report = [
        compare(
            e,
            rows_of(e, by_job=by_job, submission_jobs=submission_jobs),
            models=models,
        )
        for e in estimates
    ]
    return report, sample


async def _main(args: argparse.Namespace) -> tuple[list[ReportRow], ReviewSample]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with AsyncSession(engine) as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            return await _load(
                session,
                args.kind,
                args.limit,
                include_test_students=args.include_test_students,
            )
    finally:
        await engine.dispose()


def _print(rows: list[ReportRow]) -> None:
    """The report as an aligned table on stdout (the report IS the output)."""
    lines = [r.as_csv() for r in rows]
    widths = {c: len(c) for c in _COLUMNS[:-1]}
    for line in lines:
        for c in widths:
            widths[c] = max(widths[c], len(line[c]))
    out = ["  ".join(c.ljust(w) for c, w in widths.items()) + "  step_deviation"]
    for line in lines:
        cells = "  ".join(line[c].ljust(w) for c, w in widths.items())
        out.append(f"{cells}  {line['step_deviation']}")
    sys.stdout.write("\n".join(out) + "\n")


def main(argv: list[str] | None = None) -> int:
    """Entry point: print the report or write it as CSV."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, help="write the report here")
    parser.add_argument("--kind", choices=("review", "processing", "task_preparation"))
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--include-test-students",
        action="store_true",
        help="keep the reviews of test students (students.is_test_student)",
    )
    args = parser.parse_args(argv)
    rows, sample = asyncio.run(_main(args))
    if args.csv is not None:
        with args.csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_COLUMNS)
            writer.writeheader()
            writer.writerows(r.as_csv() for r in rows)
    else:
        _print(rows)
    sys.stdout.write(sample.line() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
