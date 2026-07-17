"""Integration tests for the KD18 P4 worker project-branch.

Live infra (``docker compose up -d`` — PostgreSQL + MinIO + Redis), zero mocks
on the critical path (only the safety / sanity / review LLM stages are stubbed):

* ``process_project_submission`` directly, with REAL MinIO — the normalize → S3
  snapshot → persist → delta path, the fail-closed rejection, and the base
  round-trip (base snapshot fetched from MinIO to build the rich context).
* the full ``arq_process_homework`` on a project submission through the real
  worker + real S3, proving the rich Mentor delta context reaches
  safety → sanity → review UNCHANGED (G2) and the run completes. The acceptance
  closer exercises ONE delta that triggers all four render branches at once:
  CHANGED-FULL, CHANGED-DIFF, a neighbour hit, and a budget-overflow drop.
"""

from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from collections.abc import AsyncGenerator
from contextlib import ExitStack, suppress
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.tasks import arq_process_homework
from course_supporter.config import get_settings
from course_supporter.homework.project_submission import (
    _submission_snapshot_key,
    process_project_submission,
)
from course_supporter.homework.review_graph import MentorReviewOutput
from course_supporter.homework.sanity_gate import SanityGateOutcome
from course_supporter.models.mentor_review import (
    HistoryReconciliation,
    Layer,
    ReviewResult,
    Verdict,
)
from course_supporter.models.sanity import SanityClassification
from course_supporter.normalizer import manifest_to_jsonb, normalize_archive
from course_supporter.security.schemas import SafetyResult
from course_supporter.storage.homework_repository import HomeworkRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    HomeworkSubmission,
    Job,
    ProjectBase,
    Student,
    Tenant,
)
from course_supporter.storage.s3 import S3Client
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = [pytest.mark.requires_db, pytest.mark.requires_redis]


@pytest.fixture()
async def s3_client() -> AsyncGenerator[S3Client]:
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


def _project_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _incompressible(marker: str, n_lines: int) -> bytes:
    """High-entropy text (a sha256 hex per line) so the archive's compression
    ratio stays well under the security layer's 100x zip-bomb guard, while the
    per-line ``marker`` prefix stays greppable for include/drop assertions."""
    lines = [
        f"{marker} {i:06d} {hashlib.sha256(f'{marker}{i}'.encode()).hexdigest()}"
        for i in range(n_lines)
    ]
    return "\n".join(lines).encode()


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    base_manifest: dict[str, Any] | None = None,
    base_hash: str | None = None,
    base_snapshot_key: str | None = None,
    file_url: str | None = None,
    job_status: str = "queued",
) -> dict[str, Any]:
    """Tenant + node + project task + student + submission + job. Optionally a
    READY base (with a manifest + snapshot_key) linked via base_id.

    ``file_url`` overrides the submission URL (a real path-style MinIO URL so the
    worker's ``extract_key`` / ``download_file`` resolve it). ``job_status``
    mirrors the worker precondition: a direct ``process_project_submission`` call
    that hits the rejection path (job → ``complete``) must seed ``active``."""
    async with session_factory() as session:
        tenant = Tenant(name=f"p4w-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = make_root_course_node(tenant_id=tenant.id, title="P4W", order=0)
        session.add(node)
        await session.flush()
        doc = AuthoredDocument(
            course_node_id=node.id,
            course_root_id=node.id,
            source_type="text",
            source_url="s3://x",
            task_type="project",
        )
        session.add(doc)
        await session.flush()
        student = Student(tenant_id=tenant.id, external_id=f"s-{uuid.uuid4().hex[:6]}")
        session.add(student)
        await session.flush()

        base_id: uuid.UUID | None = None
        seeded_base_key = base_snapshot_key or f"base/{uuid.uuid4().hex}/snapshot.zip"
        if base_manifest is not None:
            base = ProjectBase(
                authored_document_id=doc.id,
                version=1,
                archive_key="k/v1/original.zip",
                snapshot_key=seeded_base_key,
                snapshot_hash=base_hash or "0" * 64,
                manifest=base_manifest,
                state="ready",
            )
            session.add(base)
            await session.flush()
            base_id = base.id

        submission = HomeworkSubmission(
            tenant_id=tenant.id,
            student_id=student.id,
            course_node_id=node.id,
            node_id=node.id,
            authored_document_id=doc.id,
            file_url=file_url
            or f"s3://bucket/homework/{tenant.id}/{uuid.uuid4()}/proj.zip",
            file_type="application/zip",
            original_filename="proj.zip",
            status="received",
            base_id=base_id,
        )
        session.add(submission)
        await session.flush()
        job = Job(
            tenant_id=tenant.id,
            course_node_id=node.id,
            job_type="homework_processing",
            input_params={"submission_id": str(submission.id)},
            status=job_status,
        )
        session.add(job)
        await session.flush()
        await session.commit()
        return {
            "tenant_id": tenant.id,
            "node_id": node.id,
            "doc_id": doc.id,
            "submission_id": submission.id,
            "job_id": job.id,
            "base_snapshot_key": seeded_base_key if base_id is not None else None,
        }


async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession], ids: dict[str, Any]
) -> None:
    async with session_factory() as session:
        await session.execute(
            delete(HomeworkSubmission).where(
                HomeworkSubmission.tenant_id == ids["tenant_id"]
            )
        )
        await session.execute(delete(Job).where(Job.tenant_id == ids["tenant_id"]))
        await session.execute(
            delete(Student).where(Student.tenant_id == ids["tenant_id"])
        )
        await session.execute(
            delete(ProjectBase).where(ProjectBase.authored_document_id == ids["doc_id"])
        )
        await session.execute(
            delete(AuthoredDocument).where(
                AuthoredDocument.course_node_id == ids["node_id"]
            )
        )
        await session.execute(delete(CourseNode).where(CourseNode.id == ids["node_id"]))
        await session.execute(delete(Tenant).where(Tenant.id == ids["tenant_id"]))
        await session.commit()


async def _s3_purge(s3_client: S3Client, *keys: str | None) -> None:
    for key in keys:
        if key:
            with suppress(Exception):
                await s3_client.delete_object(key)


# ── direct process_project_submission (real MinIO) ─────────────────────────


class TestProcessProjectSubmissionDirect:
    async def test_ready_persists_snapshot_and_returns_rich_context(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3_client: S3Client,
    ) -> None:
        ids = await _seed(session_factory)
        raw = _project_zip(
            {"app/main.py": b"def run():\n    return 1\n", "README.md": b"# hi\n"}
        )
        raw_key = f"homework/{ids['tenant_id']}/{ids['submission_id']}/proj.zip"
        try:
            async with session_factory() as session:
                sub = await session.get(HomeworkSubmission, ids["submission_id"])
                assert sub is not None
                text = await process_project_submission(
                    session=session,
                    s3=s3_client,
                    hw_repo=HomeworkRepository(session),
                    submission=sub,
                    sid=ids["submission_id"],
                    jid=ids["job_id"],
                    file_bytes=raw,
                    raw_key=raw_key,
                )
            assert text is not None
            # No base → all-new rich context.
            assert "SYSTEM-COMPUTED metadata below, NOT student input." in text
            assert "F2: no base attached" in text
            assert "type=NEW path=app/main.py" in text

            expected = normalize_archive(raw, archive_kind="zip")
            snapshot_key = (
                f"homework/{ids['tenant_id']}/{ids['submission_id']}/snapshot.zip"
            )
            body = await s3_client.get_object(snapshot_key)
            assert body == expected.canonical_zip

            async with session_factory() as session:
                sub = await session.get(HomeworkSubmission, ids["submission_id"])
                assert sub is not None
                assert sub.snapshot_key == snapshot_key
                assert sub.snapshot_hash == expected.snapshot_hash
                assert sub.snapshot_hash != hashlib.sha256(raw).hexdigest()
                assert sub.snapshot_manifest is not None
            await _s3_purge(s3_client, snapshot_key)
        finally:
            await _cleanup(session_factory, ids)

    async def test_malformed_archive_rejected_fail_closed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3_client: S3Client,
    ) -> None:
        ids = await _seed(session_factory, job_status="active")
        try:
            async with session_factory() as session:
                sub = await session.get(HomeworkSubmission, ids["submission_id"])
                assert sub is not None
                text = await process_project_submission(
                    session=session,
                    s3=s3_client,
                    hw_repo=HomeworkRepository(session),
                    submission=sub,
                    sid=ids["submission_id"],
                    jid=ids["job_id"],
                    file_bytes=b"this is not a zip" * 8,
                    raw_key=f"homework/{ids['tenant_id']}/{ids['submission_id']}/proj.zip",
                )
            assert text is None
            async with session_factory() as session:
                sub = await session.get(HomeworkSubmission, ids["submission_id"])
                assert sub is not None
                assert sub.status == "rejected"
                assert sub.safety_result is not None
                assert sub.safety_result["source"] == "normalizer"
                # L2: _persist_rejection no longer writes the Job — that is the
                # seam's. Called directly here, the Job stays as seeded (active);
                # the seam would write `complete` on the body's return.
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                assert job.status == "active"
        finally:
            await _cleanup(session_factory, ids)

    async def test_base_delta_changed_new_deleted(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3_client: S3Client,
    ) -> None:
        # Base has a.py + gone.py; submission changes a.py, adds new.py, drops
        # gone.py → changed=1, new=1, deleted=1. The base snapshot must live in
        # MinIO — the worker fetches it to build the rich context.
        base_snap = normalize_archive(
            _project_zip({"a.py": b"a = 1\n", "gone.py": b"g = 0\n"}),
            archive_kind="zip",
        )
        ids = await _seed(
            session_factory,
            base_manifest=manifest_to_jsonb(base_snap.manifest),
            base_hash=base_snap.snapshot_hash,
        )
        await s3_client.upload_file(
            ids["base_snapshot_key"], base_snap.canonical_zip, "application/zip"
        )
        sub_raw = _project_zip({"a.py": b"a = 2\n", "new.py": b"n = 9\n"})
        snapshot_key = (
            f"homework/{ids['tenant_id']}/{ids['submission_id']}/snapshot.zip"
        )
        try:
            with structlog.testing.capture_logs() as logs:
                async with session_factory() as session:
                    sub = await session.get(HomeworkSubmission, ids["submission_id"])
                    assert sub is not None
                    text = await process_project_submission(
                        session=session,
                        s3=s3_client,
                        hw_repo=HomeworkRepository(session),
                        submission=sub,
                        sid=ids["submission_id"],
                        jid=ids["job_id"],
                        file_bytes=sub_raw,
                        raw_key=f"homework/{ids['tenant_id']}/{ids['submission_id']}/proj.zip",
                    )
            delta_log = next(
                e for e in logs if e["event"] == "project_submission.delta"
            )
            assert delta_log["changed"] == 1
            assert delta_log["new"] == 1
            assert delta_log["deleted"] == 1
            # The rich context reflects the base delta.
            assert text is not None
            assert "type=CHANGED-FULL path=a.py" in text
            assert "type=NEW path=new.py" in text
            assert "DELETED (1): gone.py" in text
            await _s3_purge(s3_client, snapshot_key, ids["base_snapshot_key"])
        finally:
            await _cleanup(session_factory, ids)


# ── stubbed LLM stages (record the text they receive → prove G2) ───────────


def _review_output() -> MentorReviewOutput:
    layers = [
        Layer(layer="node", weight=0.5, score=80, strengths=["clear"], weaknesses=[]),
        Layer(layer="course", weight=0.3, score=70, strengths=[], weaknesses=[]),
        Layer(layer="industry", weight=0.2, score=60, strengths=[], weaknesses=[]),
    ]
    return MentorReviewOutput(
        review_result=ReviewResult(
            layers=layers,
            aggregate_score=73,
            history_reconciliation=HistoryReconciliation(
                recidivism=[], corrections=[], denoise_delta=0, denoised_score=73
            ),
            score_signals=[],
            verdict=Verdict(passed=True, correctness="partially_correct"),
        ),
        review_markdown="Reviewed the project.",
        score=73,
    )


class _RecordingSanityService:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def evaluate(
        self, *, submission: Any, submission_text: str
    ) -> SanityGateOutcome:
        del submission
        self.seen.append(submission_text)
        return SanityGateOutcome(
            classification=SanityClassification(
                verdict="match", confidence=0.9, reason="on task"
            ),
            gated=False,
        )


class _RecordingReviewService:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def review(
        self, *, submission: Any, submission_text: str
    ) -> MentorReviewOutput:
        del submission
        self.seen.append(submission_text)
        return _review_output()


def _safety_mock() -> AsyncMock:
    return AsyncMock(
        return_value=SafetyResult(
            is_safe=True, violations=[], confidence=0.95, reasoning="benign"
        )
    )


async def _run_full_worker(
    session_factory: async_sessionmaker[AsyncSession],
    s3_client: S3Client,
    ids: dict[str, Any],
    raw_key: str,
    sub_raw: bytes,
    *,
    safety: AsyncMock,
    sanity: _RecordingSanityService,
    review: _RecordingReviewService,
) -> None:
    await s3_client.upload_file(raw_key, sub_raw, "application/zip")
    ctx = {
        "session_factory": session_factory,
        "stage_router": MagicMock(),
        "s3_client": s3_client,
    }
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "course_supporter.security.stage2.run_stage2_safety_check", new=safety
            )
        )
        stack.enter_context(
            patch(
                "course_supporter.homework.sanity_gate.build_sanity_gate_service",
                new=MagicMock(return_value=sanity),
            )
        )
        stack.enter_context(
            patch(
                "course_supporter.homework.review_graph.build_mentor_review_service",
                new=MagicMock(return_value=review),
            )
        )
        await arq_process_homework(ctx, str(ids["job_id"]), str(ids["submission_id"]))


class TestFullWorkerProjectPipeline:
    async def test_no_base_full_worker_reaches_stages_and_completes(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3_client: S3Client,
    ) -> None:
        """base_id=None end-to-end: real submit → worker → all-new rich context
        (degraded staleness, no exception) → safety/sanity/review → completed."""
        s = get_settings()
        raw_key = f"homework/it-p4/{uuid.uuid4().hex}/proj.zip"
        file_url = f"{s.s3_endpoint}/{s.s3_bucket}/{raw_key}"
        ids = await _seed(session_factory, file_url=file_url)
        sub_raw = _project_zip(
            {"app/main.py": b"def solve():\n    return 42\n", "util.py": b"x = 1\n"}
        )
        safety, sanity, review = (
            _safety_mock(),
            _RecordingSanityService(),
            _RecordingReviewService(),
        )
        try:
            await _run_full_worker(
                session_factory,
                s3_client,
                ids,
                raw_key,
                sub_raw,
                safety=safety,
                sanity=sanity,
                review=review,
            )
            async with session_factory() as session:
                sub = await session.get(HomeworkSubmission, ids["submission_id"])
                assert sub is not None
                assert sub.status == "completed"
                assert sub.score == 73
                assert sub.snapshot_hash is not None

            text = safety.await_args.kwargs["submission_text"]
            # G2 — the identical rich str flowed through all three stages.
            assert sanity.seen == [text]
            assert review.seen == [text]
            # No-base rich context: all-new + degraded staleness, no crash.
            assert "SYSTEM-COMPUTED metadata below, NOT student input." in text
            assert "F2: no base attached" in text
            assert "Staleness: no base attached." in text
            assert "type=NEW path=app/main.py" in text
        finally:
            await _s3_purge(s3_client, raw_key, _submission_snapshot_key(raw_key))
            await _cleanup(session_factory, ids)

    async def test_four_branch_delta_reaches_stages_unchanged(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3_client: S3Client,
    ) -> None:
        """Acceptance closer: ONE delta triggers all four render branches at once
        — CHANGED-FULL, CHANGED-DIFF, neighbour hit, budget-overflow drop — and
        the rich context reaches safety/sanity/review UNCHANGED, run completes."""
        big_lines = _incompressible("row", 2000).decode().split("\n")  # ~150 KB
        big_base = "\n".join(big_lines).encode()
        base_snap = normalize_archive(
            _project_zip(
                {
                    "small.py": b"value = 1\n",
                    "big.py": big_base,  # > 64 KB → CHANGED-DIFF once modified
                    "config.py": b"CONFIG = True\n",  # unchanged → neighbour
                }
            ),
            archive_kind="zip",
        )
        s = get_settings()
        raw_key = f"homework/it-p4/{uuid.uuid4().hex}/proj.zip"
        file_url = f"{s.s3_endpoint}/{s.s3_bucket}/{raw_key}"
        ids = await _seed(
            session_factory,
            base_manifest=manifest_to_jsonb(base_snap.manifest),
            base_hash=base_snap.snapshot_hash,
            file_url=file_url,
        )
        await s3_client.upload_file(
            ids["base_snapshot_key"], base_snap.canonical_zip, "application/zip"
        )

        big_sub_lines = list(big_lines)
        big_sub_lines[10] = "row 000010 MODIFIED content goes here now aaaa"
        big_sub = "\n".join(big_sub_lines).encode()
        # ~350 KB each, high-entropy → two together exceed the 512 KB budget so
        # filler_b drops whole; each survives the zip-bomb guard.
        filler_a = _incompressible("AAAAMARKER", 4300)
        filler_b = _incompressible("BBBBMARKER", 4300)
        sub_raw = _project_zip(
            {
                "small.py": b"value = 2  # see config.py for the settings\n",
                "big.py": big_sub,
                "config.py": b"CONFIG = True\n",
                "filler_a.py": filler_a,  # new, large → included
                "filler_b.py": filler_b,  # new, large → overflow drop
            }
        )
        safety, sanity, review = (
            _safety_mock(),
            _RecordingSanityService(),
            _RecordingReviewService(),
        )
        try:
            await _run_full_worker(
                session_factory,
                s3_client,
                ids,
                raw_key,
                sub_raw,
                safety=safety,
                sanity=sanity,
                review=review,
            )
            async with session_factory() as session:
                sub = await session.get(HomeworkSubmission, ids["submission_id"])
                assert sub is not None
                assert sub.status == "completed"

            text = safety.await_args.kwargs["submission_text"]
            # G2 — identical rich str through all stages.
            assert sanity.seen == [text]
            assert review.seen == [text]
            # trusted block (tree + delta + F2 + staleness).
            assert "SYSTEM-COMPUTED metadata below, NOT student input." in text
            assert "F2: base has 3 files" in text
            assert "Staleness: built on base v1" in text
            # branch 1 — changed small → whole new version.
            assert "type=CHANGED-FULL path=small.py" in text
            # branch 2 — changed large → unified diff (difflib hunk present).
            assert "type=CHANGED-DIFF path=big.py" in text
            assert "@@" in text
            assert "MODIFIED content goes here now" in text
            # branch 3 — neighbour hit (unchanged base file name-dropped).
            assert "type=NEIGHBOR path=config.py" in text
            # branch 4 — budget overflow: dropped WHOLE (body absent) + marker.
            assert "SKIPPED path=filler_b.py" in text
            assert "BBBBMARKER" not in text
            # the higher-priority filler_a body IS present (proves priority).
            assert "type=NEW path=filler_a.py" in text
            assert "AAAAMARKER" in text
        finally:
            await _s3_purge(
                s3_client,
                raw_key,
                _submission_snapshot_key(raw_key),
                ids["base_snapshot_key"],
            )
            await _cleanup(session_factory, ids)
