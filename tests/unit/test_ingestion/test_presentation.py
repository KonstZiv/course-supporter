"""Tests for the three-stage PresentationProcessor (Phase 2.3 sub-area #4).

Covers the rewritten processor: process_raw extraction + v0.3 N3
empty-slide filter + critical invariant + v0.3 N4 tempfile cleanup;
process_macro Pass 1 fail-fast + Pass 2a fence-strip validator;
process_detail Pass 2b slice; the ``chars_per_slide_cumsum`` bridge;
and a real-PyMuPDF PDF-path integration test. The PPTX LibreOffice
subprocess is mocked (no ``soffice`` dependency in unit tests; the
Dockerfile install lands in sub-area #7).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
from course_supporter.ingestion.presentation import (
    PresentationProcessor,
    SlideRaw,
    _strip_json_fences,
    chars_per_slide_cumsum,
)
from course_supporter.ingestion.schemas import (
    DocumentSegmentDraft,
    DocumentSummaryDraft,
)
from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.llm.stage_router import StageResult
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)
from course_supporter.security.exceptions import ErrorCategory, SecurityRejectedError

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "presentations"


# ── Helpers ─────────────────────────────────────────────────────────


def _make_source(
    file_path: Path,
    *,
    source_type: SourceType = SourceType.PRESENTATION,
) -> MagicMock:
    """Mock AuthoredDocument pointing at a real on-disk file."""
    source = MagicMock()
    source.source_type = source_type
    source.source_url = str(file_path)
    source.filename = file_path.name
    return source


def _pdf_source(tmp_path: Path, name: str = "slides.pdf") -> MagicMock:
    file_path = tmp_path / name
    file_path.write_bytes(b"%PDF-1.4 dummy")
    return _make_source(file_path)


def _slides(*specs: tuple[int, str]) -> list[SlideRaw]:
    """Build SlideRaw list from (slide_number, raw_text) specs."""
    return [
        SlideRaw(slide_number=n, raw_text=text, image_bytes=b"\x89PNG\r\n\x1a\n")
        for n, text in specs
    ]


def _ok_result(content: str, *, provider: str = "p", model: str = "m") -> StageResult:
    return StageResult(
        content=content,
        provider_used=provider,
        model_used=model,
        attempt_count=1,
    )


def _safety_json(*, is_safe: bool) -> str:
    """A SafetyResult JSON body as the safety_check_authored stage returns."""
    return json.dumps(
        {
            "is_safe": is_safe,
            "violations": [] if is_safe else ["prompt_injection"],
            "confidence": 0.99,
            "reasoning": "ok" if is_safe else "injected instructions in slide image",
        }
    )


def _make_router(
    *,
    pass1_by_slide: dict[int, str] | None = None,
    pass1_error_slide: int | None = None,
    pass2a_payload: str | None = None,
    safety_safe: bool = True,
) -> AsyncMock:
    """Fake StageRouter for process_macro.

    Pass 1 returns a per-slide description (or raises on a chosen
    slide for the fail-fast test). ``safety_check_authored`` (the
    ``_bridge_visual_content`` safety point, fired only when a deck has
    image-only slides) returns a SafetyResult verdict gated by
    ``safety_safe``. Pass 2a invokes the supplied ``response_validator``
    with ``pass2a_payload`` then returns it.
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
            slide_number = int(kwargs["slide_number"])  # type: ignore[call-overload]
            if pass1_error_slide is not None and slide_number == pass1_error_slide:
                raise RuntimeError(f"vision failed on slide {slide_number}")
            desc = (pass1_by_slide or {}).get(slide_number, f"desc {slide_number}")
            return _ok_result(desc)
        if stage_name == "safety_check_authored":
            return _ok_result(_safety_json(is_safe=safety_safe))
        if stage_name == "presentation_pass_2a_mapping":
            assert pass2a_payload is not None
            if response_validator is not None:
                response_validator(pass2a_payload)
            return _ok_result(pass2a_payload, provider="mistral", model="mistral-large")
        raise AssertionError(f"unexpected stage {stage_name}")

    router.execute_for_stage.side_effect = _execute
    return router


def _pass2a_json(
    segments: list[tuple[int, int]],
    *,
    title: str = "Deck",
    concepts: list[tuple[list[str], list[str]]] | None = None,
) -> str:
    """Build a Pass 2a payload with optional per-segment concept lists.

    ``concepts`` is a parallel list to ``segments`` carrying
    ``(main_concepts, secondary_concepts)`` per index; ``None`` omits
    the concept keys (legacy callsites) and relies on the schema
    defaults (``[]`` from ``default_factory=list``).
    """
    payload_segments: list[dict[str, object]] = []
    for idx, (s, e) in enumerate(segments):
        seg: dict[str, object] = {
            "start_slide": s,
            "end_slide": e,
            "title": f"Seg {s}-{e}",
            "description": f"Covers slides {s}-{e}.",
        }
        if concepts is not None:
            main_c, sec_c = concepts[idx]
            seg["main_concepts"] = main_c
            seg["secondary_concepts"] = sec_c
        payload_segments.append(seg)
    return json.dumps(
        {
            "title": title,
            "description": "A presentation.",
            "segments": payload_segments,
        }
    )


# ── _strip_json_fences ──────────────────────────────────────────────


class TestStripJsonFences:
    def test_fenced_json(self) -> None:
        assert _strip_json_fences('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_bare_json(self) -> None:
        assert _strip_json_fences('{"a": 1}') == '{"a": 1}'

    def test_fence_without_lang(self) -> None:
        assert _strip_json_fences('```\n{"a": 1}\n```') == '{"a": 1}'


# ── chars_per_slide_cumsum (KD-2.3-Q, Flag 2 bundle) ────────────────


class TestCharsPerSlideCumsum:
    def test_empty_chunks_baseline(self) -> None:
        assert chars_per_slide_cumsum([]) == [0]

    def test_single_slide_no_trailing_separator(self) -> None:
        chunks = [ContentChunk(chunk_type=ChunkType.SLIDE_TEXT, text="abc")]
        # Single (last) chunk adds no trailing "\n\n".
        assert chars_per_slide_cumsum(chunks) == [0, 3]

    def test_two_slides_explicit_separator_math(self) -> None:
        chunks = [
            ContentChunk(chunk_type=ChunkType.SLIDE_TEXT, text="ab"),
            ContentChunk(chunk_type=ChunkType.SLIDE_TEXT, text="cd"),
        ]
        # c0: 2 + 2 (sep) = 4; c1 (last): 2 + 0 = 6.
        assert chars_per_slide_cumsum(chunks) == [0, 4, 6]

    def test_multi_slide(self) -> None:
        chunks = [
            ContentChunk(chunk_type=ChunkType.SLIDE_TEXT, text="a"),
            ContentChunk(chunk_type=ChunkType.SLIDE_TEXT, text="bb"),
            ContentChunk(chunk_type=ChunkType.SLIDE_TEXT, text="ccc"),
        ]
        assert chars_per_slide_cumsum(chunks) == [0, 3, 7, 10]

    def test_last_entry_matches_assemble_text_length(self) -> None:
        # v0.3 N3 cross-ref: the doc was built from a slide sequence
        # where slide 2 was image-only (filtered), so doc.chunks holds
        # only the non-empty slides. The cumsum last entry must still
        # equal len(assemble_text(doc)).
        doc = SourceDocument(
            source_type=SourceType.PRESENTATION,
            source_url="file:///x.pdf",
            chunks=[
                ContentChunk(
                    chunk_type=ChunkType.SLIDE_TEXT,
                    text="Slide one body",
                    index=1,
                    metadata={"slide_number": 1},
                ),
                ContentChunk(
                    chunk_type=ChunkType.SLIDE_TEXT,
                    text="Slide three body",
                    index=3,
                    metadata={"slide_number": 3},
                ),
            ],
        )
        cumsum = chars_per_slide_cumsum(doc.chunks)
        assert cumsum[-1] == len(doc.assemble_text())


# ── process_raw ─────────────────────────────────────────────────────


class TestProcessRaw:
    async def test_pdf_path_emits_slide_text_chunks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, "Alpha"), (2, "Beta"))),
        )

        doc = await proc.process_raw(_pdf_source(tmp_path))

        assert doc.source_type == SourceType.PRESENTATION
        assert [c.chunk_type for c in doc.chunks] == [
            ChunkType.SLIDE_TEXT,
            ChunkType.SLIDE_TEXT,
        ]
        assert [c.text for c in doc.chunks] == ["Alpha", "Beta"]
        assert doc.metadata["slide_count"] == 2
        # Instance-state hand-off populated for process_macro.
        assert proc._slide_raw is not None
        assert len(proc._slide_raw) == 2

    async def test_v0_3_n3_empty_slide_filter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stage 1 (process_raw) only: raw_text=("Slide 1", "", "Slide 3") →
        # only slides 1 + 3 emit a SLIDE_TEXT chunk here; slide_number
        # metadata is preserved and the full 3-slide sequence stays on
        # _slide_raw for Pass 1. The image-only slide 2 is NOT contentless
        # downstream — process_macro's bridge later gives it a SLIDE_VISUAL
        # chunk (see TestVisualContentBridge); this filter is a Stage-1 fact.
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, "Slide 1"), (2, ""), (3, "Slide 3"))),
        )

        doc = await proc.process_raw(_pdf_source(tmp_path))

        assert len(doc.chunks) == 2
        assert doc.chunks[0].metadata["slide_number"] == 1
        assert doc.chunks[1].metadata["slide_number"] == 3
        assert proc._slide_raw is not None
        assert len(proc._slide_raw) == 3  # full sequence retained

    async def test_critical_invariant_holds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, "One"), (2, ""), (3, "Three"))),
        )

        doc = await proc.process_raw(_pdf_source(tmp_path))

        expected = "\n\n".join(c.text for c in doc.chunks if c.text)
        assert doc.assemble_text() == expected

    async def test_wrong_source_type_raises(self, tmp_path: Path) -> None:
        proc = PresentationProcessor()
        bad = _pdf_source(tmp_path)
        bad.source_type = SourceType.TEXT
        with pytest.raises(UnsupportedFormatError, match="expects 'presentation'"):
            await proc.process_raw(bad)

    async def test_unsupported_extension_raises(self, tmp_path: Path) -> None:
        proc = PresentationProcessor()
        bad_file = tmp_path / "notes.txt"
        bad_file.write_bytes(b"x")
        with pytest.raises(UnsupportedFormatError, match="Unsupported presentation"):
            await proc.process_raw(_make_source(bad_file))

    async def test_pptx_path_invokes_normalize(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = PresentationProcessor()
        normalize = AsyncMock(return_value=b"%PDF-1.4 converted")
        monkeypatch.setattr(proc, "_normalize_pptx_to_pdf", normalize)
        monkeypatch.setattr(
            proc, "_extract_pdf_pages", MagicMock(return_value=_slides((1, "Slide")))
        )

        pptx_file = tmp_path / "deck.pptx"
        pptx_file.write_bytes(b"PK fake pptx")
        doc = await proc.process_raw(_make_source(pptx_file))

        normalize.assert_awaited_once()
        assert len(doc.chunks) == 1

    async def test_normalize_cleanup_on_extract_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # v0.3 N4: even when _extract_pdf_pages raises downstream, the
        # tempfile.TemporaryDirectory opened by _normalize_pptx_to_pdf is
        # cleaned up (the context manager exits on normalize return,
        # before extract runs).
        created_dirs: list[str] = []
        real_td = tempfile.TemporaryDirectory

        def _spy_td(*args: object, **kwargs: object) -> object:
            td = real_td(*args, **kwargs)  # type: ignore[arg-type]
            created_dirs.append(td.name)
            return td

        monkeypatch.setattr(tempfile, "TemporaryDirectory", _spy_td)

        async def _fake_subprocess(*args: str, **kwargs: object) -> MagicMock:
            outdir = args[args.index("--outdir") + 1]
            input_path = args[-1]
            stem = Path(input_path).stem
            (Path(outdir) / f"{stem}.pdf").write_bytes(b"%PDF-1.4 fake")
            proc_mock = MagicMock()
            proc_mock.communicate = AsyncMock(return_value=(b"", b""))
            proc_mock.returncode = 0
            return proc_mock

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess)

        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(side_effect=RuntimeError("extract boom")),
        )

        pptx_file = tmp_path / "deck.pptx"
        pptx_file.write_bytes(b"PK fake pptx")

        with pytest.raises(RuntimeError, match="extract boom"):
            await proc.process_raw(_make_source(pptx_file))

        assert created_dirs, "normalize did not open a TemporaryDirectory"
        tmpdir_exists = await asyncio.to_thread(Path(created_dirs[0]).exists)
        assert not tmpdir_exists


# ── _normalize_pptx_to_pdf error paths ──────────────────────────────


class TestSlideCountLimit:
    """CA-3 — worker-side slide-count cap in _extract_pdf_pages (KD-2.3-F)."""

    @staticmethod
    def _write_pdf(path: Path, n_pages: int) -> Path:
        import fitz

        doc = fitz.open()
        for _ in range(n_pages):
            doc.new_page()
        doc.save(str(path))
        doc.close()
        return path

    async def test_at_limit_passes(self, tmp_path: Path) -> None:
        # Exactly 100 slides is allowed; only > 100 rejects (boundary).
        pdf = self._write_pdf(tmp_path / "deck100.pdf", 100)
        proc = PresentationProcessor()
        doc = await proc.process_raw(_make_source(pdf))
        assert doc.metadata["slide_count"] == 100

    async def test_over_limit_raises(self, tmp_path: Path) -> None:
        pdf = self._write_pdf(tmp_path / "deck101.pdf", 101)
        proc = PresentationProcessor()
        with pytest.raises(ProcessingError, match="PRESENTATION_SLIDE_LIMIT"):
            await proc.process_raw(_make_source(pdf))


class TestNormalizeErrors:
    async def test_nonzero_exit_raises_normalize_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fail_subprocess(*args: str, **kwargs: object) -> MagicMock:
            proc_mock = MagicMock()
            proc_mock.communicate = AsyncMock(return_value=(b"", b"conversion error"))
            proc_mock.returncode = 1
            return proc_mock

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail_subprocess)
        proc = PresentationProcessor()
        with pytest.raises(ProcessingError, match="PPTX_NORMALIZE_FAILED"):
            await proc._normalize_pptx_to_pdf(str(tmp_path / "deck.pptx"))

    async def test_timeout_raises_normalize_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _hang_subprocess(*args: str, **kwargs: object) -> MagicMock:
            proc_mock = MagicMock()
            proc_mock.communicate = AsyncMock(side_effect=TimeoutError())
            proc_mock.kill = MagicMock()
            proc_mock.wait = AsyncMock()
            return proc_mock

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _hang_subprocess)
        proc = PresentationProcessor()
        with pytest.raises(ProcessingError, match="PPTX_NORMALIZE_TIMEOUT"):
            await proc._normalize_pptx_to_pdf(str(tmp_path / "deck.pptx"))


# ── process_macro ───────────────────────────────────────────────────


class TestProcessMacro:
    async def test_cache_miss_guard_raises(self) -> None:
        # process_macro before process_raw → _slide_raw is None.
        proc = PresentationProcessor()
        doc = SourceDocument(
            source_type=SourceType.PRESENTATION, source_url="file:///x.pdf"
        )
        with pytest.raises(ProcessingError, match="requires process_raw"):
            await proc.process_macro(doc, _make_router(pass2a_payload="{}"))

    async def test_pass_1_fail_fast_on_slide_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, "A"), (2, "B"), (3, "C"))),
        )
        doc = await proc.process_raw(_pdf_source(tmp_path))

        router = _make_router(
            pass1_error_slide=2, pass2a_payload=_pass2a_json([(1, 3)])
        )
        with pytest.raises(ProcessingError, match="Pass 1 failed on slide 2"):
            await proc.process_macro(doc, router)

    async def test_pass_2a_fence_wrapped_json_parses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, "A"), (2, "B"), (3, "C"))),
        )
        doc = await proc.process_raw(_pdf_source(tmp_path))

        fenced = "```json\n" + _pass2a_json([(1, 2), (3, 3)]) + "\n```"
        router = _make_router(pass2a_payload=fenced)
        summary = await proc.process_macro(doc, router)

        assert summary.title == "Deck"
        assert len(summary.segments) == 2
        assert summary.main_concepts == []
        assert summary.secondary_concepts == []

    async def test_pass_2a_invalid_json_raises_structural_retry(self) -> None:
        # Drive the validator directly with malformed output; it must
        # translate the failure into StructuralRetryError so the ladder
        # retry path fires.
        proc = PresentationProcessor()
        proc._slide_raw = _slides((1, "A"), (2, "B"))
        doc = SourceDocument(
            source_type=SourceType.PRESENTATION,
            source_url="file:///x.pdf",
            chunks=[
                ContentChunk(
                    chunk_type=ChunkType.SLIDE_TEXT,
                    text="A",
                    metadata={"slide_number": 1},
                ),
                ContentChunk(
                    chunk_type=ChunkType.SLIDE_TEXT,
                    text="B",
                    metadata={"slide_number": 2},
                ),
            ],
        )

        # Single segment violates Miller's rule (min 2) → retry.
        bad_payload = json.dumps(
            {
                "description": "d",
                "segments": [{"start_slide": 1, "end_slide": 2, "description": "x"}],
            }
        )

        async def _execute(stage_name: str, *, response_validator=None, **kw: object):
            if stage_name == "presentation_pass_1_vision":
                return _ok_result("desc")
            response_validator(bad_payload)
            return _ok_result("{}")

        router = AsyncMock()
        router.execute_for_stage.side_effect = _execute

        with pytest.raises(StructuralRetryError):
            await proc.process_macro(doc, router)

    async def test_segment_drafts_bridge_offsets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, "Intro"), (2, "Body"), (3, "End"))),
        )
        doc = await proc.process_raw(_pdf_source(tmp_path))
        # assemble_text = "Intro\n\nBody\n\nEnd"; cumsum = [0, 7, 13, 16]
        router = _make_router(pass2a_payload=_pass2a_json([(1, 2), (3, 3)]))
        summary = await proc.process_macro(doc, router)

        assert [(s.start_pos, s.end_pos) for s in summary.segments] == [
            (0, 13),
            (13, 16),
        ]
        assert [(s.start_slide, s.end_slide) for s in summary.segments] == [
            (1, 2),
            (3, 3),
        ]

    async def test_segment_over_image_only_slide_gets_visual_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Inversion of the old fail-fast: a segment whose slide is image-only
        # now carries that slide's VD as content (road (a)) instead of raising
        # PRESENTATION_EMPTY_SEGMENT.
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, "Intro"), (2, ""), (3, "End"))),
        )
        doc = await proc.process_raw(_pdf_source(tmp_path))
        router = _make_router(pass2a_payload=_pass2a_json([(1, 1), (2, 2), (3, 3)]))

        summary = await proc.process_macro(doc, router)

        # Slide 2 now owns a SLIDE_VISUAL chunk carrying its Pass 1 description,
        # merged into slide order between the two text slides.
        assert [(c.chunk_type, c.text) for c in doc.chunks] == [
            (ChunkType.SLIDE_TEXT, "Intro"),
            (ChunkType.SLIDE_VISUAL, "desc 2"),
            (ChunkType.SLIDE_TEXT, "End"),
        ]
        # assemble_text = "Intro\n\ndesc 2\n\nEnd"; cumsum = [0, 7, 15, 18]
        assert [(s.start_pos, s.end_pos) for s in summary.segments] == [
            (0, 7),
            (7, 15),
            (15, 18),
        ]
        # Pass 2b slices the visual slide's content out of the same reference;
        # the non-final segment carries its trailing inter-slide "\n\n"
        # (pre-existing presentation Pass 2b behaviour, cf. the text case above).
        sliced = await proc.process_detail(doc, summary)
        assert sliced[1].content == "desc 2\n\n"


# ── Pass 2a concept aggregation + passthrough (task 2.4.15) ─────────


class TestPass2aConceptAggregation:
    """Pass 2a now emits per-segment concepts; doc-level is union+dedup.

    Task 2.4.15 lifts KD2d (a): the Pass 2a output carries
    ``main_concepts`` / ``secondary_concepts`` per segment.
    ``process_macro`` aggregates them to document level using the
    ``sorted(set-union)`` pattern shared with text/audio (KD-2.1-O),
    applying the conflict rule ``all_secondary -= all_main``.
    """

    async def test_doc_level_concepts_aggregate_via_union_with_conflict_rule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, "A"), (2, "B"), (3, "C"))),
        )
        doc = await proc.process_raw(_pdf_source(tmp_path))

        # Segment 0: main=["variable", "змінна"], secondary=["modulo"]
        # Segment 1: main=["modulo"], secondary=["operator"]
        # Conflict: "modulo" is main in seg1 and secondary in seg0 →
        # the union+dedup rule keeps it in main_concepts and removes
        # it from secondary_concepts.
        payload = _pass2a_json(
            [(1, 2), (3, 3)],
            concepts=[
                (["variable", "змінна"], ["modulo"]),
                (["modulo"], ["operator"]),
            ],
        )
        router = _make_router(pass2a_payload=payload)
        summary = await proc.process_macro(doc, router)

        # Doc-level main = sorted union of all segment mains.
        assert summary.main_concepts == sorted({"variable", "змінна", "modulo"})
        # Doc-level secondary = (union of seg seconds) - (union of seg mains);
        # "modulo" was promoted to main by seg1, so it leaves secondary.
        assert summary.secondary_concepts == ["operator"]

    async def test_segment_drafts_carry_concept_lists_from_pass2a(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, "Intro"), (2, "Body"), (3, "End"))),
        )
        doc = await proc.process_raw(_pdf_source(tmp_path))

        payload = _pass2a_json(
            [(1, 2), (3, 3)],
            concepts=[
                (["variable"], ["assignment"]),
                (["arithmetic operator"], ["modulo"]),
            ],
        )
        router = _make_router(pass2a_payload=payload)
        summary = await proc.process_macro(doc, router)

        assert [seg.main_concepts for seg in summary.segments] == [
            ["variable"],
            ["arithmetic operator"],
        ]
        assert [seg.secondary_concepts for seg in summary.segments] == [
            ["assignment"],
            ["modulo"],
        ]

    async def test_legacy_payload_without_concept_keys_defaults_to_empty_lists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Defence-in-depth: a Pass 2a payload that omits the new
        # concept keys (e.g. a model that did not pick up the prompt
        # update yet) must still parse — the schema defaults populate
        # empty lists at both segment and doc level.
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, "A"), (2, "B"))),
        )
        doc = await proc.process_raw(_pdf_source(tmp_path))
        router = _make_router(pass2a_payload=_pass2a_json([(1, 1), (2, 2)]))

        summary = await proc.process_macro(doc, router)

        assert summary.main_concepts == []
        assert summary.secondary_concepts == []
        for seg in summary.segments:
            assert seg.main_concepts == []
            assert seg.secondary_concepts == []


# ── process_detail ──────────────────────────────────────────────────


class TestProcessDetail:
    async def test_pass_2b_slices_content(self) -> None:
        proc = PresentationProcessor()
        doc = SourceDocument(
            source_type=SourceType.PRESENTATION,
            source_url="file:///x.pdf",
            chunks=[
                ContentChunk(
                    chunk_type=ChunkType.SLIDE_TEXT,
                    text="Intro text",
                    metadata={"slide_number": 1},
                ),
                ContentChunk(
                    chunk_type=ChunkType.SLIDE_TEXT,
                    text="Body text",
                    metadata={"slide_number": 2},
                ),
            ],
        )
        # assemble_text = "Intro text\n\nBody text" (len 21); cumsum [0,12,21]
        summary = DocumentSummaryDraft(
            title="T",
            description="D",
            segments=[
                DocumentSegmentDraft(
                    order=0,
                    start_pos=0,
                    end_pos=12,
                    description="seg0",
                    start_slide=1,
                    end_slide=1,
                ),
                DocumentSegmentDraft(
                    order=1,
                    start_pos=12,
                    end_pos=21,
                    description="seg1",
                    start_slide=2,
                    end_slide=2,
                ),
            ],
        )

        drafts = await proc.process_detail(doc, summary)

        assert drafts[0].content == "Intro text\n\n"
        assert drafts[1].content == "Body text"


# ── Integration — real PyMuPDF PDF path (no LLM, no LibreOffice) ─────


class TestPdfPathIntegration:
    async def test_lesson6_pdf_real_extraction(self) -> None:
        pdf = _FIXTURES / "lesson6_functions_1.pdf"
        proc = PresentationProcessor()
        doc = await proc.process_raw(_make_source(pdf))

        assert doc.metadata["slide_count"] == 14
        assert len(doc.chunks) == 14  # all 14 slides carry text
        assert all(c.chunk_type == ChunkType.SLIDE_TEXT for c in doc.chunks)
        # Critical invariant holds on real extraction.
        expected = "\n\n".join(c.text for c in doc.chunks if c.text)
        assert doc.assemble_text() == expected
        # Instance state carries the full sequence with rendered images.
        assert proc._slide_raw is not None
        assert len(proc._slide_raw) == 14
        assert all(sr.image_bytes for sr in proc._slide_raw)


class TestRenderedSlidesAccessor:
    """Phase 6 T3 read-only seam accessor (additive, KD-2.3-F intact)."""

    def test_empty_before_process_raw(self) -> None:
        """No renders yet → empty sequence (never None)."""
        proc = PresentationProcessor()
        assert proc.rendered_slides == ()
        assert len(proc.rendered_slides) == 0

    async def test_surfaces_full_sequence_after_extraction(self) -> None:
        """Accessor returns the exact per-slide sequence Pass 1 consumed."""
        pdf = _FIXTURES / "lesson6_functions_1.pdf"
        proc = PresentationProcessor()
        await proc.process_raw(_make_source(pdf))

        slides = proc.rendered_slides
        assert len(slides) == 14
        assert [s.slide_number for s in slides] == list(range(1, 15))
        assert all(s.image_bytes for s in slides)
        # Same object the processor holds internally — a view, not a copy.
        assert list(slides) == proc._slide_raw


# ── Visual-content bridge (road (a): image-only slides → SLIDE_VISUAL) ──


def _stage_names(router: AsyncMock) -> list[str]:
    """Ordered stage names the router was asked to execute."""
    return [call.args[0] for call in router.execute_for_stage.await_args_list]


class TestVisualContentBridge:
    """``_bridge_visual_content`` — image-only slides become segment content."""

    async def test_all_image_only_deck_gets_visual_content_for_every_slide(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, ""), (2, ""), (3, ""), (4, ""))),
        )
        doc = await proc.process_raw(_pdf_source(tmp_path))
        # process_raw emits no chunk for a fully image-only deck.
        assert doc.chunks == []
        router = _make_router(pass2a_payload=_pass2a_json([(1, 2), (3, 4)]))

        summary = await proc.process_macro(doc, router)

        # Every slide now owns a SLIDE_VISUAL chunk in slide order.
        assert [
            (c.chunk_type, c.text, c.metadata["slide_number"]) for c in doc.chunks
        ] == [
            (ChunkType.SLIDE_VISUAL, "desc 1", 1),
            (ChunkType.SLIDE_VISUAL, "desc 2", 2),
            (ChunkType.SLIDE_VISUAL, "desc 3", 3),
            (ChunkType.SLIDE_VISUAL, "desc 4", 4),
        ]
        # assemble_text = "desc 1\n\ndesc 2\n\ndesc 3\n\ndesc 4"; cumsum step 8.
        # The first (non-final) segment carries its trailing inter-slide "\n\n"
        # (pre-existing presentation Pass 2b behaviour).
        sliced = await proc.process_detail(doc, summary)
        assert [s.content for s in sliced] == [
            "desc 1\n\ndesc 2\n\n",
            "desc 3\n\ndesc 4",
        ]

    async def test_visual_chunks_merged_in_slide_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Image-only slide 1 precedes text slide 2 but is appended after it
        # (process_raw emitted only the text chunk); the merge must re-sort.
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, ""), (2, "Text"))),
        )
        doc = await proc.process_raw(_pdf_source(tmp_path))
        assert [c.metadata["slide_number"] for c in doc.chunks] == [2]
        router = _make_router(pass2a_payload=_pass2a_json([(1, 1), (2, 2)]))

        await proc.process_macro(doc, router)

        assert [(c.chunk_type, c.metadata["slide_number"]) for c in doc.chunks] == [
            (ChunkType.SLIDE_VISUAL, 1),
            (ChunkType.SLIDE_TEXT, 2),
        ]

    async def test_text_slide_gets_no_visual_chunk_and_skips_safety(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, "Text1"), (2, "Text2"))),
        )
        doc = await proc.process_raw(_pdf_source(tmp_path))
        router = _make_router(pass2a_payload=_pass2a_json([(1, 1), (2, 2)]))

        await proc.process_macro(doc, router)

        # No SLIDE_VISUAL chunk and — the pure-text deck being unchanged — no
        # safety call at all (the bridge returns early).
        assert all(c.chunk_type == ChunkType.SLIDE_TEXT for c in doc.chunks)
        assert "safety_check_authored" not in _stage_names(router)

    async def test_safety_surface_is_only_the_visual_descriptions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The already-vetted slide text is NOT re-checked; only the new VD is.
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, "KeepText"), (2, ""))),
        )
        doc = await proc.process_raw(_pdf_source(tmp_path))
        router = _make_router(pass2a_payload=_pass2a_json([(1, 1), (2, 2)]))

        await proc.process_macro(doc, router)

        safety_calls = [
            call
            for call in router.execute_for_stage.await_args_list
            if call.args[0] == "safety_check_authored"
        ]
        assert len(safety_calls) == 1
        assert safety_calls[0].kwargs["submission_text"] == "desc 2"

    async def test_empty_visual_description_fails_fast(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A slide with neither text nor a description is genuinely empty.
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, "Text"), (2, ""))),
        )
        doc = await proc.process_raw(_pdf_source(tmp_path))
        router = _make_router(
            pass1_by_slide={2: "   "},  # whitespace-only → empty after strip
            pass2a_payload=_pass2a_json([(1, 1), (2, 2)]),
        )
        with pytest.raises(ProcessingError, match="PRESENTATION_EMPTY_SEGMENT"):
            await proc.process_macro(doc, router)

    async def test_unsafe_visual_content_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, ""), (2, ""))),
        )
        doc = await proc.process_raw(_pdf_source(tmp_path))
        router = _make_router(
            pass2a_payload=_pass2a_json([(1, 1), (2, 2)]),
            safety_safe=False,
        )
        with pytest.raises(SecurityRejectedError) as exc_info:
            await proc.process_macro(doc, router)
        assert exc_info.value.category == ErrorCategory.STAGE2_REJECTED
        # Rejection happens before Pass 2a — no mapping call is spent.
        assert "presentation_pass_2a_mapping" not in _stage_names(router)

    async def test_assemble_text_and_cumsum_account_for_visual(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = PresentationProcessor()
        monkeypatch.setattr(
            proc,
            "_extract_pdf_pages",
            MagicMock(return_value=_slides((1, "Intro"), (2, ""), (3, "End"))),
        )
        doc = await proc.process_raw(_pdf_source(tmp_path))
        router = _make_router(pass2a_payload=_pass2a_json([(1, 2), (3, 3)]))

        await proc.process_macro(doc, router)

        assert doc.assemble_text() == "Intro\n\ndesc 2\n\nEnd"
        assert chars_per_slide_cumsum(doc.chunks) == [0, 7, 15, 18]
