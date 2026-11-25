"""Delete a student's account at the student's request (Б-2, terms 1.2).

What is erased, what is kept (step 1 of Б-2, ``README.md`` beside this file):

* **Sign-in and person** — the login becomes ``deleted-<student id>``, the
  password hash one that matches no password, the e-mail addresses go, the
  one-use tokens and the self-enrollment requests carrying the student's
  address are deleted, the credential is switched off; on the student row the
  caller's ``external_id`` becomes ``deleted-<student id>`` and the name,
  metadata and standing preferences go. A portal session is a stateless token
  checked against the credential and ``students.deleted_at`` on every request
  (``api/deps.py``), so it stops working with the commit; nothing to erase.
* **Personal, outside the work** — the comment on a submission, the student's
  replies to remarks, the free text of feedback; the NAME of the submitted
  file (owner's decision of 25.11, option A): the stored object is copied to
  ``…/<submission>/submission.<extension>`` byte for byte, ``file_url``,
  ``original_filename`` and the verdicts' ``quote_file`` follow it, and the old
  key is deleted after the commit.
* **The work** — the file's bytes, test answers, reviews, verdicts, quotes,
  explanations, stage outputs — stays as it is, tied to the student row, which
  is now a marker without personal data (``deleted_at`` set). Submission rows
  keep counting for the author's statistics and for billing.
* **Legal evidence** — consents and the billing audit log (opaque ids only)
  are not touched. The student's billing accounts are closed and anonymized
  by :func:`.billing.accounts.anonymize_student_accounts` in the same
  transaction; unused units are not refunded here (``--dry-run`` shows what a
  refund through the payment provider would be).
* **Enrollments** — deleted, as an author's unbind deletes them.

The database part is one transaction; the copies of the renamed files are
made, and checked by SHA-256 against the original, before it commits — a copy
that fails or differs aborts everything. After the commit the old objects and
the student's rate-limit counters in Redis are deleted, with retries; a step
that still fails is an ERROR line (submission ids, never keys) and the
caller's non-zero exit. :func:`finish_storage` repeats the storage step for an
already deleted student.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final

import structlog
from botocore.exceptions import BotoCoreError, ClientError
from redis.exceptions import RedisError
from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.upload_validation import file_extension
from course_supporter.auth.rate_limits import counter_key
from course_supporter.billing.accounts import anonymize_student_accounts
from course_supporter.billing.journal import positions
from course_supporter.billing.kinds import (
    AccountKind,
    BatchSource,
    ReservationState,
    RuleScope,
)
from course_supporter.billing.refunds import refund_amount
from course_supporter.homework.submission_core import (
    ALLOWED_HOMEWORK_EXTENSIONS,
    TEST_ANSWERS_FILENAME,
)
from course_supporter.remark_kinds import CommentAuthor
from course_supporter.storage.orm import (
    BillingAccount,
    BillingPaymentRule,
    BillingReservation,
    BillingTestBatch,
    HomeworkStatus,
    HomeworkSubmission,
    PortalSignup,
    RemarkComment,
    Student,
    StudentCredential,
    StudentCredentialToken,
    StudentEnrollment,
    StudentFeedback,
    SubmissionCriterionVerdict,
    Tenant,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger(__name__)

DELETED_TEXT: Final = "[deleted at the student's request]"
"""What the student's own texts become where a text is required (a reply)."""

NEUTRAL_STEM: Final = "submission"
"""The stem of a renamed submission file: ``submission.<extension>``."""

UNUSABLE_PASSWORD_HASH: Final = "!deleted"  # noqa: S105 - a marker, no secret
"""Not an argon2 hash: :func:`.auth.student_passwords.verify_password` answers
False for every password."""

FINISHED_STATUSES: Final = frozenset(
    {
        HomeworkStatus.COMPLETED.value,
        HomeworkStatus.DELIVERED.value,
        HomeworkStatus.REJECTED.value,
        HomeworkStatus.MISMATCH.value,
        HomeworkStatus.FAILED.value,
    }
)
"""A submission in any other status — under way or held (awaiting funds or
criteria, which a top-up or a continuation would resume) — refuses the
deletion: its resumption would need a live payer."""

EMAIL_COUNTERS: Final = ("login_by_email_email",)
"""Rate-limit counters keyed by an e-mail (``api/routes/portal_join.py``)."""

STUDENT_COUNTERS: Final = ("join_course_student",)
"""Rate-limit counters keyed by the student id (``api/routes/portal_join.py``)."""

RETRY_DELAYS_S: tuple[float, ...] = (1.0, 3.0, 9.0)
"""Pauses between the attempts of an after-commit step (len + 1 attempts)."""

_DELETED: Final = "deleted"
"""``PlaceCount.fields`` of a place whose rows are deleted."""


class DeletionRefusedError(Exception):
    """The deletion is refused; nothing was written. The message says why and
    names no person."""


# ── Results ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PlaceCount:
    """How many rows of one place change (or would change)."""

    place: str
    fields: str
    """The fields written, comma-separated, or ``deleted``."""
    rows: int


@dataclass(frozen=True, slots=True)
class BatchRefund:
    """One valid batch of the student's: its unused units and what returning
    them through the payment provider would be (decision 4 of Б-2)."""

    batch_id: uuid.UUID
    account_id: uuid.UUID
    kind: str
    """``purchase``; ``gift`` (an administrator's credit, no money paid);
    ``test`` (marked as a test purchase). Only a purchase is refunded."""
    units: int
    unused: int
    """Free units: neither spent, reserved nor held for a refund."""
    held: int
    amount_paid_minor: int | None
    currency: str | None
    refund_minor: int
    """amount paid x unused / units, rounded down; 0 for a gift or a test."""


@dataclass(slots=True)
class DeletionReport:
    """What a run did, or (dry) would do."""

    tenant_id: uuid.UUID
    student_id: uuid.UUID
    dry_run: bool
    places: list[PlaceCount]
    unfinished_submissions: int
    active_reserves: int
    batches: list[BatchRefund]
    same_email_elsewhere: list[tuple[uuid.UUID, uuid.UUID]]
    """(student id, tenant id) of live students elsewhere whose confirmed
    e-mail is one of this student's."""
    redis_checked: bool = True
    failed_steps: list[str] = field(default_factory=list)
    """After-commit steps that failed after every retry: ``storage``, ``redis``."""

    def rows(self, place: str) -> int:
        """Rows of ``place`` (0 when it is not in the report)."""
        return sum(p.rows for p in self.places if p.place == place)


@dataclass(frozen=True, slots=True)
class StorageCleanup:
    """What :func:`finish_storage` removed, and where it failed."""

    removed: int
    failed_submission_ids: list[uuid.UUID]


@dataclass(frozen=True, slots=True)
class _Rename:
    submission_id: uuid.UUID
    old_key: str
    new_key: str
    old_names: frozenset[str]
    """The file's names a verdict's ``quote_file`` can carry."""
    new_name: str


@dataclass(frozen=True, slots=True)
class _Place:
    place: str
    fields: str
    model: Any
    where: tuple[Any, ...]
    values: dict[Any, Any] | None
    """None — the rows are deleted."""


@dataclass(slots=True)
class _Context:
    tenant_id: uuid.UUID
    student: Student
    places: list[_Place]
    renames: list[_Rename]
    quote_files: int
    account_ids: list[uuid.UUID]
    student_rules: int
    unfinished: int
    reserves: int
    batches: list[BatchRefund]
    same_email_elsewhere: list[tuple[uuid.UUID, uuid.UUID]]
    redis_keys: list[str]


# ── The file name ──────────────────────────────────────────────────────


def neutral_name(original: str) -> str:
    """``submission`` + the original's extension when the upload door accepts
    it (``.tar.gz`` whole); an unknown extension is dropped (it could be a
    word of the name)."""
    if original.lower().endswith(".tar.gz"):
        return f"{NEUTRAL_STEM}.tar.gz"
    extension = file_extension(original)
    if extension in ALLOWED_HOMEWORK_EXTENSIONS:
        return NEUTRAL_STEM + extension
    return NEUTRAL_STEM


def neutral_key(key: str, original_filename: str | None) -> tuple[str, str] | None:
    """The neutral key and name for a submission's stored file, or None when
    the key carries no name of the student's.

    ``homework/<tenant>/<id>/<the file's name>`` becomes
    ``homework/<tenant>/<id>/submission.<extension>``. A test's answers are
    stored under the platform's own name (``answers.json``) and keep it.
    """
    name = PurePosixPath(key).name
    if original_filename == TEST_ANSWERS_FILENAME and name == TEST_ANSWERS_FILENAME:
        return None
    new_name = neutral_name(original_filename or name)
    parts = key.split("/", 3)
    if len(parts) == 4 and parts[0] == "homework":
        new_key = "/".join([*parts[:3], new_name])
    else:
        new_key = str(PurePosixPath(key).parent / new_name)
    if new_key == key:
        return None
    return new_key, new_name


# ── Checks ─────────────────────────────────────────────────────────────


def check_reason(reason: str) -> str:
    """The reason, stripped; refused when empty or when it holds an address."""
    stripped = reason.strip()
    if not stripped:
        raise DeletionRefusedError("the reason is empty")
    if "@" in stripped:
        raise DeletionRefusedError(
            "the reason holds '@': write the ground (a ticket number), "
            "not an e-mail address"
        )
    return stripped


async def resolve_tenant(session: AsyncSession, value: str) -> uuid.UUID:
    """The tenant's id from its id or its name."""
    try:
        return uuid.UUID(value)
    except ValueError:
        pass
    tenant_id = (
        await session.execute(select(Tenant.id).where(Tenant.name == value))
    ).scalar_one_or_none()
    if tenant_id is None:
        raise DeletionRefusedError(f"tenant {value!r} not found")
    return tenant_id


async def _load_student(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    student_id: uuid.UUID,
    *,
    lock: bool,
) -> Student:
    student = await session.get(Student, student_id, with_for_update=lock)
    if student is None:
        raise DeletionRefusedError(f"student {student_id} not found")
    if student.tenant_id != tenant_id:
        raise DeletionRefusedError(
            f"student {student_id} belongs to another tenant, not {tenant_id}"
        )
    return student


def _refuse_deleted(student: Student) -> None:
    if student.deleted_at is not None:
        raise DeletionRefusedError(
            f"student {student.id} already deleted at "
            f"{student.deleted_at.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC; "
            "nothing changed"
        )


# ── What would change ──────────────────────────────────────────────────


async def _count(session: AsyncSession, place: _Place) -> int:
    stmt = select(func.count()).select_from(place.model).where(*place.where)
    return int((await session.execute(stmt)).scalar_one())


def _addresses(values: Sequence[str | None]) -> set[str]:
    """Every address as stored and lower-cased (a counter's subject is the
    address as typed; the stored ones are already lower-cased)."""
    found: set[str] = set()
    for value in values:
        if value:
            found |= {value, value.lower()}
    return found


async def _batches(
    session: AsyncSession, account_ids: list[uuid.UUID], now: datetime
) -> list[BatchRefund]:
    found: list[BatchRefund] = []
    for account_id in account_ids:
        valid = [p for p in await positions(session, account_id) if p.valid(now)]
        ids = [p.batch.id for p in valid]
        marked = set(
            (
                await session.execute(
                    select(BillingTestBatch.batch_id).where(
                        BillingTestBatch.batch_id.in_(ids)
                    )
                )
            ).scalars()
        )
        for p in valid:
            batch = p.batch
            unused = max(p.free, 0)
            if batch.id in marked:
                kind, refund = "test", 0
            elif batch.source != BatchSource.PURCHASE.value or (
                batch.amount_paid_minor is None
            ):
                kind, refund = "gift", 0
            else:
                kind = "purchase"
                refund = refund_amount(batch, unused) if unused > 0 else 0
            found.append(
                BatchRefund(
                    batch_id=batch.id,
                    account_id=account_id,
                    kind=kind,
                    units=batch.units,
                    unused=unused,
                    held=p.held,
                    amount_paid_minor=batch.amount_paid_minor,
                    currency=batch.currency,
                    refund_minor=refund,
                )
            )
    return found


async def _scalar_count(session: AsyncSession, *where: Any) -> int:
    return int((await session.execute(select(func.count()).where(*where))).scalar_one())


async def _addresses_of(
    session: AsyncSession, student_id: uuid.UUID
) -> tuple[set[str], set[str]]:
    """The student's e-mail addresses: all (as stored and lower-cased), and
    the confirmed ones (lower-cased) — the recovery address once confirmed and
    the address of the signup the student came from."""
    credential = (
        await session.execute(
            select(StudentCredential).where(StudentCredential.student_id == student_id)
        )
    ).scalar_one_or_none()
    signup_emails = list(
        (
            await session.execute(
                select(PortalSignup.email).where(PortalSignup.student_id == student_id)
            )
        ).scalars()
    )
    recovery = credential.recovery_email if credential is not None else None
    confirmed = {e.lower() for e in signup_emails}
    if recovery and credential is not None and credential.recovery_email_confirmed_at:
        confirmed.add(recovery.lower())
    return _addresses([recovery, *signup_emails]), confirmed


async def _same_email_elsewhere(
    session: AsyncSession, tenant_id: uuid.UUID, confirmed: set[str]
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    if not confirmed:
        return []
    rows = await session.execute(
        select(StudentCredential.student_id, StudentCredential.tenant_id)
        .join(Student, Student.id == StudentCredential.student_id)
        .where(
            func.lower(StudentCredential.recovery_email).in_(sorted(confirmed)),
            StudentCredential.recovery_email_confirmed_at.isnot(None),
            StudentCredential.tenant_id != tenant_id,
            Student.deleted_at.is_(None),
        )
        .order_by(StudentCredential.tenant_id, StudentCredential.student_id)
    )
    return [(s, t) for s, t in rows.tuples()]


def _renames(s3: S3Client, submissions: list[HomeworkSubmission]) -> list[_Rename]:
    renames: list[_Rename] = []
    for submission in submissions:
        if submission.deleted_at is not None:
            # Only a cascade from the student or the tenant soft-deletes a
            # submission; under a live student it is an inconsistency, and the
            # soft-delete trigger would refuse the scrub.
            raise DeletionRefusedError(
                f"submission {submission.id} is soft-deleted under a live "
                "student; nothing changed"
            )
        key = s3.extract_key(submission.file_url)
        if key is None:
            raise DeletionRefusedError(
                f"submission {submission.id}: its file is not in this storage; "
                "nothing changed"
            )
        renamed = neutral_key(key, submission.original_filename)
        if renamed is None:
            continue
        old_names = {PurePosixPath(key).name}
        if submission.original_filename:
            old_names.add(submission.original_filename)
        renames.append(
            _Rename(
                submission_id=submission.id,
                old_key=key,
                new_key=renamed[0],
                old_names=frozenset(old_names),
                new_name=renamed[1],
            )
        )
    return renames


def _places(
    tenant_id: uuid.UUID, sid: uuid.UUID, addresses: set[str], now: datetime
) -> list[_Place]:
    """Every row-level change, in the order it is written: the student row
    last, since the soft-delete trigger freezes it once ``deleted_at`` is set."""
    marker = f"deleted-{sid}"
    signups_of = PortalSignup.student_id == sid
    if addresses:
        signups_of = or_(
            signups_of,
            and_(
                PortalSignup.tenant_id == tenant_id,
                PortalSignup.confirmed_at.is_(None),
                PortalSignup.email.in_(sorted(addresses)),
            ),
        )
    credential_ids = select(StudentCredential.id).where(
        StudentCredential.student_id == sid
    )
    return [
        _Place(
            "student_credential_tokens",
            _DELETED,
            StudentCredentialToken,
            (StudentCredentialToken.credential_id.in_(credential_ids),),
            None,
        ),
        _Place(
            "student_credentials",
            "login, password_hash, is_active, recovery_email, "
            "recovery_email_confirmed_at",
            StudentCredential,
            (StudentCredential.student_id == sid,),
            {
                StudentCredential.login: marker,
                StudentCredential.password_hash: UNUSABLE_PASSWORD_HASH,
                StudentCredential.is_active: False,
                StudentCredential.recovery_email: None,
                StudentCredential.recovery_email_confirmed_at: None,
            },
        ),
        _Place("portal_signups", _DELETED, PortalSignup, (signups_of,), None),
        _Place(
            "student_enrollments",
            _DELETED,
            StudentEnrollment,
            (StudentEnrollment.student_id == sid,),
            None,
        ),
        _Place(
            "homework_submissions",
            "student_note",
            HomeworkSubmission,
            (
                HomeworkSubmission.student_id == sid,
                HomeworkSubmission.student_note.isnot(None),
            ),
            {HomeworkSubmission.student_note: None},
        ),
        _Place(
            "remark_comments",
            "body (the student's replies)",
            RemarkComment,
            (
                RemarkComment.student_id == sid,
                RemarkComment.author == CommentAuthor.STUDENT.value,
                RemarkComment.body != DELETED_TEXT,
            ),
            {RemarkComment.body: DELETED_TEXT},
        ),
        _Place(
            "student_feedback",
            "text",
            StudentFeedback,
            (StudentFeedback.student_id == sid, StudentFeedback.text.isnot(None)),
            {StudentFeedback.text: None},
        ),
        _Place(
            "students",
            "external_id, display_name, metadata, mentor_preferences, deleted_at",
            Student,
            (Student.id == sid,),
            {
                Student.external_id: marker,
                Student.display_name: None,
                Student.metadata_: None,
                Student.mentor_preferences: None,
                Student.deleted_at: now,
            },
        ),
    ]


async def _gather(
    session: AsyncSession,
    s3: S3Client,
    tenant_id: uuid.UUID,
    student: Student,
    now: datetime,
) -> _Context:
    """Everything the run writes, and what the dry run reports, read once."""
    sid = student.id
    addresses, confirmed = await _addresses_of(session, sid)
    submissions = list(
        (
            await session.execute(
                select(HomeworkSubmission)
                .where(HomeworkSubmission.student_id == sid)
                .order_by(HomeworkSubmission.created_at, HomeworkSubmission.id)
            )
        ).scalars()
    )
    renames = _renames(s3, submissions)
    account_ids = list(
        (
            await session.execute(
                select(BillingAccount.id)
                .where(
                    BillingAccount.kind == AccountKind.STUDENT.value,
                    BillingAccount.student_id == sid,
                    BillingAccount.anonymized_at.is_(None),
                )
                .order_by(BillingAccount.id)
            )
        ).scalars()
    )
    return _Context(
        tenant_id=tenant_id,
        student=student,
        places=_places(tenant_id, sid, addresses, now),
        renames=renames,
        quote_files=sum(
            [await _scalar_count(session, *_quote_file_of(r)) for r in renames]
        ),
        account_ids=account_ids,
        student_rules=await _scalar_count(
            session,
            BillingPaymentRule.active.is_(True),
            BillingPaymentRule.scope == RuleScope.STUDENT.value,
            BillingPaymentRule.student_id == sid,
        ),
        unfinished=sum(1 for s in submissions if s.status not in FINISHED_STATUSES),
        reserves=await _scalar_count(
            session,
            BillingReservation.account_id.in_(account_ids),
            BillingReservation.state == ReservationState.ACTIVE.value,
        ),
        batches=await _batches(session, account_ids, now),
        same_email_elsewhere=await _same_email_elsewhere(session, tenant_id, confirmed),
        redis_keys=[
            counter_key(name, address)
            for name in EMAIL_COUNTERS
            for address in sorted(addresses)
        ]
        + [counter_key(name, str(sid)) for name in STUDENT_COUNTERS],
    )


def _quote_file_of(rename: _Rename) -> tuple[Any, ...]:
    return (
        SubmissionCriterionVerdict.submission_id == rename.submission_id,
        SubmissionCriterionVerdict.quote_file.in_(sorted(rename.old_names)),
    )


def _report_places(
    counted: list[tuple[_Place, int]],
    *,
    renamed: int,
    quote_files: int,
    accounts: int,
    rules: int,
    counters: int,
) -> list[PlaceCount]:
    places = [PlaceCount(p.place, p.fields, n) for p, n in counted]
    at = next(i for i, p in enumerate(places) if p.place == "students")
    places[at:at] = [
        PlaceCount(
            "homework_submissions",
            "original_filename, file_url (file renamed)",
            renamed,
        ),
        PlaceCount("submission_criterion_verdicts", "quote_file", quote_files),
    ]
    places += [
        PlaceCount("billing_accounts", "closed and anonymized", accounts),
        PlaceCount("billing_payment_rules", "student rules ended", rules),
        PlaceCount("storage", "objects renamed", renamed),
        PlaceCount("redis", "rate-limit counters deleted", counters),
    ]
    return places


# ── Writing ────────────────────────────────────────────────────────────


async def _write(
    session: AsyncSession, context: _Context, now: datetime
) -> list[tuple[_Place, int]]:
    written: list[tuple[_Place, int]] = []
    for place in context.places:
        if place.values is None:
            stmt: Any = delete(place.model).where(*place.where)
        else:
            stmt = update(place.model).where(*place.where).values(place.values)
        result: Any = await session.execute(
            stmt.execution_options(synchronize_session=False)
        )
        written.append((place, int(result.rowcount)))
        if place.model is Student:
            await session.refresh(context.student)
    return written


async def _rename_rows(
    session: AsyncSession, s3: S3Client, renames: list[_Rename]
) -> int:
    quote_files = 0
    for rename in renames:
        await session.execute(
            update(HomeworkSubmission)
            .where(HomeworkSubmission.id == rename.submission_id)
            .values(
                file_url=s3.get_object_url(rename.new_key),
                original_filename=rename.new_name,
            )
            .execution_options(synchronize_session=False)
        )
        result: Any = await session.execute(
            update(SubmissionCriterionVerdict)
            .where(*_quote_file_of(rename))
            .values(quote_file=rename.new_name)
            .execution_options(synchronize_session=False)
        )
        quote_files += int(result.rowcount)
    return quote_files


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _copy_files(
    s3: S3Client, renames: list[_Rename], copied: list[_Rename]
) -> None:
    """Copy each file to its neutral key and check the copy byte for byte;
    ``copied`` collects what was written, for the caller's undo."""
    for rename in renames:
        try:
            await s3.copy_object(rename.old_key, rename.new_key)
            copied.append(rename)
            original = await s3.get_object(rename.old_key, log_key=False)
            copy = await s3.get_object(rename.new_key)
        except (ClientError, BotoCoreError) as exc:
            raise DeletionRefusedError(
                f"submission {rename.submission_id}: its file could not be "
                f"copied ({type(exc).__name__}); nothing changed"
            ) from exc
        if _sha256(copy) != _sha256(original):
            raise DeletionRefusedError(
                f"submission {rename.submission_id}: the copy of its file "
                "differs from the original; nothing changed"
            )


async def _retrying(step: str, op: Callable[[], Awaitable[object]]) -> bool:
    """Run ``op`` until it succeeds, at most len(RETRY_DELAYS_S) + 1 times."""
    delays: list[float | None] = [*RETRY_DELAYS_S, None]
    for delay in delays:
        try:
            await op()
        except (ClientError, BotoCoreError, RedisError, OSError, TimeoutError) as exc:
            logger.warning(
                "student_deletion_step_retry", step=step, error=type(exc).__name__
            )
            if delay is None:
                return False
            await asyncio.sleep(delay)
        else:
            return True
    return False  # pragma: no cover - the loop always returns


async def _delete_old_objects(s3: S3Client, renames: list[_Rename]) -> list[uuid.UUID]:
    """Delete the old keys; the submissions whose old key stayed."""
    failed: list[uuid.UUID] = []
    for rename in renames:

        async def _delete(key: str = rename.old_key) -> None:
            await s3.delete_object(key, log_key=False)

        if not await _retrying("storage", _delete):
            failed.append(rename.submission_id)
    return failed


# ── The run ────────────────────────────────────────────────────────────


async def delete_student(
    session_factory: async_sessionmaker[AsyncSession],
    s3: S3Client,
    redis: Redis | None,
    *,
    tenant: str,
    student_id: uuid.UUID,
    actor: str,
    reason: str,
    dry_run: bool,
    now: datetime | None = None,
) -> DeletionReport:
    """Delete the student's account (module docstring), or with ``dry_run``
    count what would change and write nothing (a READ ONLY transaction, no
    storage or Redis write).

    Raises:
        DeletionRefusedError: the run is refused; nothing was written.
    """
    actor = actor.strip()
    if not actor:
        raise DeletionRefusedError("the actor is empty")
    reason = check_reason(reason)
    now = now or datetime.now(UTC)

    if dry_run:
        return await _dry_run(
            session_factory, s3, redis, tenant=tenant, student_id=student_id, now=now
        )

    copied: list[_Rename] = []
    try:
        async with session_factory() as session, session.begin():
            tenant_id = await resolve_tenant(session, tenant)
            student = await _load_student(session, tenant_id, student_id, lock=True)
            _refuse_deleted(student)
            context = await _gather(session, s3, tenant_id, student, now)
            if context.unfinished or context.reserves:
                raise DeletionRefusedError(
                    f"student {student_id} has {context.unfinished} unfinished "
                    f"submission(s) and {context.reserves} active reserve(s); "
                    "run again once they have finished; nothing changed"
                )
            written = await _write(session, context, now)
            quote_files = await _rename_rows(session, s3, context.renames)
            anonymized = await anonymize_student_accounts(
                session, student_id, actor=actor, now=now
            )
            await _copy_files(s3, context.renames, copied)
    except BaseException:
        # Undo the copies; their names are neutral, so one left behind
        # reveals nothing.
        for rename in copied:

            async def _undo(key: str = rename.new_key) -> None:
                await s3.delete_object(key)

            await _retrying("storage_undo", _undo)
        raise

    report = DeletionReport(
        tenant_id=tenant_id,
        student_id=student_id,
        dry_run=False,
        places=[],
        unfinished_submissions=0,
        active_reserves=0,
        batches=context.batches,
        same_email_elsewhere=context.same_email_elsewhere,
    )

    failed = await _delete_old_objects(s3, context.renames)
    if failed:
        report.failed_steps.append("storage")
        logger.error(
            "student_deletion_storage_failed",
            student_id=str(student_id),
            submission_ids=[str(i) for i in failed],
            hint="scripts/student_admin.py finish-storage",
        )
    counters = 0
    if redis is not None and context.redis_keys:

        async def _delete_counters() -> None:
            nonlocal counters
            counters = int(await redis.delete(*context.redis_keys))

        if not await _retrying("redis", _delete_counters):
            report.failed_steps.append("redis")
            logger.error("student_deletion_redis_failed", student_id=str(student_id))
    report.redis_checked = redis is not None

    report.places = _report_places(
        written,
        renamed=len(context.renames),
        quote_files=quote_files,
        accounts=len(anonymized.account_ids),
        rules=len(anonymized.ended_rule_ids),
        counters=counters,
    )
    logger.info(
        "student_deleted",
        actor=actor,
        reason=reason,
        tenant_id=str(tenant_id),
        student_id=str(student_id),
        changes={f"{p.place}: {p.fields}": p.rows for p in report.places},
        failed_steps=report.failed_steps,
    )
    return report


async def _dry_run(
    session_factory: async_sessionmaker[AsyncSession],
    s3: S3Client,
    redis: Redis | None,
    *,
    tenant: str,
    student_id: uuid.UUID,
    now: datetime,
) -> DeletionReport:
    async with session_factory() as session, session.begin():
        await session.execute(text("SET TRANSACTION READ ONLY"))
        tenant_id = await resolve_tenant(session, tenant)
        student = await _load_student(session, tenant_id, student_id, lock=False)
        _refuse_deleted(student)
        context = await _gather(session, s3, tenant_id, student, now)
        counted = [(p, await _count(session, p)) for p in context.places]
    counters, checked = 0, redis is not None
    if redis is not None and context.redis_keys:
        try:
            counters = int(await redis.exists(*context.redis_keys))
        except (RedisError, OSError, TimeoutError):
            checked = False
    return DeletionReport(
        tenant_id=tenant_id,
        student_id=student_id,
        dry_run=True,
        places=_report_places(
            counted,
            renamed=len(context.renames),
            quote_files=context.quote_files,
            accounts=len(context.account_ids),
            rules=context.student_rules,
            counters=counters,
        ),
        unfinished_submissions=context.unfinished,
        active_reserves=context.reserves,
        batches=context.batches,
        same_email_elsewhere=context.same_email_elsewhere,
        redis_checked=checked,
    )


async def finish_storage(
    session_factory: async_sessionmaker[AsyncSession],
    s3: S3Client,
    *,
    tenant: str,
    student_id: uuid.UUID,
    actor: str,
) -> StorageCleanup:
    """For a DELETED student: delete every object left in a submission's own
    folder besides its file and its project snapshot — an old key a failed
    after-commit step left behind. Keys are never logged.

    Raises:
        DeletionRefusedError: no such student in the tenant, or not deleted.
    """
    async with session_factory() as session, session.begin():
        await session.execute(text("SET TRANSACTION READ ONLY"))
        tenant_id = await resolve_tenant(session, tenant)
        student = await _load_student(session, tenant_id, student_id, lock=False)
        if student.deleted_at is None:
            raise DeletionRefusedError(
                f"student {student_id} is not deleted; run delete first"
            )
        submissions = list(
            (
                await session.execute(
                    select(HomeworkSubmission).where(
                        HomeworkSubmission.student_id == student_id
                    )
                )
            ).scalars()
        )
    removed = 0
    failed: list[uuid.UUID] = []
    for submission in submissions:
        key = s3.extract_key(submission.file_url)
        if key is None or not key.startswith("homework/"):
            continue
        keep = {key, submission.snapshot_key}
        folder = f"{PurePosixPath(key).parent}/"
        try:
            listed = await s3.list_objects(folder)
        except (ClientError, BotoCoreError):
            failed.append(submission.id)
            continue
        for obj in listed:
            if obj["key"] in keep:
                continue

            async def _delete(stale: str = obj["key"]) -> None:
                await s3.delete_object(stale, log_key=False)

            if await _retrying("storage", _delete):
                removed += 1
            elif submission.id not in failed:
                failed.append(submission.id)
    if failed:
        logger.error(
            "student_deletion_storage_failed",
            student_id=str(student_id),
            submission_ids=[str(i) for i in failed],
        )
    logger.info(
        "student_deletion_storage_finished",
        actor=actor,
        student_id=str(student_id),
        removed=removed,
    )
    return StorageCleanup(removed=removed, failed_submission_ids=failed)
