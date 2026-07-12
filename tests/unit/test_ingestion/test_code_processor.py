"""Unit tests for CodeProcessor (task-code-materials commit 4).

Deterministic segmentation (F1 offsets over assemble_text, F2
included-only reference), the two-step generation wiring against a
stage-aware fake StageRouter (no real LLM calls — the fake invokes
each ``response_validator`` on a canned payload, mirroring the
production semantics per the test_text_macro precedent), and the
ratified anchor invariant: exactly ONE anchor non-null per segment.
"""

from __future__ import annotations

import io
import itertools
import json
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
from course_supporter.ingestion.code import CodeProcessor
from course_supporter.llm.stage_router import StageResult
from course_supporter.models.source import ChunkType, SourceDocument, SourceType

_PY_BODY = 'print("hello")\n'
_RB_BODY = "# service\nclass PostService\nend\n"


def _mock_source(source_url: str, *, filename: str | None = None) -> MagicMock:
    source = MagicMock()
    source.source_type = SourceType.CODE
    source.source_url = source_url
    source.filename = filename
    return source


def _write_zip(path: Path, entries: dict[str, bytes]) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    path.write_bytes(buf.getvalue())


def _stage_result(payload: str) -> StageResult:
    return StageResult(
        content=payload,
        provider_used="gemini",
        model_used="gemini-3.1-flash-lite",
        attempt_count=1,
    )


def _stage_router(payloads: dict[str, str]) -> AsyncMock:
    """Stage-aware fake router: payload keyed by stage name.

    Invokes ``response_validator`` on the canned payload (fixup
    2.1.7.2 semantics) before returning a StageResult.
    """
    router = AsyncMock()

    async def _fake_execute(
        stage_name: str,
        *,
        response_validator: Any | None = None,
        **_render_context: Any,
    ) -> StageResult:
        payload = payloads[stage_name]
        if response_validator is not None:
            response_validator(payload)
        return _stage_result(payload)

    router.execute_for_stage = AsyncMock(side_effect=_fake_execute)
    return router


_DESCRIBE_PAYLOAD = json.dumps(
    {
        "description": "Demonstrates the service pattern.",
        "main_concepts": ["service object"],
        "secondary_concepts": ["result type"],
    }
)
_SUMMARY_PAYLOAD = json.dumps(
    {"title": "Lesson project", "description": "A small demo project."}
)
_SKELETON_PAYLOAD = json.dumps(
    {
        "language": "ruby",
        "module_namespace": ["PostService"],
        "imports": [],
        "declarations": [
            {
                "kind": "class",
                "name": "PostService",
                "parent": None,
                "signature": "class PostService",
                "docstring": "# service",
            }
        ],
        "leading_comments": "# service",
        "truncated": False,
    }
)


class TestProcessRaw:
    async def test_wrong_source_type_rejected(self, tmp_path: Path) -> None:
        source = _mock_source(str(tmp_path / "x.py"))
        source.source_type = SourceType.TEXT
        with pytest.raises(UnsupportedFormatError):
            await CodeProcessor().process_raw(source)

    async def test_single_file_becomes_one_chunk(self, tmp_path: Path) -> None:
        f = tmp_path / "script.py"
        f.write_text(_PY_BODY)
        doc = await CodeProcessor().process_raw(
            _mock_source(str(f), filename="script.py")
        )
        assert doc.source_type == SourceType.CODE
        assert len(doc.chunks) == 1
        chunk = doc.chunks[0]
        assert chunk.chunk_type == ChunkType.CODE_FILE
        assert chunk.text.startswith("===== FILE: script.py =====\n")
        assert chunk.text.endswith(_PY_BODY)
        assert chunk.metadata["file_path"] == "script.py"
        assert doc.metadata["excluded_entries"] == []

    async def test_zip_splits_included_and_excluded(self, tmp_path: Path) -> None:
        archive = tmp_path / "project.zip"
        _write_zip(
            archive,
            {
                "src/app.py": _PY_BODY.encode(),
                "lib/service.rb": _RB_BODY.encode(),
                "bin/tool.exe": b"MZ\x90\x00binary",
            },
        )
        doc = await CodeProcessor().process_raw(_mock_source(str(archive)))
        paths = [c.metadata["file_path"] for c in doc.chunks]
        assert paths == ["src/app.py", "lib/service.rb"]
        excluded = doc.metadata["excluded_entries"]
        assert [e["path"] for e in excluded] == ["bin/tool.exe"]
        assert excluded[0]["reason"] == "forbidden_type"

    async def test_zip_with_no_includable_files_raises(self, tmp_path: Path) -> None:
        archive = tmp_path / "junk.zip"
        _write_zip(archive, {"tool.exe": b"MZ\x90\x00binary"})
        with pytest.raises(ProcessingError, match="no includable source files"):
            await CodeProcessor().process_raw(_mock_source(str(archive)))


class TestOffsets:
    def test_full_cover_over_assemble_text(self) -> None:
        doc = SourceDocument(
            source_type=SourceType.CODE,
            source_url="file:///p.zip",
            chunks=[
                # Header + body shape mirrors process_raw output.
                _chunk("a.py", "===== FILE: a.py =====\nbody-a"),
                _chunk("b.py", "===== FILE: b.py =====\nbody-b longer"),
                _chunk("c.py", "===== FILE: c.py =====\nc"),
            ],
        )
        reference = doc.assemble_text()
        chunks = [c for c in doc.chunks if c.text]
        offsets = CodeProcessor._chunk_offsets(chunks, reference)
        assert offsets[0][0] == 0
        assert offsets[-1][1] == len(reference)
        for (_, prev_end), (nxt_start, _) in itertools.pairwise(offsets):
            assert prev_end == nxt_start
        # Each segment's slice starts with its own file header.
        for (start, _), chunk in zip(offsets, chunks, strict=True):
            assert reference[start:].startswith(chunk.text)


def _chunk(path: str, text: str) -> Any:
    from course_supporter.models.source import ContentChunk

    return ContentChunk(
        chunk_type=ChunkType.CODE_FILE,
        text=text,
        index=0,
        metadata={"file_path": path, "size_bytes": len(text)},
    )


class TestTwoStepGeneration:
    async def test_macro_builds_validated_draft(self, tmp_path: Path) -> None:
        """Python file (AST rung) — no skeleton LLM call is made."""
        f = tmp_path / "script.py"
        f.write_text(_PY_BODY)
        processor = CodeProcessor()
        doc = await processor.process_raw(_mock_source(str(f), filename="script.py"))
        router = _stage_router(
            {
                "code_segment_description": _DESCRIBE_PAYLOAD,
                "code_summary": _SUMMARY_PAYLOAD,
            }
        )
        draft = await processor.process_macro(doc, router)

        assert draft.title == "Lesson project"
        assert draft.description == "A small demo project."
        assert draft.main_concepts == ["service object"]
        assert draft.secondary_concepts == ["result type"]
        assert len(draft.segments) == 1
        seg = draft.segments[0]
        assert seg.title == "script.py"
        assert seg.start_pos == 0
        assert seg.end_pos == len(doc.assemble_text())
        assert draft.structure == {
            "entries": [
                {
                    "path": "script.py",
                    "size": len(_PY_BODY.encode()),
                    "cls": "included",
                    "reason": None,
                }
            ]
        }
        stages_called = [c.args[0] for c in router.execute_for_stage.await_args_list]
        assert stages_called.count("code_summary") == 1
        assert "code_skeleton_extraction" not in stages_called

    async def test_macro_llm_skeleton_rung_for_non_family_language(
        self, tmp_path: Path
    ) -> None:
        """Ruby file — outside AST/regex families, hits the LLM rung."""
        f = tmp_path / "service.rb"
        f.write_bytes(_RB_BODY.encode())
        processor = CodeProcessor()
        doc = await processor.process_raw(_mock_source(str(f), filename="service.rb"))
        router = _stage_router(
            {
                "code_skeleton_extraction": _SKELETON_PAYLOAD,
                "code_segment_description": _DESCRIBE_PAYLOAD,
                "code_summary": _SUMMARY_PAYLOAD,
            }
        )
        draft = await processor.process_macro(doc, router)
        stages_called = [c.args[0] for c in router.execute_for_stage.await_args_list]
        assert stages_called.count("code_skeleton_extraction") == 1
        # Skeleton (not raw code) feeds the describe call (ratified F1:
        # skeletons never enter the offset space, only LLM inputs).
        describe_call = next(
            c
            for c in router.execute_for_stage.await_args_list
            if c.args[0] == "code_segment_description"
        )
        assert "PostService" in describe_call.kwargs["skeleton"]
        assert len(draft.segments) == 1

    async def test_empty_document_raises(self) -> None:
        doc = SourceDocument(
            source_type=SourceType.CODE, source_url="file:///x", chunks=[]
        )
        with pytest.raises(ProcessingError, match="empty document"):
            await CodeProcessor().process_macro(doc, AsyncMock())


class TestProcessDetail:
    async def test_slice_and_single_anchor_invariant(self, tmp_path: Path) -> None:
        """Ratified: exactly ONE anchor non-null — file_path, six others None."""
        archive = tmp_path / "p.zip"
        _write_zip(
            archive,
            {"a.py": b"aaa\n", "b/c.py": b"ccc\n"},
        )
        processor = CodeProcessor()
        doc = await processor.process_raw(_mock_source(str(archive)))
        router = _stage_router(
            {
                "code_segment_description": _DESCRIBE_PAYLOAD,
                "code_summary": _SUMMARY_PAYLOAD,
            }
        )
        draft = await processor.process_macro(doc, router)
        filled = await processor.process_detail(doc, draft)

        reference = doc.assemble_text()
        assert [s.file_path for s in filled] == ["a.py", "b/c.py"]
        for seg in filled:
            assert seg.content == reference[seg.start_pos : seg.end_pos]
            # Exactly-one-anchor invariant (task-code-materials ratify).
            assert seg.file_path is not None
            assert seg.start_time_sec is None
            assert seg.end_time_sec is None
            assert seg.start_slide is None
            assert seg.end_slide is None
            assert seg.start_paragraph is None
            assert seg.end_paragraph is None
        # FULL-COVER reproduced on the filled drafts.
        assert filled[0].start_pos == 0
        assert filled[-1].end_pos == len(reference)
