"""Б-2: delete a student's account on request — the locks (а)–(є).

A committed world: a student of author A with a login, a confirmed e-mail,
recovery tokens, self-enrollment requests, an enrollment, three submissions
(a file whose name carries the student's surname, a project archive with its
snapshot, a test's answers) stored in the S3-compatible storage, a verdict
quoting the file by name, the student's reply and the Mentor's answer, a
feedback text, billing (two purchases at different prices — one partly spent
— a gift and a test purchase, a student rule) and two rate-limit counters in
Redis. The student's surname is planted in every personal place; the work's
content carries none, so the probe (ґ) can demand zero after the deletion.

Requires ``docker compose up -d``; run with ``--run-db --run-redis``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from arq.connections import ArqRedis
from scripts import student_admin
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.auth.rate_limits import KEY_PREFIX, RedisRateLimiter, counter_key
from course_supporter.auth.student_passwords import hash_password
from course_supporter.billing import accounts as billing_accounts
from course_supporter.billing import admin as billing_admin
from course_supporter.billing import ledger, testmark
from course_supporter.billing.kinds import (
    ActionKind,
    MarkOrigin,
    PayerSource,
    RuleScope,
)
from course_supporter.config import RateLimit, get_settings
from course_supporter.jobs.job_type import JobType
from course_supporter.storage.orm import (
    AuthoredDocument,
    BillingAccount,
    BillingPaymentRule,
    ExternalServiceCall,
    HomeworkSubmission,
    Job,
    PortalSignup,
    RemarkComment,
    Student,
    StudentCredential,
    StudentCredentialToken,
    StudentEnrollment,
    StudentFeedback,
    SubmissionCriterionVerdict,
)
from course_supporter.storage.s3 import S3Client
from course_supporter.student_deletion import (
    DELETED_TEXT,
    DeletionRefusedError,
    DeletionReport,
    delete_student,
    finish_storage,
    service,
)
from tests._helpers.billing_cleanup import drop_billing_of_tenant
from tests._helpers.open_courses import (
    PASSWORD,
    FakeArq,
    World,
    app_client,
    auth,
    drop_world,
    make_world,
)

pytestmark = [pytest.mark.requires_db, pytest.mark.requires_redis]

ACTOR = "kostiantyn"
VERDICT = {"passed": True, "correctness": 0.9}
REVIEW_TEXT = "Good work: the function sums the list."
REMARK = "An empty list must return zero."
ARCHIVE_MEMBER = "src/main.py"
REASON = "ticket 4242"
_TEXT_TYPES = ("text", "character varying", "jsonb", "json", "ARRAY")


# ── The world ─────────────────────────────────────────────────────────────


@dataclass
class Seeded:
    world: World
    surname: str
    email: str
    login: str
    student_id: uuid.UUID
    account_id: uuid.UUID
    file_sub: uuid.UUID
    project_sub: uuid.UUID
    test_sub: uuid.UUID
    old_keys: dict[uuid.UUID, str]
    """The stored file's key of each submission, before the deletion."""
    contents: dict[uuid.UUID, bytes]
    snapshot_key: str
    batches: dict[str, uuid.UUID]
    stranger_signup: uuid.UUID
    """A pending signup of another person in the same tenant: must survive."""
    job_id: uuid.UUID
    call_id: uuid.UUID
    accounts: list[uuid.UUID] = field(default_factory=list)

    @property
    def needles(self) -> tuple[str, ...]:
        return (self.surname, self.email, self.login)


@pytest.fixture()
async def s3_client() -> AsyncIterator[S3Client]:
    s = get_settings()
    client = S3Client(
        endpoint_url=s.s3_endpoint,
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key.get_secret_value(),
        bucket=s.s3_bucket,
    )
    await client.open()
    try:
        await client.ensure_bucket()
        yield client
    finally:
        await client.close()


@pytest.fixture(autouse=True)
def _no_retry_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "RETRY_DELAYS_S", (0.0,))


async def _upload(s3: S3Client, key: str, data: bytes) -> None:
    await s3.upload_file(key, data, "application/octet-stream")


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    s3: S3Client,
    redis: ArqRedis,
) -> Seeded:
    world = await make_world(session_factory)
    tag = uuid.uuid4().hex[:8]
    surname = f"Ivanenko{tag}"
    email = f"ivanenko{tag}@example.org"
    login = f"ivanenko{tag}"
    now = datetime.now(UTC)
    a = world.tenant_a

    file_key = f"homework/{a}/{uuid.uuid4()}/{surname}_dz3.py"
    project_dir = f"homework/{a}/{uuid.uuid4()}"
    project_key = f"{project_dir}/{surname}_project.tar.gz"
    snapshot_key = f"{project_dir}/snapshot.zip"
    test_key = f"homework/{a}/{uuid.uuid4()}/answers.json"
    file_bytes = b"def total(items):\n    return sum(items)\n"
    project_bytes = hashlib.sha256(tag.encode()).digest() * 64
    test_bytes = b'{"answers": {"q1": ["a"]}}'
    for key, data in (
        (file_key, file_bytes),
        (project_key, project_bytes),
        (snapshot_key, b"snapshot"),
        (test_key, test_bytes),
    ):
        await _upload(s3, key, data)

    async with session_factory() as session:
        student = Student(
            tenant_id=a,
            external_id=f"{login}@school.example",
            display_name=f"Petro {surname}",
            metadata_={"full_name": f"Petro {surname}", "group": "3A"},
            mentor_preferences=f"Call me {surname}, short answers please",
            preferred_language="ukr",
        )
        session.add(student)
        await session.flush()
        credential = StudentCredential(
            student_id=student.id,
            tenant_id=a,
            login=login,
            password_hash=hash_password(PASSWORD),
            recovery_email=email,
            recovery_email_confirmed_at=now,
        )
        session.add(credential)
        await session.flush()
        for purpose in ("password_reset", "email_confirm"):
            session.add(
                StudentCredentialToken(
                    credential_id=credential.id,
                    purpose=purpose,
                    token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                    expires_at=now,
                )
            )
        signup_common: dict[str, Any] = {
            "tenant_id": a,
            "course_node_id": world.course,
            "password_hash": "x",
            "adult_confirmed_at": now,
            "terms_version": "1.2",
            "text_sha256": "a" * 64,
            "expires_at": now,
        }
        session.add_all(
            [
                PortalSignup(  # confirmed: became this student
                    login=login,
                    email=email,
                    display_name=f"Petro {surname}",
                    token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                    confirmed_at=now,
                    student_id=student.id,
                    **signup_common,
                ),
                PortalSignup(  # pending, with the student's address
                    login=f"{login}-again",
                    email=email,
                    token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                    **signup_common,
                ),
            ]
        )
        stranger = PortalSignup(
            login=f"stranger-{tag}",
            email=f"stranger-{tag}@example.org",
            token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            **signup_common,
        )
        session.add(stranger)
        session.add(
            StudentEnrollment(student_id=student.id, course_node_id=world.course)
        )
        doc = AuthoredDocument(
            course_node_id=world.child,
            course_root_id=world.course,
            source_type="text",
            source_url=f"https://example.com/{tag}",
            task_type="task",
            language="ukr",
            content_hash="v1",
        )
        session.add(doc)
        await session.flush()

        def submission(key: str, name: str, data: bytes, **extra: Any) -> Any:
            extra.setdefault("status", "completed")
            extra.setdefault("review_markdown", REVIEW_TEXT)
            extra.setdefault("review_result", {"verdict": VERDICT})
            return HomeworkSubmission(
                tenant_id=a,
                student_id=student.id,
                course_node_id=world.course,
                node_id=world.child,
                authored_document_id=doc.id,
                file_url=s3.get_object_url(key),
                file_type="text/plain",
                original_filename=name,
                file_hash=hashlib.sha256(data).hexdigest(),
                score=90,
                **extra,
            )

        file_sub = submission(
            file_key,
            f"{surname}_dz3.py",
            file_bytes,
            student_note=f"{surname}: please check the edge cases",
            task_version="v1",
            # The remark's place, as text_result._place_of and
            # review_assembler._position_line write it.
            review_result={
                "verdict": VERDICT,
                "remarks": {
                    "c2": {"position": {"kind": "file", "value": f"{surname}_dz3.py:1"}}
                },
            },
            review_markdown=f"{REVIEW_TEXT}\n\n*файл {surname}_dz3.py:1*\n{REMARK}",
        )
        project_sub = submission(
            project_key,
            f"{surname}_project.tar.gz",
            project_bytes,
            snapshot_key=snapshot_key,
            status="delivered",
            # A path inside the archive is the work's: it stays as it is.
            snapshot_manifest={"included": [{"path": ARCHIVE_MEMBER}]},
            review_result={
                "verdict": VERDICT,
                "remarks": {
                    "c1": {"position": {"kind": "file", "value": f"{ARCHIVE_MEMBER}:3"}}
                },
            },
        )
        test_sub = submission(test_key, "answers.json", test_bytes)
        session.add_all([file_sub, project_sub, test_sub])
        await session.flush()
        job = Job(
            tenant_id=a,
            job_type=JobType.HOMEWORK_PROCESSING.value,
            subject_type="homework_submission",
            subject_id=file_sub.id,
            input_params={"submission_id": str(file_sub.id)},
        )
        session.add(job)
        await session.flush()
        file_sub.job_id = job.id
        call = ExternalServiceCall(
            job_id=job.id,
            action="criteria_evaluation",
            strategy="default",
            input_text=f"=== {surname}_dz3.py ===\n{file_bytes.decode()}",
            output_text=json.dumps(
                {
                    "items": [
                        {
                            "id": "c2",
                            "verdict": "not_met",
                            "quote_file": f"{surname}_dz3.py",
                            "quote_lines": [1, 1],
                        }
                    ]
                }
            ),
        )
        session.add(call)
        await session.flush()
        session.add(
            SubmissionCriterionVerdict(
                tenant_id=a,
                submission_id=file_sub.id,
                criteria_layer="model",
                criteria_source_id=uuid.uuid4(),
                item_id="c1",
                item_kind="criterion",
                weight="must",
                verdict="met",
                model_verdict="met",
                quote="def total(items):",
                quote_file=f"{surname}_dz3.py",
                quote_line_start=1,
                quote_line_end=1,
            )
        )
        question = RemarkComment(
            tenant_id=a,
            student_id=student.id,
            authored_document_id=doc.id,
            task_version="v1",
            submission_id=file_sub.id,
            item_id="c1",
            author="student",
            kind="question",
            body=f"Why is this wrong? — {surname}",
        )
        session.add(question)
        await session.flush()
        session.add(
            RemarkComment(
                tenant_id=a,
                student_id=student.id,
                authored_document_id=doc.id,
                task_version="v1",
                submission_id=file_sub.id,
                item_id="c1",
                parent_id=question.id,
                author="mentor",
                kind="answer",
                body="An empty list must return zero.",
            )
        )
        session.add(
            StudentFeedback(
                tenant_id=a,
                student_id=student.id,
                target_kind="review",
                target_id=file_sub.id,
                kind="touch",
                value="helped",
                text=f"thanks, {surname}",
            )
        )
        # Billing: two purchases at different prices (the first partly spent),
        # a gift, a test purchase; a student rule paid by the author.
        account = await billing_accounts.get_or_create_student_account(
            session, tenant_id=a, student_id=student.id, actor=ACTOR
        )
        cheap = await ledger.credit_purchase(
            session,
            account_id=account.id,
            units=100,
            amount_paid_minor=500,
            currency="EUR",
            provider="creem",
            provider_transaction_id=f"ord_a_{tag}",
            provider_line_id=f"line_a_{tag}",
            now=now,
        )
        dear = await ledger.credit_purchase(
            session,
            account_id=account.id,
            units=300,
            amount_paid_minor=2000,
            currency="EUR",
            provider="creem",
            provider_transaction_id=f"ord_b_{tag}",
            provider_line_id=f"line_b_{tag}",
            now=now,
        )
        gift = await ledger.credit_admin(
            session, account_id=account.id, units=50, reason="pilot", actor=ACTOR
        )
        test = await ledger.credit_purchase(
            session,
            account_id=account.id,
            units=20,
            amount_paid_minor=100,
            currency="EUR",
            provider="creem",
            provider_transaction_id=f"ord_t_{tag}",
            provider_line_id=f"line_t_{tag}",
            now=now,
        )
        await testmark.mark(
            session, batch_id=test.batch.id, origin=MarkOrigin.CREDIT, now=now
        )
        await ledger.reserve(
            session,
            action_kind=ActionKind.REVIEW,
            action_key=str(file_sub.id),
            account_id=account.id,
            units=40,
            now=now,
        )
        await ledger.settle(
            session,
            action_kind=ActionKind.REVIEW,
            action_key=str(file_sub.id),
            fact_units=40,
            beneficiary_account_id=account.id,
            payer_source=PayerSource.DEFAULT,
            now=now,
        )
        await billing_admin.create_payment_rule(
            session,
            actor=ACTOR,
            tenant_id=a,
            scope=RuleScope.STUDENT,
            action_kind=ActionKind.REVIEW,
            student_id=student.id,
        )
        await session.commit()

    limiter = RedisRateLimiter(redis)
    rule = RateLimit(limit=100, window_s=600)
    await limiter.hit("login_by_email_email", email, rule)
    await limiter.hit("join_course_student", str(student.id), rule)

    return Seeded(
        world=world,
        surname=surname,
        email=email,
        login=login,
        student_id=student.id,
        account_id=account.id,
        file_sub=file_sub.id,
        project_sub=project_sub.id,
        test_sub=test_sub.id,
        old_keys={
            file_sub.id: file_key,
            project_sub.id: project_key,
            test_sub.id: test_key,
        },
        contents={
            file_sub.id: file_bytes,
            project_sub.id: project_bytes,
            test_sub.id: test_bytes,
        },
        snapshot_key=snapshot_key,
        batches={
            "cheap": cheap.batch.id,
            "dear": dear.batch.id,
            "gift": gift.batch.id,
            "test": test.batch.id,
        },
        stranger_signup=stranger.id,
        job_id=job.id,
        call_id=call.id,
        accounts=[account.id],
    )


@pytest.fixture()
async def seeded(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    arq_redis: ArqRedis,
) -> AsyncIterator[Seeded]:
    made = await _seed(session_factory, s3_client, arq_redis)
    yield made
    async with session_factory() as session:
        # Jobs outlive their tenant (SET NULL); the register rows point at them.
        await session.execute(
            delete(ExternalServiceCall).where(ExternalServiceCall.job_id == made.job_id)
        )
        await session.execute(
            update(HomeworkSubmission)
            .where(HomeworkSubmission.job_id == made.job_id)
            .values(job_id=None)
        )
        await session.execute(delete(Job).where(Job.id == made.job_id))
        await session.commit()
    for obj in await s3_client.list_objects(f"homework/{made.world.tenant_a}/"):
        await s3_client.delete_object(obj["key"])
    await drop_billing_of_tenant(
        session_factory, made.world.tenant_a, also_accounts=made.accounts
    )
    await drop_world(session_factory, made.world)


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    s3: S3Client,
    redis: ArqRedis,
    seeded: Seeded,
    *,
    dry_run: bool = False,
    tenant: str | None = None,
) -> DeletionReport:
    return await delete_student(
        session_factory,
        s3,
        redis,
        tenant=tenant or str(seeded.world.tenant_a),
        student_id=seeded.student_id,
        actor=ACTOR,
        reason=REASON,
        dry_run=dry_run,
    )


# ── Probes ────────────────────────────────────────────────────────────────


async def _table_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    async with session_factory() as session:
        tables = (
            await session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                )
            )
        ).scalars()
        return {
            t: int(
                (
                    # The names come from the catalog, not from input.
                    await session.execute(text(f'SELECT count(*) FROM "{t}"'))  # noqa: S608
                ).scalar_one()
            )
            for t in sorted(tables)
        }


async def _db_hits(
    session_factory: async_sessionmaker[AsyncSession], needles: tuple[str, ...]
) -> dict[str, int]:
    """Every text-like column of every table that holds one of ``needles``."""
    hits: dict[str, int] = {}
    async with session_factory() as session:
        columns = (
            await session.execute(
                text(
                    "SELECT c.table_name, c.column_name "
                    "FROM information_schema.columns c "
                    "JOIN information_schema.tables t "
                    "ON t.table_name = c.table_name "
                    "AND t.table_schema = c.table_schema "
                    "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
                    "AND c.data_type = ANY(:types)"
                ),
                {"types": list(_TEXT_TYPES)},
            )
        ).tuples()
        for table, column in columns:
            # Table and column names come from the catalog, not from input.
            probe = text(
                f'SELECT count(*) FROM "{table}" WHERE "{column}"::text ILIKE :p'  # noqa: S608
            )
            for needle in needles:
                found = int(
                    (await session.execute(probe, {"p": f"%{needle}%"})).scalar_one()
                )
                if found:
                    key = f"{table}.{column}"
                    hits[key] = hits.get(key, 0) + found
    return hits


async def _storage_hits(s3: S3Client, seeded: Seeded) -> list[str]:
    keys = [
        o["key"] for o in await s3.list_objects(f"homework/{seeded.world.tenant_a}/")
    ]
    return [k for k in keys if seeded.surname.lower() in k.lower()]


async def _redis_hits(redis: ArqRedis, seeded: Seeded) -> list[str]:
    personal = {
        counter_key("login_by_email_email", seeded.email),
        counter_key("join_course_student", str(seeded.student_id)),
    }
    stored = {k.decode() for k in await redis.keys(f"{KEY_PREFIX}*")}
    raw = [k for k in stored if any(n.lower() in k.lower() for n in seeded.needles)]
    return sorted((stored & personal) | set(raw))


# ── (а) the dry run ───────────────────────────────────────────────────────


async def test_a_dry_run_writes_nothing_and_shows_refunds(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    arq_redis: ArqRedis,
    seeded: Seeded,
) -> None:
    tables = await _table_counts(session_factory)
    stored = await s3_client.list_objects(f"homework/{seeded.world.tenant_a}/")
    redis_keys = sorted(await arq_redis.keys("*"))

    report = await _run(session_factory, s3_client, arq_redis, seeded, dry_run=True)

    assert await _table_counts(session_factory) == tables
    assert await s3_client.list_objects(f"homework/{seeded.world.tenant_a}/") == stored
    assert sorted(await arq_redis.keys("*")) == redis_keys
    async with session_factory() as session:
        student = await session.get(Student, seeded.student_id)
        assert student is not None and student.deleted_at is None

    refunds = {b.batch_id: (b.kind, b.unused, b.refund_minor) for b in report.batches}
    assert refunds == {
        # 100 units for 500, 40 spent: 500 x 60 / 100.
        seeded.batches["cheap"]: ("purchase", 60, 300),
        seeded.batches["dear"]: ("purchase", 300, 2000),
        seeded.batches["gift"]: ("gift", 50, 0),
        seeded.batches["test"]: ("test", 20, 0),
    }
    assert report.rows("student_credential_tokens") == 2
    assert report.rows("portal_signups") == 2
    assert report.rows("storage") == 2  # the file and the archive; not answers.json
    assert {
        (p.place, p.fields.split(":")[0]): p.rows
        for p in report.places
        if p.unit == "occurrence(s)"
    } == {
        ("homework_submissions", "review_result"): 1,
        ("homework_submissions", "review_markdown"): 1,
        ("submission_criterion_verdicts", "quote_file"): 1,
        ("external_service_calls", "input_text"): 1,
        ("external_service_calls", "output_text"): 1,
    }
    assert report.rows("redis") == 2
    assert report.rows("billing_accounts") == 1
    assert report.rows("billing_payment_rules") == 1
    assert (report.unfinished_submissions, report.active_reserves) == (0, 0)

    out = student_admin.render(report)
    assert "dry run: nothing written" in out
    assert "refund 300 EUR" in out and "refund 2000 EUR" in out
    assert "total refund 2300 EUR" in out
    assert not any(n.lower() in out.lower() for n in seeded.needles)


async def test_a_dry_run_lists_the_same_confirmed_email_elsewhere(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    arq_redis: ArqRedis,
    seeded: Seeded,
) -> None:
    async with session_factory() as session:
        twin = Student(tenant_id=seeded.world.tenant_b, external_id="twin")
        session.add(twin)
        await session.flush()
        session.add(
            StudentCredential(
                student_id=twin.id,
                tenant_id=seeded.world.tenant_b,
                login="twin",
                password_hash="x",
                recovery_email=seeded.email.upper(),
                recovery_email_confirmed_at=datetime.now(UTC),
            )
        )
        await session.commit()

    report = await _run(session_factory, s3_client, arq_redis, seeded, dry_run=True)

    assert report.same_email_elsewhere == [(twin.id, seeded.world.tenant_b)]


# ── (б) no way in ─────────────────────────────────────────────────────────


async def test_b_no_sign_in_after_the_deletion(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    arq_redis: ArqRedis,
    seeded: Seeded,
) -> None:
    by_login = {
        "tenant_id": str(seeded.world.tenant_a),
        "login": seeded.login,
        "password": PASSWORD,
    }
    by_email = {"email": seeded.email, "password": PASSWORD}
    async with app_client(seeded.world, session_factory, FakeArq()) as client:
        before = await client.post("/api/v1/portal/login", json=by_login)
        assert before.status_code == 200, before.text
        token = before.json()["access_token"]
        assert (
            await client.get("/api/v1/portal/me", headers=auth(token))
        ).status_code == 200
        assert (
            await client.post("/api/v1/portal/login-by-email", json=by_email)
        ).status_code == 200

        await _run(session_factory, s3_client, arq_redis, seeded)

        assert (
            await client.post("/api/v1/portal/login", json=by_login)
        ).status_code == 401
        assert (
            await client.post("/api/v1/portal/login-by-email", json=by_email)
        ).status_code == 401
        assert (
            await client.get("/api/v1/portal/me", headers=auth(token))
        ).status_code == 401

    async with session_factory() as session:
        signups = list(
            (
                await session.execute(
                    select(PortalSignup.id).where(
                        PortalSignup.tenant_id == seeded.world.tenant_a
                    )
                )
            ).scalars()
        )
        tokens = await session.execute(
            select(func.count())
            .select_from(StudentCredentialToken)
            .join(StudentCredential)
            .where(StudentCredential.student_id == seeded.student_id)
        )
    assert signups == [seeded.stranger_signup]
    assert tokens.scalar_one() == 0


# ── (в) the marker row ────────────────────────────────────────────────────


async def test_c_the_student_row_is_a_marker_and_enrollments_are_gone(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    arq_redis: ArqRedis,
    seeded: Seeded,
) -> None:
    report = await _run(session_factory, s3_client, arq_redis, seeded)

    marker = f"deleted-{seeded.student_id}"
    async with session_factory() as session:
        student = await session.get(Student, seeded.student_id)
        credential = (
            await session.execute(
                select(StudentCredential).where(
                    StudentCredential.student_id == seeded.student_id
                )
            )
        ).scalar_one()
        enrollments = (
            await session.execute(
                select(func.count()).where(
                    StudentEnrollment.student_id == seeded.student_id
                )
            )
        ).scalar_one()
    assert student is not None and student.deleted_at is not None
    assert (
        student.external_id,
        student.display_name,
        student.metadata_,
        student.mentor_preferences,
    ) == (marker, None, None, None)
    assert (credential.login, credential.is_active, credential.recovery_email) == (
        marker,
        False,
        None,
    )
    assert credential.recovery_email_confirmed_at is None
    assert enrollments == 0
    assert report.rows("student_enrollments") == 1
    assert report.failed_steps == []


# ── (г) the work stays ────────────────────────────────────────────────────


async def test_d_the_work_stays_and_the_personal_texts_go(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    arq_redis: ArqRedis,
    seeded: Seeded,
) -> None:
    tables = await _table_counts(session_factory)

    await _run(session_factory, s3_client, arq_redis, seeded)

    after = await _table_counts(session_factory)
    for table in (
        "homework_submissions",
        "submission_criterion_verdicts",
        "submission_explanations",
        "remark_comments",
        "student_feedback",
        "billing_movements",
        "billing_batches",
        "billing_consents",
    ):
        assert after[table] == tables[table], table
    async with session_factory() as session:
        subs = {
            s.id: s
            for s in (
                await session.execute(
                    select(HomeworkSubmission).where(
                        HomeworkSubmission.student_id == seeded.student_id
                    )
                )
            ).scalars()
        }
        verdict = (
            await session.execute(
                select(SubmissionCriterionVerdict).where(
                    SubmissionCriterionVerdict.submission_id == seeded.file_sub
                )
            )
        ).scalar_one()
        comments = {
            c.author: c.body
            for c in (
                await session.execute(
                    select(RemarkComment).where(
                        RemarkComment.student_id == seeded.student_id
                    )
                )
            ).scalars()
        }
        feedback = (
            await session.execute(
                select(StudentFeedback).where(
                    StudentFeedback.student_id == seeded.student_id
                )
            )
        ).scalar_one()

    file_sub, project, test = (
        subs[seeded.file_sub],
        subs[seeded.project_sub],
        subs[seeded.test_sub],
    )
    assert (file_sub.original_filename, file_sub.student_note) == (
        "submission.py",
        None,
    )
    assert project.original_filename == "submission.tar.gz"
    assert test.original_filename == "answers.json"  # the platform's own name
    assert s3_client.extract_key(test.file_url) == seeded.old_keys[seeded.test_sub]
    assert project.snapshot_key == seeded.snapshot_key
    assert file_sub.review_markdown == (
        f"{REVIEW_TEXT}\n\n*файл submission.py:1*\n{REMARK}"
    )
    assert file_sub.review_result == {
        "verdict": VERDICT,
        "remarks": {"c2": {"position": {"kind": "file", "value": "submission.py:1"}}},
    }
    # The archive's own paths are the work: untouched.
    assert project.snapshot_manifest == {"included": [{"path": ARCHIVE_MEMBER}]}
    assert project.review_result is not None
    assert project.review_result["remarks"]["c1"]["position"]["value"] == (
        f"{ARCHIVE_MEMBER}:3"
    )
    async with session_factory() as session:
        call = await session.get(ExternalServiceCall, seeded.call_id)
    assert call is not None and call.output_text is not None
    assert json.loads(call.output_text)["items"][0]["quote_file"] == "submission.py"
    assert call.input_text == (
        f"=== submission.py ===\n{seeded.contents[seeded.file_sub].decode()}"
    )
    assert (file_sub.score, file_sub.status, project.status) == (
        90,
        "completed",
        "delivered",
    )
    assert (verdict.quote, verdict.quote_file) == ("def total(items):", "submission.py")
    assert comments == {
        "student": DELETED_TEXT,
        "mentor": "An empty list must return zero.",
    }
    assert (feedback.text, feedback.value) == (None, "helped")

    # The renamed objects hold the very same bytes.
    for sub_id in (seeded.file_sub, seeded.project_sub, seeded.test_sub):
        key = s3_client.extract_key(subs[sub_id].file_url)
        assert key is not None
        body = await s3_client.get_object(key)
        assert (
            hashlib.sha256(body).hexdigest()
            == hashlib.sha256(seeded.contents[sub_id]).hexdigest()
        )
        assert subs[sub_id].file_hash == hashlib.sha256(body).hexdigest()
    assert await s3_client.get_object(seeded.snapshot_key) == b"snapshot"


# ── (ґ) nothing left anywhere ─────────────────────────────────────────────


async def test_e_nothing_personal_is_left_anywhere(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    arq_redis: ArqRedis,
    seeded: Seeded,
) -> None:
    # The probe sees what it looks for: before, every class of place answers.
    before = await _db_hits(session_factory, seeded.needles)
    for place in (
        "students.external_id",
        "students.display_name",
        "students.metadata",
        "students.mentor_preferences",
        "student_credentials.login",
        "student_credentials.recovery_email",
        "portal_signups.email",
        "homework_submissions.original_filename",
        "homework_submissions.file_url",
        "homework_submissions.student_note",
        "submission_criterion_verdicts.quote_file",
        "homework_submissions.review_result",
        "homework_submissions.review_markdown",
        "external_service_calls.input_text",
        "external_service_calls.output_text",
        "remark_comments.body",
        "student_feedback.text",
    ):
        assert before.get(place, 0) > 0, place
    assert len(await _storage_hits(s3_client, seeded)) == 2
    assert len(await _redis_hits(arq_redis, seeded)) == 2

    await _run(session_factory, s3_client, arq_redis, seeded)

    assert await _db_hits(session_factory, seeded.needles) == {}
    assert await _storage_hits(s3_client, seeded) == []
    assert await _redis_hits(arq_redis, seeded) == []
    keys = sorted(
        o["key"]
        for o in await s3_client.list_objects(f"homework/{seeded.world.tenant_a}/")
    )
    assert sorted(k.rsplit("/", 1)[1] for k in keys) == [
        "answers.json",
        "snapshot.zip",
        "submission.py",
        "submission.tar.gz",
    ]


# ── (д) billing ───────────────────────────────────────────────────────────


async def test_f_billing_is_closed_and_a_second_run_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    arq_redis: ArqRedis,
    seeded: Seeded,
) -> None:
    await _run(session_factory, s3_client, arq_redis, seeded)

    async with session_factory() as session:
        account = await session.get(BillingAccount, seeded.account_id)
        rule = (
            await session.execute(
                select(BillingPaymentRule).where(
                    BillingPaymentRule.student_id == seeded.student_id
                )
            )
        ).scalar_one()
    assert account is not None
    assert (account.status, account.tenant_id, account.student_id) == (
        "closed",
        None,
        None,
    )
    assert account.anonymized_at is not None
    assert rule.active is False

    tables = await _table_counts(session_factory)
    stored = await s3_client.list_objects(f"homework/{seeded.world.tenant_a}/")
    with pytest.raises(DeletionRefusedError, match="already deleted at"):
        await _run(session_factory, s3_client, arq_redis, seeded)
    assert await _table_counts(session_factory) == tables
    assert await s3_client.list_objects(f"homework/{seeded.world.tenant_a}/") == stored


# ── (е) the author's screen ───────────────────────────────────────────────


async def test_g_the_author_screen_answers_404(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    arq_redis: ArqRedis,
    seeded: Seeded,
) -> None:
    path = f"/api/v1/students/{seeded.student_id}/submissions"
    async with app_client(seeded.world, session_factory, FakeArq()) as client:
        assert (await client.get(path)).status_code == 200
        await _run(session_factory, s3_client, arq_redis, seeded)
        assert (await client.get(path)).status_code == 404


# ── (є) refusals ──────────────────────────────────────────────────────────


def _cli(*args: str) -> list[str]:
    return ["delete", *args, "--actor", ACTOR]


async def _main(argv: list[str]) -> int:
    """The script as an operator runs it: its own event loop and engine."""
    return await asyncio.to_thread(student_admin.main, argv)


async def test_h_refusals_each_say_their_own_reason(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    arq_redis: ArqRedis,
    seeded: Seeded,
    capsys: pytest.CaptureFixture[str],
) -> None:
    a, b = str(seeded.world.tenant_a), str(seeded.world.tenant_b)
    sid = str(seeded.student_id)
    tables = await _table_counts(session_factory)
    cases = [
        (
            _cli("--tenant", a, "--student", str(uuid.uuid4()), "--reason", REASON),
            "not found",
        ),
        (
            _cli("--tenant", b, "--student", sid, "--reason", REASON),
            "belongs to another tenant",
        ),
        (
            _cli(
                "--tenant", a, "--student", sid, "--reason", f"asked by {seeded.email}"
            ),
            "holds '@'",
        ),
        (
            _cli("--tenant", a, "--student", sid, "--reason", "x@y", "--dry-run"),
            "holds '@'",
        ),
    ]
    for argv, said in cases:
        assert await _main(argv) == 1, argv
        err = capsys.readouterr().err
        assert said in err, (argv, err)
        assert not any(n.lower() in err.lower() for n in seeded.needles)
    assert await _table_counts(session_factory) == tables

    await _run(session_factory, s3_client, arq_redis, seeded)
    tables = await _table_counts(session_factory)
    assert await _main(_cli("--tenant", a, "--student", sid, "--reason", REASON)) == 1
    assert "already deleted at" in capsys.readouterr().err
    assert await _table_counts(session_factory) == tables


async def test_h_an_unfinished_submission_refuses_the_run(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    arq_redis: ArqRedis,
    seeded: Seeded,
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(HomeworkSubmission)
            .where(HomeworkSubmission.id == seeded.project_sub)
            .values(status="awaiting_funds")
        )
        await session.commit()
    dry = await _run(session_factory, s3_client, arq_redis, seeded, dry_run=True)
    assert dry.unfinished_submissions == 1
    tables = await _table_counts(session_factory)

    with pytest.raises(DeletionRefusedError, match="1 unfinished submission"):
        await _run(session_factory, s3_client, arq_redis, seeded)

    assert await _table_counts(session_factory) == tables
    assert len(await _storage_hits(s3_client, seeded)) == 2


async def test_h_a_copy_that_fails_changes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    arq_redis: ArqRedis,
    seeded: Seeded,
) -> None:
    # The archive's object is gone: its copy fails after the file's succeeded.
    await s3_client.delete_object(seeded.old_keys[seeded.project_sub])
    tables = await _table_counts(session_factory)

    with pytest.raises(DeletionRefusedError, match="could not be copied"):
        await _run(session_factory, s3_client, arq_redis, seeded)

    assert await _table_counts(session_factory) == tables
    async with session_factory() as session:
        student = await session.get(Student, seeded.student_id)
        assert student is not None and student.deleted_at is None
    names = [
        o["key"].rsplit("/", 1)[1]
        for o in await s3_client.list_objects(f"homework/{seeded.world.tenant_a}/")
    ]
    assert not any(n.startswith("submission") for n in names)  # copies undone


async def test_finish_storage_removes_an_old_key_left_behind(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    arq_redis: ArqRedis,
    seeded: Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _refuse(*_: object, **__: object) -> None:
        raise OSError("storage down")

    monkeypatch.setattr(s3_client, "delete_object", _refuse)
    report = await _run(session_factory, s3_client, arq_redis, seeded)
    assert report.failed_steps == ["storage"]
    assert len(await _storage_hits(s3_client, seeded)) == 2
    monkeypatch.undo()
    monkeypatch.setattr(service, "RETRY_DELAYS_S", (0.0,))

    done = await finish_storage(
        session_factory,
        s3_client,
        tenant=seeded.world.tenant_a_name,
        student_id=seeded.student_id,
        actor=ACTOR,
    )

    assert (done.removed, done.failed_submission_ids) == (2, [])
    assert await _storage_hits(s3_client, seeded) == []
    assert await s3_client.get_object(seeded.snapshot_key) == b"snapshot"
