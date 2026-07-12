"""Unit tests for the deterministic code-skeleton ladder rungs.

task-code-materials: pure functions (no I/O, no LLM) — AST rung,
regex rung, verbatim/minimum rungs, the code-side 8192-byte cap
(ratified: enforced for EVERY rung; model-side compliance is not
trusted per the task spike), and the deterministic namespace core.
"""

from __future__ import annotations

from course_supporter.ingestion.code_skeleton import (
    SKELETON_MAX_BYTES,
    ast_skeleton,
    cap_skeleton,
    is_verbatim_file,
    minimum_skeleton,
    namespace_core,
    regex_skeleton,
    render_llm_skeleton,
    verbatim_skeleton,
)
from course_supporter.ingestion.schemas import (
    CodeSkeletonDeclaration,
    CodeSkeletonResult,
)

_PY_SOURCE = '''"""Module docstring, kept verbatim."""

import csv

MAX_ROWS = 100


class Loader:
    """Loads rows."""

    def load(self, path: str) -> list[str]:
        """Reads the file."""
        return []


def helper(x: int) -> int:
    return x * 2
'''

_PY_BROKEN = '''"""Broken module."""

def bad(x: int) -> int
    return x
'''

_TS_SOURCE = """// Leading comment block.
// Second line.

import { thing } from './thing'

/** Doc for the service. */
export class InvoiceService {
  finalize(id: string): void {}
}

export function helper(x: number): number {
  return x * 2
}

const fmt = (n: number) => n.toFixed(2)
"""


class TestCapSkeleton:
    def test_under_cap_is_identity(self) -> None:
        assert cap_skeleton("short") == "short"

    def test_over_cap_truncates_at_line_boundary_with_marker(self) -> None:
        lines = "\n".join(f"decl_{i}: signature" for i in range(1000))
        capped = cap_skeleton(lines)
        raw = capped.encode()
        assert len(raw) <= SKELETON_MAX_BYTES
        assert capped.endswith("[... skeleton truncated at 8192 bytes ...]")
        # No half-emitted declaration line before the marker.
        body = capped.rsplit("\n", 1)[0]
        assert body.splitlines()[-1].startswith("decl_")
        assert body.splitlines()[-1].endswith("signature")

    def test_multibyte_boundary_survives(self) -> None:
        capped = cap_skeleton("я" * 10000)
        assert len(capped.encode()) <= SKELETON_MAX_BYTES


class TestAstSkeleton:
    def test_extracts_declarations_and_verbatim_docstrings(self) -> None:
        skeleton = ast_skeleton("src/loader.py", _PY_SOURCE)
        assert skeleton is not None
        assert "Module docstring, kept verbatim." in skeleton
        assert "class Loader" in skeleton
        assert "def load(self, path: str) -> list[str]" in skeleton
        assert "Reads the file." in skeleton
        assert "def helper(x: int) -> int" in skeleton
        assert "MAX_ROWS = 100" in skeleton
        assert "import csv" in skeleton

    def test_syntax_error_returns_none(self) -> None:
        assert ast_skeleton("bad.py", _PY_BROKEN) is None


class TestRegexSkeleton:
    def test_ts_declarations_with_doc_comments(self) -> None:
        skeleton = regex_skeleton("src/service.ts", "ts", _TS_SOURCE)
        assert skeleton is not None
        assert "export class InvoiceService" in skeleton
        assert "/** Doc for the service. */" in skeleton
        assert "export function helper(x: number): number {" in skeleton
        assert "const fmt = (n: number) => n.toFixed(2)" in skeleton
        assert "// Leading comment block." in skeleton

    def test_outside_families_returns_none(self) -> None:
        assert regex_skeleton("app.rb", "rb", "class Foo\nend\n") is None

    def test_no_declarations_returns_none(self) -> None:
        assert regex_skeleton("x.ts", "ts", "just a comment-free blob") is None


class TestVerbatimAndMinimum:
    def test_readme_detected_and_taken_whole(self) -> None:
        assert is_verbatim_file("docs/README.md")
        assert is_verbatim_file("config.yaml")
        assert not is_verbatim_file("src/app.py")
        out = verbatim_skeleton("README.md", "# Title\nBody")
        assert "(verbatim)" in out
        assert "# Title" in out

    def test_minimum_carries_path_size_and_leading_comment(self) -> None:
        out = minimum_skeleton(
            "src/thing.rb", "# top comment\nclass X\nend\n", size_bytes=42
        )
        assert "src/thing.rb (42 bytes" in out
        assert "# top comment" in out


class TestRenderLlmSkeleton:
    def test_renders_and_caps(self) -> None:
        result = CodeSkeletonResult(
            language="ruby",
            module_namespace=["Blog"],
            imports=["json"],
            declarations=[
                CodeSkeletonDeclaration(
                    kind="class",
                    name="PostService",
                    parent="Blog",
                    signature="class PostService",
                    docstring="# Creates posts.",
                )
            ],
            leading_comments="# frozen_string_literal: true",
            truncated=False,
        )
        out = render_llm_skeleton("lib/post_service.rb", result)
        assert "namespace: Blog" in out
        assert "class PostService [in Blog]: class PostService" in out
        assert "doc: # Creates posts." in out
        assert "# frozen_string_literal: true" in out


class TestNamespaceCore:
    def test_python_top_level_names(self) -> None:
        assert namespace_core("m.py", "py", _PY_SOURCE) == ["Loader", "helper"]

    def test_regex_family_identifiers(self) -> None:
        core = namespace_core("s.ts", "ts", _TS_SOURCE)
        assert "InvoiceService" in core
        assert "helper" in core

    def test_fallback_to_stem(self) -> None:
        assert namespace_core("lib/util.rb", "rb", "x = 1") == ["util"]
