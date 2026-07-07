"""Integration tests for the KD18 P3 worker project-branch.

Two surfaces, both on a live DB:

* ``process_project_submission`` directly, with REAL MinIO — the P3-specific
  normalize → S3 snapshot → persist → delta path, plus the fail-closed rejection.
* the full ``arq_process_homework`` on a project submission with the safety /
  sanity / review LLM stages stubbed (mirroring test_homework_pipeline) — proving
  a project flows safety → sanity → review on the interim text to ``delivered``.

Requires ``docker compose up -d`` (PostgreSQL + MinIO).
"""

from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from collections.abc import AsyncGenerator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.tasks import arq_process_homework
from course_supporter.config import get_settings
from course_supporter.homework.project_submission import process_project_submission
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
from course_supporter.storage.job_repository import JobRepository
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


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    base_manifest: dict[str, Any] | None = None,
    base_hash: str | None = None,
    job_status: str = "queued",
) -> dict[str, uuid.UUID]:
    """Tenant + node + project task + student + submission + job. Optionally a
    READY base (with a manifest) linked to the submission via base_id.

    ``job_status`` mirrors the worker precondition: the full worker sets the job
    ``active`` before the project branch, so a direct call to
    ``process_project_submission`` that hits the rejection path (which transitions
    the job to ``complete``) must seed ``active`` (not ``queued``)."""
    async with session_factory() as session:
        tenant = Tenant(name=f"p3w-{uuid.uuid4().hex[:8]}")
        session.add(tenant)
        await session.flush()
        node = make_root_course_node(tenant_id=tenant.id, title="P3W", order=0)
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
        if base_manifest is not None:
            base = ProjectBase(
                authored_document_id=doc.id,
                version=1,
                archive_key="k/v1/original.zip",
                snapshot_key="k/v1/snapshot.zip",
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
            file_url=f"s3://bucket/homework/{tenant.id}/{uuid.uuid4()}/proj.zip",
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
            job_type="homework",
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
        }


async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession], ids: dict[str, uuid.UUID]
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
            delete(AuthoredDocument).where(
                AuthoredDocument.course_node_id == ids["node_id"]
            )
        )
        await session.execute(delete(CourseNode).where(CourseNode.id == ids["node_id"]))
        await session.execute(delete(Tenant).where(Tenant.id == ids["tenant_id"]))
        await session.commit()


class TestProcessProjectSubmissionDirect:
    async def test_ready_persists_snapshot_and_returns_text(
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
                    job_repo=JobRepository(session),
                    submission=sub,
                    sid=ids["submission_id"],
                    jid=ids["job_id"],
                    file_bytes=raw,
                    raw_key=raw_key,
                )
            assert text is not None
            assert "--- app/main.py ---" in text

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
            await s3_client.delete_object(snapshot_key)
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
                    job_repo=JobRepository(session),
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
                assert "reason" in sub.safety_result
                job = await session.get(Job, ids["job_id"])
                assert job is not None
                assert job.status == "complete"
        finally:
            await _cleanup(session_factory, ids)

    async def test_no_base_delta_is_all_new(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3_client: S3Client,
    ) -> None:
        ids = await _seed(session_factory)
        raw = _project_zip({"a.py": b"a = 1\n", "b.py": b"b = 2\n"})
        try:
            with structlog.testing.capture_logs() as logs:
                async with session_factory() as session:
                    sub = await session.get(HomeworkSubmission, ids["submission_id"])
                    assert sub is not None
                    await process_project_submission(
                        session=session,
                        s3=s3_client,
                        hw_repo=HomeworkRepository(session),
                        job_repo=JobRepository(session),
                        submission=sub,
                        sid=ids["submission_id"],
                        jid=ids["job_id"],
                        file_bytes=raw,
                        raw_key=f"homework/{ids['tenant_id']}/{ids['submission_id']}/proj.zip",
                    )
            delta_log = next(
                e for e in logs if e["event"] == "project_submission.delta"
            )
            assert delta_log["new"] == 2
            assert delta_log["changed"] == 0
            assert delta_log["deleted"] == 0
            key = f"homework/{ids['tenant_id']}/{ids['submission_id']}/snapshot.zip"
            await s3_client.delete_object(key)
        finally:
            await _cleanup(session_factory, ids)

    async def test_base_delta_changed_new_deleted(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3_client: S3Client,
    ) -> None:
        # Base has a.py + gone.py; submission changes a.py, adds new.py, drops
        # gone.py → changed=1, new=1, deleted=1.
        base_snap = normalize_archive(
            _project_zip({"a.py": b"a = 1\n", "gone.py": b"g = 0\n"}),
            archive_kind="zip",
        )
        ids = await _seed(
            session_factory,
            base_manifest=manifest_to_jsonb(base_snap.manifest),
            base_hash=base_snap.snapshot_hash,
        )
        sub_raw = _project_zip({"a.py": b"a = 2\n", "new.py": b"n = 9\n"})
        try:
            with structlog.testing.capture_logs() as logs:
                async with session_factory() as session:
                    sub = await session.get(HomeworkSubmission, ids["submission_id"])
                    assert sub is not None
                    await process_project_submission(
                        session=session,
                        s3=s3_client,
                        hw_repo=HomeworkRepository(session),
                        job_repo=JobRepository(session),
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
            key = f"homework/{ids['tenant_id']}/{ids['submission_id']}/snapshot.zip"
            await s3_client.delete_object(key)
        finally:
            await _cleanup(session_factory, ids)


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


class _FakeSanityService:
    async def evaluate(
        self, *, submission: Any, submission_text: str
    ) -> SanityGateOutcome:
        del submission, submission_text
        return SanityGateOutcome(
            classification=SanityClassification(
                verdict="match", confidence=0.9, reason="on task"
            ),
            gated=False,
        )


class _FakeReviewService:
    async def review(
        self, *, submission: Any, submission_text: str
    ) -> MentorReviewOutput:
        del submission, submission_text
        return _review_output()


class TestFullWorkerProjectPipeline:
    async def test_project_flows_to_delivered_on_interim_text(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        s3_client: S3Client,
        tmp_path: Path,
    ) -> None:
        """A project submission runs through the real worker: normalize (real
        MinIO snapshot) → safety → sanity → review (stubbed) → delivered, and the
        stubbed safety receives the bounded interim text."""
        ids = await _seed(session_factory)
        raw = _project_zip({"app/main.py": b"def solve():\n    return 42\n"})
        raw_key = f"homework/{ids['tenant_id']}/{ids['submission_id']}/proj.zip"
        await s3_client.upload_file(raw_key, raw, "application/zip")
        tmp_file = tmp_path / "proj.zip"
        tmp_file.write_bytes(raw)

        s3_ctx = MagicMock()
        s3_ctx.extract_key = MagicMock(return_value=raw_key)
        s3_ctx.download_file = AsyncMock(return_value=tmp_file)
        s3_ctx.upload_file = AsyncMock()  # snapshot PUT (assert called)
        ctx = {
            "session_factory": session_factory,
            "stage_router": MagicMock(),
            "s3_client": s3_ctx,
        }

        safety = AsyncMock(
            return_value=SafetyResult(
                is_safe=True, violations=[], confidence=0.95, reasoning="benign"
            )
        )
        try:
            with ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "course_supporter.security.stage2.run_stage2_safety_check",
                        new=safety,
                    )
                )
                stack.enter_context(
                    patch(
                        "course_supporter.homework.sanity_gate.build_sanity_gate_service",
                        new=MagicMock(return_value=_FakeSanityService()),
                    )
                )
                stack.enter_context(
                    patch(
                        "course_supporter.homework.review_graph.build_mentor_review_service",
                        new=MagicMock(return_value=_FakeReviewService()),
                    )
                )
                await arq_process_homework(
                    ctx, str(ids["job_id"]), str(ids["submission_id"])
                )

            async with session_factory() as session:
                sub = await session.get(HomeworkSubmission, ids["submission_id"])
                assert sub is not None
                # Terminal success — the seed carries no webhook_url, so the
                # pipeline terminates at 'completed' (delivery needs a webhook).
                assert sub.status == "completed"
                assert sub.score == 73
                # The P3 snapshot columns were persisted before safety.
                assert sub.snapshot_hash is not None
                assert sub.snapshot_manifest is not None

            # The snapshot PUT happened; safety saw the interim project text.
            s3_ctx.upload_file.assert_awaited_once()
            safety.assert_awaited_once()
            interim = safety.await_args.kwargs["submission_text"]
            assert "--- app/main.py ---" in interim
        finally:
            await _cleanup(session_factory, ids)
