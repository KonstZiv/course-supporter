"""Real-API audio pipeline smoke test (RUN_SMOKE-gated; KD-2.2-A/D/E/F/I).

Drives the full AudioProcessor pipeline against a real STT provider
(ElevenLabs Scribe v2 or compatible) and a real StageRouter LLM ladder.
Skipped by default; opt-in via ``RUN_SMOKE=1`` environment variable
plus the local presence of a real audio file at
``/tmp/spike-audio/WxOfZx1OQYc.mp3``.

The RUN_SMOKE convention is greenfield in Phase 2.2 (per Decision 3):
module-level ``pytestmark`` gates the entire file in one decision,
avoiding per-test decorator boilerplate. Future phases (video, etc.)
inherit the pattern.

Local-file-skip pattern (per Decision 3): the real audio binary is
*not* committed to git (excluded per Decision 1 — only the .full.json
fixture lives in tests/fixtures/audio/). Smoke runs require the
operator to either (a) re-run the spike procedure to regenerate
/tmp/spike-audio/<id>.mp3, or (b) fetch from remote storage. Phase
2.3+ infra may codify a remote-fetch convention.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from course_supporter.config import get_settings
from course_supporter.ingestion.audio import AudioProcessor
from course_supporter.ingestion.schemas import DocumentSummaryDraft
from course_supporter.models.source import SourceType
from course_supporter.service_logging import set_job_from_arq

_LOCAL_AUDIO_PATH = Path("/tmp/spike-audio/WxOfZx1OQYc.mp3")

pytestmark = [
    pytest.mark.audio_smoke,
    pytest.mark.skipif(
        not os.getenv("RUN_SMOKE"),
        reason=(
            "Real-API audio pipeline smoke test gated by RUN_SMOKE env "
            "variable; opt-in only to avoid accidental real-API consumption."
        ),
    ),
]


@pytest.mark.skipif(
    not _LOCAL_AUDIO_PATH.exists(),
    reason=(
        f"Local audio binary absent at {_LOCAL_AUDIO_PATH}; re-run spike "
        "procedure or fetch from remote (Phase 2.3+ infra)."
    ),
)
async def test_audio_smoke_real_stt_real_llm() -> None:
    """Full pipeline against real STT + real StageRouter ladder.

    Exercises the activation moment fixated at sub-area #7 — every
    runtime tripwire (critical invariant assert in process_raw, Pass 2c
    selective routing in process_detail, factory dispatch combined-guard)
    runs against production code paths. A successful pass validates the
    end-to-end shape; failures are diagnostic for ladder, prompt, or
    Pydantic schema regressions invisible to AsyncMock-driven tests.
    """
    from arq import create_pool
    from arq.connections import RedisSettings

    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.stt.setup import create_stt_router

    settings = get_settings()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))

    # session_factory is required by create_stt_router; smoke does not
    # exercise DB IO so a minimal stand-in is acceptable.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    stt_router = create_stt_router(settings, session_factory)
    stage_router = StageRouter.from_config()

    proc = AudioProcessor(stt_router=stt_router, redis=redis)
    set_job_from_arq(uuid.uuid4())

    # AuthoredDocument stand-in — only source_url + source_type + filename
    # read by process_raw.
    class _SmokeSource:
        source_type = SourceType.AUDIO
        source_url = str(_LOCAL_AUDIO_PATH)
        filename = _LOCAL_AUDIO_PATH.name

    doc = await proc.process_raw(_SmokeSource())
    assert doc.source_type == SourceType.AUDIO
    assert len(doc.chunks) > 0

    summary = await proc.process_macro(doc, stage_router)
    assert isinstance(summary, DocumentSummaryDraft)
    assert summary.description  # non-empty doc-level synthesis
    assert len(summary.segments) > 0

    detail = await proc.process_detail(doc, summary, router=stage_router)
    assert len(detail) == len(summary.segments)
    assert all(seg.content for seg in detail)

    await redis.close()
    await engine.dispose()
