"""End-to-end topology tests for the video_pipeline (Phase 2.4).

Drives the real ``arq_ingest_material`` orchestrator against a live DB
with the ``VideoProcessor`` injected via ``create_processors``. Krok 1-2
are real (task 2.4.2); the external toolchain (ffmpeg/ffprobe) and the
ElevenLabs Scribe core are mocked in the ``requires_db`` tests so they
stay independent of ffmpeg, and exercised for real in the separate
``requires_ffmpeg`` test on an ffmpeg-generated fixture (no binary in
VCS). Stage 2 safety + the ARQ work-window are patched.

* happy path — ``source_type=video`` traverses all 7 gnízda → ``state=ready``
  with a persisted ``DocumentSummary`` + ``DocumentSegment[]`` (cascade
  content_hash to root);
* R1 failure path — a gnízdo raising → ``state=ERROR`` with non-empty
  ``error_message``, the row **retained** (NOT soft-deleted, KD-2.1-P +
  task 2.4.8 retry), and nothing persisted;
* real-ffmpeg path — a 1 s ``tiny.mp4`` generated at runtime runs through
  real ffprobe + ffmpeg (mock STT) → ``state=ready``.

Requires ``docker compose up -d`` (PostgreSQL).
Run with: ``uv run pytest tests/integration/test_video_skeleton.py --run-db -v``
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from course_supporter.api.tasks import arq_ingest_material
from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.video_pipeline import VideoProcessor
from course_supporter.ingestion.video_pipeline.schemas import (
    ChangeClass,
    DetectionResult,
    SampledFrame,
    Scene,
    VideoFileMetadata,
)
from course_supporter.models.source import SourceType
from course_supporter.storage.authored_document_repository import (
    AuthoredDocumentRepository,
)
from course_supporter.storage.course_node_repository import CourseNodeRepository
from course_supporter.storage.job_repository import JobRepository
from course_supporter.storage.orm import (
    AuthoredDocument,
    DocumentSegment,
    DocumentSummary,
)
from course_supporter.stt.router import STTRouter
from course_supporter.stt.schemas import STTResult, STTWord

pytestmark = pytest.mark.requires_db

_FACTORY = "course_supporter.api.tasks.create_processors"
_HEAVY = "course_supporter.api.tasks.create_heavy_steps"
_WORK_WINDOW = "course_supporter.job_priority.check_work_window"
_STAGE2 = "course_supporter.security.stage2.run_stage2_safety_check"
_PKG = "course_supporter.ingestion.video_pipeline"
_PROBE = f"{_PKG}.media.probe_metadata"
_EXTRACT = f"{_PKG}.media.extract_audio"
_STEP3 = f"{_PKG}.steps.step_3_detection"
_STEP4 = f"{_PKG}.steps.step_4_pass1_vision"

_SOURCE_URL = "s3://bucket/lecture.mp4"
_MOCK_META = VideoFileMetadata(duration_ms=60_000, codec="h264", resolution="1280x720")


@pytest.fixture
def tiny_video(tmp_path: Path) -> Path:
    """A 4 s 320x240 h264+aac mp4 generated via ffmpeg (requires_ffmpeg only).

    Avoids a committed binary (``*.mp4`` is gitignored): the fixture only
    materialises for the ``requires_ffmpeg`` test, which is skipped when
    ffmpeg is absent — so ``shutil.which`` always resolves here. 4 s so
    Krok 3 (fps=0.5) extracts more than one frame.
    """
    out = tmp_path / "tiny.mp4"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-pix_fmt",
            "yuv420p",
            "-shortest",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


def _stt_router_with_words() -> AsyncMock:
    """STTRouter mock returning a two-word transcript (no ElevenLabs call)."""
    router = AsyncMock(spec=STTRouter)
    router.transcribe = AsyncMock(
        return_value=STTResult(
            text="hello world",
            words=[
                STTWord(start_sec=0.0, end_sec=0.5, text="hello"),
                STTWord(start_sec=0.6, end_sec=1.0, text="world"),
            ],
            language="en",
            detected_language="en",
            provider="elevenlabs",
            model_id="scribe_v2",
        )
    )
    return router


def _build_ctx(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """Minimal ARQ worker context for the ingestion task."""
    return {
        "session_factory": session_factory,
        "model_router": MagicMock(),
        "stage_router": MagicMock(),
        "redis": AsyncMock(),
        "s3_client": None,
    }


def _video_processors(
    stt_router: AsyncMock | None = None,
) -> dict[SourceType, VideoProcessor]:
    """Inject the real VideoProcessor at the VIDEO dispatch key.

    The vision call (Krok 4) is patched at the ``step_4_pass1_vision`` seam
    in each test, so the injected ``stage_router`` is a bare mock here.
    """
    return {
        SourceType.VIDEO: VideoProcessor(
            stt_router=stt_router or _stt_router_with_words(),
            redis=AsyncMock(),
            stage_router=MagicMock(),
        )
    }


def _detection_result() -> DetectionResult:
    """One-scene detection stand-in (keeps the ffmpeg-free topology test off cv2)."""
    return DetectionResult(
        scenes=[
            Scene(
                scene_id=0,
                start_ms=0,
                end_ms=1000,
                frames=[
                    SampledFrame(
                        frame_position_ms=0,
                        change_class=ChangeClass.FIRST,
                        frame_path=Path("/tmp/x/frames/frame_000001.jpg"),
                    )
                ],
            )
        ],
        pip_mask=None,
    )


async def _seed_video_job(
    session_factory: async_sessionmaker[AsyncSession],
    committed_seeds: dict[str, uuid.UUID],
    *,
    source_url: str = _SOURCE_URL,
) -> uuid.UUID:
    """Flip the seeded document to source_type=video + source_url and add a Job."""
    async with session_factory() as session:
        await session.execute(
            update(AuthoredDocument)
            .where(AuthoredDocument.id == committed_seeds["material_id"])
            .values(source_type="video", source_url=source_url)
        )
        job = await JobRepository(session).create(
            tenant_id=committed_seeds["tenant_id"],
            course_node_id=committed_seeds["course_node_id"],
            job_type="ingest",
        )
        await session.commit()
        return job.id


def _safe_verdict() -> Any:
    from course_supporter.security.schemas import SafetyResult

    return SafetyResult(
        is_safe=True,
        violations=[],
        confidence=0.95,
        reasoning="benign",
    )


async def _assert_ready_with_cascade(
    session_factory: async_sessionmaker[AsyncSession],
    mid: uuid.UUID,
    job_id: uuid.UUID,
) -> None:
    """Shared assertions: state=ready + Summary + Segments + cascade hash."""
    async with session_factory() as session:
        final_job = await JobRepository(session).get_by_id(job_id)
        final_mat = await AuthoredDocumentRepository(session).get_by_id(mid)
    assert final_job is not None
    assert final_job.status == "complete"
    assert final_mat is not None
    assert final_mat.state == "ready"
    assert final_mat.error_message is None

    async with session_factory() as session:
        summary = (
            await session.execute(
                select(DocumentSummary).where(
                    DocumentSummary.authored_document_id == mid
                )
            )
        ).scalar_one_or_none()
    assert summary is not None, "Pass 2a did not persist a summary"

    async with session_factory() as session:
        segments = list(
            (
                await session.execute(
                    select(DocumentSegment)
                    .where(DocumentSegment.document_summary_id == summary.id)
                    .order_by(DocumentSegment.order)
                )
            )
            .scalars()
            .all()
        )
    assert len(segments) >= 1, "no segment rows persisted"
    assert all(seg.content for seg in segments)

    async with session_factory() as session:
        node_repo = CourseNodeRepository(session)
        entry = await AuthoredDocumentRepository(session).get_by_id(mid)
        assert entry is not None
        root = await node_repo.get_root_for(entry.course_node_id)
    assert summary.content_hash is not None
    assert entry.content_hash is not None
    assert root is not None
    assert root.content_hash is not None


class TestVideoTopology:
    """Acceptance #1-#6 — full orchestrator topology over real Krok 1-2."""

    async def test_reaches_ready_with_persisted_cascade(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
    ) -> None:
        """Krok 1-3 (mocked toolchain/cv2) + stub 4-7 → state=ready + persist."""
        mid = committed_seeds["material_id"]
        job_id = await _seed_video_job(session_factory, committed_seeds)
        ctx = _build_ctx(session_factory)

        with (
            patch(_WORK_WINDOW),
            patch(_HEAVY),
            patch(_FACTORY, return_value=_video_processors()),
            patch(_STAGE2, new=AsyncMock(return_value=_safe_verdict())),
            patch(_PROBE, new=AsyncMock(return_value=_MOCK_META)),
            patch(_EXTRACT, new=AsyncMock(return_value=Path("/tmp/x/audio.mp3"))),
            patch(_STEP3, new=AsyncMock(return_value=_detection_result())),
            patch(_STEP4, new=AsyncMock(return_value=[])),
        ):
            await arq_ingest_material(ctx, str(job_id), str(mid), "video", _SOURCE_URL)

        await _assert_ready_with_cascade(session_factory, mid, job_id)

    async def test_failure_path_marks_error_without_soft_delete(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
    ) -> None:
        """R1 — Krok 1 ProcessingError → state=ERROR, row retained, no persist."""
        mid = committed_seeds["material_id"]
        job_id = await _seed_video_job(session_factory, committed_seeds)
        ctx = _build_ctx(session_factory)

        error_msg = "ffprobe failed: corrupt container"

        with (
            patch(_WORK_WINDOW),
            patch(_HEAVY),
            patch(_FACTORY, return_value=_video_processors()),
            patch(_PROBE, new=AsyncMock(side_effect=ProcessingError(error_msg))),
        ):
            await arq_ingest_material(ctx, str(job_id), str(mid), "video", _SOURCE_URL)

        async with session_factory() as session:
            final_job = await JobRepository(session).get_by_id(job_id)
            final_mat = await AuthoredDocumentRepository(session).get_by_id(mid)

        assert final_job is not None
        assert final_job.status == "failed"
        assert error_msg in (final_job.error_message or "")

        assert final_mat is not None
        assert final_mat.state == "error"
        assert error_msg in (final_mat.error_message or "")
        assert final_mat.deleted_at is None

        async with session_factory() as session:
            summary = (
                await session.execute(
                    select(DocumentSummary).where(
                        DocumentSummary.authored_document_id == mid
                    )
                )
            ).scalar_one_or_none()
        assert summary is None, "no DocumentSummary should persist on failure"


@pytest.mark.requires_ffmpeg
class TestVideoRealFfmpeg:
    """Acceptance #1/#3 — real ffprobe + ffmpeg over the committed fixture."""

    async def test_real_toolchain_reaches_ready(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        committed_seeds: dict[str, uuid.UUID],
        tiny_video: Path,
    ) -> None:
        """tiny.mp4 → real ffprobe metadata + real audio extract (mock STT)."""
        mid = committed_seeds["material_id"]
        job_id = await _seed_video_job(
            session_factory, committed_seeds, source_url=str(tiny_video)
        )
        ctx = _build_ctx(session_factory)

        with (
            patch(_WORK_WINDOW),
            patch(_HEAVY),
            patch(_FACTORY, return_value=_video_processors()),
            patch(_STAGE2, new=AsyncMock(return_value=_safe_verdict())),
            # Real ffprobe + ffmpeg + cv2 detection; the vision LLM (Krok 4)
            # stays mocked — real Pass 1 is the RUN_SMOKE calibration concern.
            patch(_STEP4, new=AsyncMock(return_value=[])),
        ):
            await arq_ingest_material(
                ctx, str(job_id), str(mid), "video", str(tiny_video)
            )

        await _assert_ready_with_cascade(session_factory, mid, job_id)
