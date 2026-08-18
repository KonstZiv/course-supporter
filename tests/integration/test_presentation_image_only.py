"""End-to-end integration for image-only / mixed presentations (road (a)).

Drives the real ``PresentationProcessor`` pipeline — real PyMuPDF extraction
of the in-repo image-only and mixed fixtures (``process_raw`` →
``process_macro`` → ``process_detail``) — against a MOCKED ``StageRouter``, so
the visual-content bridge is exercised end to end without a live model
(CI-safe; the real-LLM path stays in the RUN_SMOKE smoke test and prod
acceptance). Asserts segment content composition, not Job status: the full
``complete`` path through ``arq_ingest_material`` is covered generically by
``test_arq_task_e2e`` (mocked processor) and by the prod live acceptance
(``vision-rules#10`` — verify content, not just status).

Fixtures (committed in this task, real renders per ``impl-rules#13``):
``image_only_intro.pdf`` (3 pages, all image-only) is a verbatim cut of the
author's deck; ``mixed_slides.pdf`` (10 pages, pages 4-5 flattened to raster)
places image-only slides inside a text run.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from course_supporter.ingestion.presentation import PresentationProcessor
from course_supporter.llm.stage_router import StageResult
from course_supporter.models.source import ChunkType, SourceType
from course_supporter.service_logging import set_job_from_arq

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "presentations"


class _Source:
    """Minimal AuthoredDocument stand-in pointing at an on-disk PDF."""

    def __init__(self, path: Path, *, language: str | None = None) -> None:
        self.source_type = SourceType.PRESENTATION
        self.source_url = str(path)
        self.filename = path.name
        self.language = language


def _router(pass2a_segments: list[tuple[int, int]]) -> AsyncMock:
    """Mocked StageRouter: per-slide VD, safe verdict, fixed Pass 2a grouping.

    Pass 1 returns an identifiable ``"VD slide N"`` per slide so content
    assertions can pin the slide a piece of content came from.
    """
    router = AsyncMock()

    async def _execute(
        stage_name: str,
        *,
        response_validator=None,
        contents=None,
        **kwargs: object,
    ) -> StageResult:
        if stage_name == "presentation_pass_1_vision":
            n = int(kwargs["slide_number"])  # type: ignore[call-overload]
            return StageResult(
                content=f"VD slide {n}",
                provider_used="mock",
                model_used="m",
                attempt_count=1,
            )
        if stage_name == "safety_check_authored":
            verdict = {
                "is_safe": True,
                "violations": [],
                "confidence": 0.99,
                "reasoning": "ok",
            }
            return StageResult(
                content=json.dumps(verdict),
                provider_used="mock",
                model_used="m",
                attempt_count=1,
            )
        if stage_name == "presentation_pass_2a_mapping":
            payload = json.dumps(
                {
                    "title": "Deck",
                    "description": "A presentation.",
                    "segments": [
                        {
                            "start_slide": s,
                            "end_slide": e,
                            "title": f"Seg {s}-{e}",
                            "description": f"Covers slides {s}-{e}.",
                        }
                        for s, e in pass2a_segments
                    ],
                }
            )
            if response_validator is not None:
                response_validator(payload)
            return StageResult(
                content=payload,
                provider_used="mock",
                model_used="m",
                attempt_count=1,
            )
        raise AssertionError(f"unexpected stage {stage_name}")

    router.execute_for_stage.side_effect = _execute
    return router


@pytest.mark.skipif(
    not (_FIXTURES / "image_only_intro.pdf").exists(),
    reason="image_only_intro.pdf fixture absent",
)
async def test_all_image_only_deck_reaches_content() -> None:
    proc = PresentationProcessor()
    set_job_from_arq(uuid.uuid4())
    doc = await proc.process_raw(
        _Source(_FIXTURES / "image_only_intro.pdf", language="ukr")
    )

    # Real extraction: every slide is image-only, so no SLIDE_TEXT chunk.
    assert doc.chunks == []
    assert doc.metadata["slide_count"] == 3

    summary = await proc.process_macro(doc, _router([(1, 2), (3, 3)]))

    # The bridge gave every slide a SLIDE_VISUAL chunk in slide order.
    assert [(c.chunk_type, c.metadata["slide_number"]) for c in doc.chunks] == [
        (ChunkType.SLIDE_VISUAL, 1),
        (ChunkType.SLIDE_VISUAL, 2),
        (ChunkType.SLIDE_VISUAL, 3),
    ]

    sliced = await proc.process_detail(doc, summary)
    # Every segment carries non-empty content built from the slide VDs, in order.
    assert all(s.content and s.content.strip() for s in sliced)
    first, second = sliced[0].content or "", sliced[1].content or ""
    assert first.index("VD slide 1") < first.index("VD slide 2")
    assert "VD slide 3" in second


@pytest.mark.skipif(
    not (_FIXTURES / "mixed_slides.pdf").exists(),
    reason="mixed_slides.pdf fixture absent",
)
async def test_mixed_deck_interleaves_visual_and_text_in_slide_order() -> None:
    proc = PresentationProcessor()
    set_job_from_arq(uuid.uuid4())
    doc = await proc.process_raw(_Source(_FIXTURES / "mixed_slides.pdf"))

    # Real extraction: slides 4-5 are the flattened image-only pair, so only
    # the eight text slides emit a SLIDE_TEXT chunk in process_raw.
    text_slides = [c.metadata["slide_number"] for c in doc.chunks]
    assert text_slides == [1, 2, 3, 6, 7, 8, 9, 10]
    slide3_text = next(c.text for c in doc.chunks if c.metadata["slide_number"] == 3)
    slide6_text = next(c.text for c in doc.chunks if c.metadata["slide_number"] == 6)

    # Segment [3-6] spans text slide 3, image-only slides 4-5, text slide 6.
    summary = await proc.process_macro(doc, _router([(1, 2), (3, 6), (7, 10)]))

    # Only the two image-only slides gained a SLIDE_VISUAL chunk.
    visual_slides = [
        c.metadata["slide_number"]
        for c in doc.chunks
        if c.chunk_type == ChunkType.SLIDE_VISUAL
    ]
    assert visual_slides == [4, 5]

    sliced = await proc.process_detail(doc, summary)
    assert all(s.content and s.content.strip() for s in sliced)

    middle = sliced[1].content or ""
    # The image-only descriptions are interleaved between the text slides, in
    # slide order: text(3) → VD(4) → VD(5) → text(6).
    positions = [
        middle.index(slide3_text),
        middle.index("VD slide 4"),
        middle.index("VD slide 5"),
        middle.index(slide6_text),
    ]
    assert positions == sorted(positions)
