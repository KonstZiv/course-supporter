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
from course_supporter.security.exceptions import ErrorCategory, SecurityRejectedError

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
                "tools/helper.exe": b"MZ\x90\x00binary",
                # `bin` is a KD18 denylist segment — skipped at
                # extraction (№14), never unpacked; unified reason.
                "bin/tool.exe": b"MZ\x90\x00binary",
            },
        )
        doc = await CodeProcessor().process_raw(_mock_source(str(archive)))
        paths = [c.metadata["file_path"] for c in doc.chunks]
        assert paths == ["src/app.py", "lib/service.rb"]
        excluded = {e["path"]: e["reason"] for e in doc.metadata["excluded_entries"]}
        assert excluded == {
            "tools/helper.exe": "forbidden_type",
            "bin/tool.exe": "denylist_dir: bin/",
        }

    async def test_zip_with_no_includable_files_raises(self, tmp_path: Path) -> None:
        archive = tmp_path / "junk.zip"
        _write_zip(archive, {"tool.exe": b"MZ\x90\x00binary"})
        with pytest.raises(ProcessingError, match="no includable source files"):
            await CodeProcessor().process_raw(_mock_source(str(archive)))

    async def test_typicality_partition_description_only(self, tmp_path: Path) -> None:
        """F2/F3/F7: typical + oversize files go description-only."""
        from course_supporter.ingestion.code_typicality import (
            KEPT_SINGLE_MAX_BYTES,
        )

        archive = tmp_path / "project.zip"
        _write_zip(
            archive,
            {
                "src/app.py": _PY_BODY.encode(),
                "node_modules/lodash/index.js": b"module.exports = {}\n",
                "package-lock.json": b'{"lockfileVersion": 3}\n',
                "data/dump.sql": b"-- huge\n" * (KEPT_SINGLE_MAX_BYTES // 8 + 1),
            },
        )
        doc = await CodeProcessor().process_raw(_mock_source(str(archive)))
        # Only the custom file becomes a chunk/segment.
        assert [c.metadata["file_path"] for c in doc.chunks] == ["src/app.py"]
        entries = doc.metadata["description_only_entries"]
        by_path = {e["path"]: e for e in entries}
        # node_modules is skipped at EXTRACTION now (№14, never
        # unpacked) — it lands in excluded_entries with the unified
        # reason, not in the typicality partition.
        assert "node_modules/lodash/index.js" not in by_path
        excluded = {e["path"]: e["reason"] for e in doc.metadata["excluded_entries"]}
        assert excluded["node_modules/lodash/index.js"] == (
            "denylist_dir: node_modules/"
        )
        assert by_path["package-lock.json"]["disposition"] == "typical"
        assert by_path["data/dump.sql"]["disposition"] == "oversize"

    async def test_single_typical_file_not_rejected(self, tmp_path: Path) -> None:
        """R5: one policy for a single file; the material is NOT rejected."""
        f = tmp_path / "package-lock.json"
        f.write_bytes(b'{"lockfileVersion": 3}\n')
        doc = await CodeProcessor().process_raw(
            _mock_source(str(f), filename="package-lock.json")
        )
        assert doc.chunks == []
        assert len(doc.metadata["description_only_entries"]) == 1

    async def test_incident_form_archive_processes_clean(self, tmp_path: Path) -> None:
        """№14 regress: the prod-incident archive shape passes end-to-end.

        A macOS-packed lesson (``__MACOSX/`` AppleDouble companions over
        an 8-level ``.angular/cache``) used to die wholesale on the
        depth guard; now the junk is skipped before accounting and only
        honest sources become segments.
        """
        apple_double = bytes.fromhex("00051607") + b"\x00\x02" + b"\x00" * 60
        cache = ".angular/cache/21.1.4/app/vite/deps_ssr/chunks"
        archive = tmp_path / "lesson.zip"
        _write_zip(
            archive,
            {
                "app/src/main.js": b"export const answer = 42;\n",
                f"app/{cache}/chunk-ABC.js": b"// generated vite chunk\n",
                f"__MACOSX/app/{cache}/._chunk-ABC.js": apple_double,
                "__MACOSX/app/._main.js": apple_double,
                ".DS_Store": b"\x00\x01Bud1",
            },
        )
        doc = await CodeProcessor().process_raw(_mock_source(str(archive)))
        assert [c.metadata["file_path"] for c in doc.chunks] == ["app/src/main.js"]
        excluded = {e["path"]: e["reason"] for e in doc.metadata["excluded_entries"]}
        assert excluded == {
            f"app/{cache}/chunk-ABC.js": "denylist_dir: app/.angular/",
            f"__MACOSX/app/{cache}/._chunk-ABC.js": "denylist_dir: __MACOSX/",
            "__MACOSX/app/._main.js": "denylist_dir: __MACOSX/",
            ".DS_Store": "denylist_dir: .DS_Store",
        }
        assert doc.metadata["description_only_entries"] == []

    async def test_angular_typescript_lesson_all_ts_become_segments(
        self, tmp_path: Path
    ) -> None:
        """№17 regress: every .ts in a real Angular lesson is a segment.

        Before the textual-invariant fix, libmagic fingerprinted each
        .ts as application/javascript, the family table expected only
        ("text/",), and all eight files were silently excluded as
        magic_mismatch — the lesson kept only configs and templates.
        """
        component = (
            b"import { Component } from '@angular/core';\n\n"
            b"@Component({ selector: 'app-root', template: '<h1>Hi</h1>' })\n"
            b"export class AppComponent {}\n"
        )
        routes = (
            b"import { Routes } from '@angular/router';\n"
            b"import { Home } from './home';\n\n"
            b"export const routes: Routes = [{ path: '', component: Home }];\n"
        )
        ts_files = {
            "src/main.ts": component,
            "src/app/app.ts": component,
            "src/app/app.config.ts": (
                b"import { ApplicationConfig } from '@angular/core';\n"
                b"export const appConfig: ApplicationConfig = { providers: [] };\n"
            ),
            "src/app/app.routes.ts": routes,
            "src/app/app.spec.ts": (
                b"import { TestBed } from '@angular/core/testing';\n"
                b"describe('AppComponent', () => { it('works', () => {}); });\n"
            ),
            "src/app/first.ts": component,
            "src/app/second.ts": routes,
            "src/app/third.ts": component,
        }
        archive = tmp_path / "angular.zip"
        _write_zip(
            archive,
            {
                **ts_files,
                # The configs/templates that WERE included before the fix.
                "package.json": b'{\n  "name": "lesson"\n}\n',
                "README.md": b"# Angular lesson\n",
                "src/index.html": b"<!DOCTYPE html>\n<html><body></body></html>\n",
                "src/styles.css": b"body { margin: 0; }\n",
            },
        )
        doc = await CodeProcessor().process_raw(_mock_source(str(archive)))
        segmented = {c.metadata["file_path"] for c in doc.chunks}
        assert set(ts_files) <= segmented, set(ts_files) - segmented
        assert doc.metadata["excluded_entries"] == []

    async def test_same_ts_file_solo_and_in_archive_agree(self, tmp_path: Path) -> None:
        """№17 path-consistency: main.ts is included solo AND in-archive."""
        body = (
            b"import { Component } from '@angular/core';\n"
            b"export class AppComponent {}\n"
        )
        solo = tmp_path / "main.ts"
        solo.write_bytes(body)
        solo_doc = await CodeProcessor().process_raw(
            _mock_source(str(solo), filename="main.ts")
        )
        assert [c.metadata["file_path"] for c in solo_doc.chunks] == ["main.ts"]

        archive = tmp_path / "p.zip"
        _write_zip(archive, {"main.ts": body})
        arc_doc = await CodeProcessor().process_raw(_mock_source(str(archive)))
        assert [c.metadata["file_path"] for c in arc_doc.chunks] == ["main.ts"]

    async def test_solo_masked_binary_rejected_not_segmented(
        self, tmp_path: Path
    ) -> None:
        """№17 textual-guard (solo): a binary named .py must not segment.

        The solo path used to skip the magic check entirely, so a binary
        would be decoded and dropped verbatim into the reference text.
        """
        pe = b"\x4d\x5a\x90\x00\x03\x00\x00\x00" + b"\x00" * 400
        solo = tmp_path / "evil.py"
        solo.write_bytes(pe)
        with pytest.raises(SecurityRejectedError) as exc_info:
            await CodeProcessor().process_raw(
                _mock_source(str(solo), filename="evil.py")
            )
        assert exc_info.value.category is ErrorCategory.MAGIC_MISMATCH

    async def test_archive_masked_binary_excluded(self, tmp_path: Path) -> None:
        """№17 textual-guard (archive): a binary .py is excluded, not segmented."""
        pe = b"\x4d\x5a\x90\x00\x03\x00\x00\x00" + b"\x00" * 400
        archive = tmp_path / "p.zip"
        _write_zip(archive, {"evil.py": pe, "ok.py": _PY_BODY.encode()})
        doc = await CodeProcessor().process_raw(_mock_source(str(archive)))
        assert [c.metadata["file_path"] for c in doc.chunks] == ["ok.py"]
        excluded = {e["path"]: e["reason"] for e in doc.metadata["excluded_entries"]}
        assert excluded == {"evil.py": "magic_mismatch"}

    async def test_solo_empty_code_file_consistent_with_archive(
        self, tmp_path: Path
    ) -> None:
        """№17 empty-file consistency: empty solo file is included, not rejected.

        The in-archive classify path short-circuits empty content to
        INCLUDED before the magic check (test_archive_classify's
        ``test_empty_whitelisted_file_is_included``). The solo ``if raw``
        guard mirrors that — an empty ``__init__.py`` uploaded solo must
        NOT raise; both paths treat empty identically.
        """
        solo = tmp_path / "__init__.py"
        solo.write_bytes(b"")
        doc = await CodeProcessor().process_raw(
            _mock_source(str(solo), filename="__init__.py")
        )
        assert [c.metadata["file_path"] for c in doc.chunks] == ["__init__.py"]
        assert doc.metadata["excluded_entries"] == []


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

    async def test_all_description_only_yields_zero_segment_draft(
        self, tmp_path: Path
    ) -> None:
        """R5: all files description-only → summary-only material, no reject."""
        f = tmp_path / "package-lock.json"
        f.write_bytes(b'{"lockfileVersion": 3}\n')
        processor = CodeProcessor()
        doc = await processor.process_raw(
            _mock_source(str(f), filename="package-lock.json")
        )
        router = _stage_router({"code_summary": _SUMMARY_PAYLOAD})
        draft = await processor.process_macro(doc, router)

        assert draft.segments == []
        assert draft.title == "Lesson project"
        assert draft.structure is not None
        (entry,) = draft.structure["entries"]
        assert entry["cls"] == "description_only"
        assert entry["path"] == "package-lock.json"
        # No skeleton/describe calls — only the ONE summary call.
        stages = [c.args[0] for c in router.execute_for_stage.await_args_list]
        assert stages == ["code_summary"]

    async def test_structure_carries_all_three_classes(self, tmp_path: Path) -> None:
        archive = tmp_path / "p.zip"
        _write_zip(
            archive,
            {
                "src/app.py": _PY_BODY.encode(),
                # json extension passes the archive whitelist, so the
                # lockfile reaches (and is caught by) the typicality
                # file layer; a *.lock name would already be excluded
                # at the archive layer (forbidden_type) — same outcome,
                # different reason.
                "package-lock.json": b'{"lockfileVersion": 3}\n',
                "tool.exe": b"MZ\x90\x00bin",
            },
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
        assert draft.structure is not None
        cls_by_path = {e["path"]: e["cls"] for e in draft.structure["entries"]}
        assert cls_by_path == {
            "src/app.py": "included",
            "package-lock.json": "description_only",
            "tool.exe": "excluded",
        }


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
