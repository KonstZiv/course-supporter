"""Real-API presentation pipeline smoke test (RUN_SMOKE-gated; KD-2.3-G/H/T).

Drives PresentationProcessor (process_raw -> process_macro -> process_detail)
against a real StageRouter ladder (Pass 1 vision + Pass 2a mapping). Skipped
by default; opt-in via ``RUN_SMOKE=1``.

Closes the gap that let the Pass 2a prompt/schema contract mismatch reach
Phase 2.3 backend merge: mock-based unit tests are schema-conformant by
construction (they hand-craft a response containing the top-level
``description``), so they never exercise the real prompt against the real
schema. This test does. Mirrors ``test_audio_pipeline_smoke.py`` (RUN_SMOKE
convention, Phase 2.2 Decision 3). A content-rich PDF fixture lives in-repo.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from course_supporter.ingestion.presentation import PresentationProcessor
from course_supporter.ingestion.schemas import DocumentSummaryDraft
from course_supporter.models.source import SourceType
from course_supporter.service_logging import set_job_from_arq

# tests/integration/<file> -> tests -> fixtures/presentations (in-repo).
_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "presentations"
    / "lesson6_functions_1.pdf"
)

pytestmark = [
    pytest.mark.presentation_smoke,
    pytest.mark.skipif(
        not os.getenv("RUN_SMOKE"),
        reason=(
            "Real-API presentation pipeline smoke test gated by RUN_SMOKE env "
            "variable; opt-in only to avoid accidental real-API consumption."
        ),
    ),
]


@pytest.mark.skipif(
    not _FIXTURE.exists(),
    reason=f"Presentation fixture absent at {_FIXTURE}.",
)
async def test_presentation_smoke_real_llm() -> None:
    """Full presentation pipeline against the real StageRouter ladder.

    Validates the Pass 2a prompt -> PresentationPass2aResult schema roundtrip
    with a real model: success proves the prompt instructs the model to emit
    the required top-level ``description`` whose omission caused the
    deterministic LadderExhaustedError fixed in the companion commit.
    """
    from course_supporter.llm.stage_router import StageRouter

    stage_router = StageRouter.from_config()

    proc = PresentationProcessor()
    set_job_from_arq(uuid.uuid4())

    class _SmokeSource:
        source_type = SourceType.PRESENTATION
        source_url = str(_FIXTURE)
        filename = _FIXTURE.name

    doc = await proc.process_raw(_SmokeSource())
    assert doc.source_type == SourceType.PRESENTATION

    summary = await proc.process_macro(doc, stage_router)
    assert isinstance(summary, DocumentSummaryDraft)
    # The fix: Pass 2a now emits the required top-level description.
    assert summary.description
    assert len(summary.segments) > 0

    detail = await proc.process_detail(doc, summary)
    assert len(detail) == len(summary.segments)
