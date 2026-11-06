"""The four periodic tasks of billing (BIL-B part 2), for the scheduler.

Each body calls the billing function that does the work and adds nothing to
its logic; the lock, the transaction and the log line are the runner's
(``course_supporter.periodic.runner``).

    billing_expire          — :func:`.expiry.expire_batches`: the free units of
                              expired batches burn. Hourly.
    billing_annul           — :func:`.expiry.annul_differences`: differences
                              past ``annul_after`` are annulled. Hourly.
    billing_stale_reserves  — :func:`.stale_reserves.release_stale_reservations`:
                              active reserves older than
                              ``billing_stale_reserve_hours`` are released, each
                              with a WARNING. Every 15 minutes.
    billing_purge           — :mod:`.retention`: accounts anonymized more than
                              ``billing_retention_years`` ago are destroyed, in
                              batches of ``billing_purge_batch_size`` accounts,
                              one transaction each; an account row another
                              account still points at stays as a reference
                              (``kept_as_reference`` in the INFO line). Nightly
                              (UTC).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

import structlog

from course_supporter.billing import retention
from course_supporter.billing.expiry import annul_differences, expire_batches
from course_supporter.billing.stale_reserves import release_stale_reservations
from course_supporter.periodic.runner import (
    PeriodicTask,
    TaskBusyError,
    TaskContext,
    TaskResult,
)

if TYPE_CHECKING:
    from course_supporter.config import Settings

log = structlog.get_logger()


async def _expire(run: TaskContext) -> TaskResult:
    async with run.transaction() as session:
        burned = await expire_batches(session, run.now)
    return TaskResult(len(burned), {"units": sum(b.units for b in burned)})


async def _annul(run: TaskContext) -> TaskResult:
    async with run.transaction() as session:
        annulled = await annul_differences(session, run.now)
    return TaskResult(len(annulled), {"units": sum(a.units for a in annulled)})


async def _stale_reserves(run: TaskContext) -> TaskResult:
    threshold = timedelta(hours=run.settings.billing_stale_reserve_hours)
    async with run.transaction() as session:
        released = await release_stale_reservations(
            session, run.now, older_than=threshold
        )
    for done in released:
        log.warning(
            "billing_stale_reserve_released",
            reservation_id=str(done.reservation_id),
            account_id=str(done.account_id),
            action_kind=done.action_kind.value,
            action_key=done.action_key,
            released_units=done.released_units,
            age_hours=round((run.now - done.created_at).total_seconds() / 3600, 1),
            threshold_hours=run.settings.billing_stale_reserve_hours,
            dry_run=run.dry_run,
        )
    return TaskResult(len(released), {"units": sum(d.released_units for d in released)})


async def _purge(run: TaskContext) -> TaskResult:
    cutoff = retention.retention_cutoff(run.now, run.settings.billing_retention_years)
    after: uuid.UUID | None = None
    rows = accounts = kept = blocked = 0
    while True:
        try:
            async with run.transaction() as session:
                due = await retention.accounts_due(
                    session,
                    cutoff=cutoff,
                    after=after,
                    limit=run.settings.billing_purge_batch_size,
                )
                outcome = await retention.purge_accounts(session, due, cutoff=cutoff)
        except TaskBusyError:
            if after is None:
                raise  # nothing done yet: the whole run is skipped
            break  # another run took over between two batches
        if not due:
            break
        after = due[-1]
        rows += outcome.rows
        accounts += len(outcome.destroyed)
        kept += len(outcome.kept)
        blocked += len(outcome.blocked)
        for stuck in outcome.blocked:
            log.warning(
                "billing_purge_blocked",
                account_id=str(stuck.account_id),
                constraint=stuck.constraint,
                dry_run=run.dry_run,
            )
    return TaskResult(
        rows,
        {"accounts": accounts, "kept_as_reference": kept, "blocked": blocked},
    )


def _expire_enabled(s: Settings) -> bool:
    return s.periodic_billing_expire_enabled


def _expire_cron(s: Settings) -> str:
    return s.periodic_billing_expire_cron


def _annul_enabled(s: Settings) -> bool:
    return s.periodic_billing_annul_enabled


def _annul_cron(s: Settings) -> str:
    return s.periodic_billing_annul_cron


def _stale_enabled(s: Settings) -> bool:
    return s.periodic_billing_stale_reserves_enabled


def _stale_cron(s: Settings) -> str:
    return s.periodic_billing_stale_reserves_cron


def _purge_enabled(s: Settings) -> bool:
    return s.periodic_billing_purge_enabled


def _purge_cron(s: Settings) -> str:
    return s.periodic_billing_purge_cron


BILLING_EXPIRE = PeriodicTask(
    name="billing_expire",
    body=_expire,
    enabled=_expire_enabled,
    cron=_expire_cron,
    summary="burn the free units of expired batches",
)
BILLING_ANNUL = PeriodicTask(
    name="billing_annul",
    body=_annul,
    enabled=_annul_enabled,
    cron=_annul_cron,
    summary="annul differences whose term has passed",
)
BILLING_STALE_RESERVES = PeriodicTask(
    name="billing_stale_reserves",
    body=_stale_reserves,
    enabled=_stale_enabled,
    cron=_stale_cron,
    summary="release active reserves older than the threshold",
)
BILLING_PURGE = PeriodicTask(
    name="billing_purge",
    body=_purge,
    enabled=_purge_enabled,
    cron=_purge_cron,
    summary="destroy accounts anonymized longer ago than the retention period",
)

BILLING_TASKS: tuple[PeriodicTask, ...] = (
    BILLING_EXPIRE,
    BILLING_ANNUL,
    BILLING_STALE_RESERVES,
    BILLING_PURGE,
)
