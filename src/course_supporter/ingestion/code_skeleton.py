"""Deterministic rungs of the per-file code-skeleton extraction ladder.

task-code-materials R4: every included source file gets a *skeleton*
(declarations + verbatim docstrings + leading comments) that feeds the
cheap per-file ``code_segment_description`` LLM call. The full ladder:

1. **AST** (Python v1) — :func:`ast_skeleton`; full-fidelity structural
   walk. Fails (returns ``None``) on a ``SyntaxError``.
2. **regex** (JS/TS + Java/C-family + Go, ratified v1 families) —
   :func:`regex_skeleton`; declaration-line extraction with preceding
   doc-comment capture.
3. **cheap LLM** (``code_skeleton_extraction`` stage) — rung composition
   ratified from the task spike; lives in the CodeProcessor (needs the
   StageRouter), NOT here.
4. **deterministic minimum** — :func:`minimum_skeleton`; path + size +
   leading comment block. Never fails.

README / small config / doc-like files bypass the ladder entirely and
go in whole (:func:`verbatim_skeleton`, R4 "README/малі конфіги
повністю в межах cap").

Every rung's OUTPUT passes :func:`cap_skeleton` — the ratified
8192-byte cap is enforced CODE-SIDE for all four rungs (truncate at a
line boundary + visible marker); the spike showed model-side cap
instructions cannot be trusted (gemini-3.1-flash-lite emitted 9897B
with ``truncated=false``).

Pure functions, no I/O, no LLM — unit-testable in isolation.
"""

from __future__ import annotations

import ast
import re
from typing import Final

from course_supporter.ingestion.schemas import CodeSkeletonResult

# Ratified per-file skeleton output cap (bytes) — enforced on the
# rendered skeleton of EVERY rung (AST / regex / LLM / minimum).
SKELETON_MAX_BYTES: Final[int] = 8192

_TRUNCATION_MARKER: Final[str] = "\n[... skeleton truncated at 8192 bytes ...]"

# Files taken verbatim (whole content within the cap) instead of a
# structural skeleton — R4: README and small configs carry their meaning
# in prose/values, not declarations.
_VERBATIM_BASENAMES: Final[frozenset[str]] = frozenset(
    {"readme", "readme.md", "readme.txt", "readme.rst", "license", "changelog"}
)
_VERBATIM_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {"md", "markdown", "txt", "json", "yaml", "yml", "toml", "ini", "cfg"}
)

# Regex families (ratified v1): JS/TS + Java/C-family + Go.
_REGEX_FAMILY_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        # JS / TS
        "js",
        "mjs",
        "cjs",
        "jsx",
        "ts",
        "tsx",
        # Java / C-family
        "java",
        "kt",
        "kts",
        "cs",
        "c",
        "h",
        "cpp",
        "hpp",
        "cc",
        # Go
        "go",
    }
)

# Declaration-header pattern for the regex rung. Deliberately permissive
# (a skeleton line too many is cheap; a missed declaration starves the
# describe call): class/interface/struct/enum/func/fn keywords plus
# JS-style `name = (args) =>` and Go `func (recv) Name(`.
_MODIFIERS = (
    r"(?:public|private|protected|internal|abstract"
    r"|final|static|sealed|partial|async)"
)
_DECL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*(?:"
    r"(?:export[ \t]+)?(?:default[ \t]+)?" + _MODIFIERS + r"?[ \t]*"
    r"(?:"
    r"(?:class|interface|struct|enum|trait|record|type|namespace)[ \t]+\w+"
    r"|(?:export[ \t]+)?(?:async[ \t]+)?function[ \t*]*\w*"
    r"|func[ \t]+(?:\([^)]*\)[ \t]*)?\w+"
    r"|(?:const|let|var)[ \t]+\w+[ \t]*(?::[^=]+)?"
    r"=[ \t]*(?:async[ \t]*)?(?:\([^)]*\)|\w+)[ \t]*=>"
    r"|" + _MODIFIERS + r"[ \t][\w<>\[\],. \t]+\([^;{]*"
    r")"
    r")",
)

_LINE_COMMENT_PREFIXES: Final[tuple[str, ...]] = ("//", "#", "--")


def cap_skeleton(skeleton: str, *, max_bytes: int = SKELETON_MAX_BYTES) -> str:
    """Enforce the ratified per-rung byte cap on a rendered skeleton.

    Truncates at the last full LINE boundary that fits under
    ``max_bytes`` (declaration boundary in the rendered form — one
    declaration per line) and appends a visible marker. Idempotent for
    skeletons already under the cap.
    """
    raw = skeleton.encode("utf-8")
    if len(raw) <= max_bytes:
        return skeleton
    budget = max_bytes - len(_TRUNCATION_MARKER.encode("utf-8"))
    head = raw[:budget]
    # Cut at the last newline so no declaration line is half-emitted;
    # errors="ignore" guards a multi-byte char split at the boundary.
    text = head.decode("utf-8", errors="ignore")
    cut = text.rfind("\n")
    if cut > 0:
        text = text[:cut]
    return text + _TRUNCATION_MARKER


def is_verbatim_file(path: str) -> bool:
    """True for README/config/doc-like files taken whole (R4)."""
    basename = path.rsplit("/", 1)[-1].lower()
    if basename in _VERBATIM_BASENAMES:
        return True
    ext = basename.rsplit(".", 1)[-1] if "." in basename else ""
    return ext in _VERBATIM_EXTENSIONS


def verbatim_skeleton(path: str, text: str) -> str:
    """Whole-content 'skeleton' for README/small-config files, capped."""
    return cap_skeleton(f"FILE: {path} (verbatim)\n{text}")


def _leading_comment_block(text: str) -> str | None:
    """Verbatim leading line-comment block, or ``None``."""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines:
                break
            continue
        if stripped.startswith(_LINE_COMMENT_PREFIXES):
            lines.append(line)
        else:
            break
    return "\n".join(lines) if lines else None


def ast_skeleton(path: str, text: str) -> str | None:
    """Python skeleton via ``ast`` — rung 1. ``None`` on SyntaxError.

    Emits module docstring verbatim, imports, and every class /
    function / method signature with its verbatim docstring (R4:
    docstrings are never paraphrased).
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None

    lines: list[str] = [f"FILE: {path} (python skeleton)"]
    module_doc = ast.get_docstring(tree, clean=False)
    if module_doc:
        lines.append(f'"""{module_doc}"""')

    def _emit(node: ast.AST, indent: int) -> None:
        pad = "    " * indent
        if isinstance(node, ast.Import | ast.ImportFrom):
            lines.append(pad + ast.unparse(node))
            return
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            header = (
                f"{pad}class {node.name}({bases}):"
                if bases
                else f"{pad}class {node.name}:"
            )
            lines.append(header)
            doc = ast.get_docstring(node, clean=False)
            if doc:
                lines.append(f'{pad}    """{doc}"""')
            for child in node.body:
                _emit(child, indent + 1)
            return
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            args = ast.unparse(node.args)
            returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            lines.append(f"{pad}{prefix} {node.name}({args}){returns}: ...")
            doc = ast.get_docstring(node, clean=False)
            if doc:
                lines.append(f'{pad}    """{doc}"""')
            return
        if isinstance(node, ast.Assign) and indent == 0:
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if targets and all(t.isupper() for t in targets):
                lines.append(pad + ast.unparse(node))
            return
        if isinstance(node, ast.AnnAssign) and indent == 0:
            if isinstance(node.target, ast.Name) and node.target.id.isupper():
                lines.append(pad + ast.unparse(node))
            return

    for top in tree.body:
        _emit(top, 0)
    return cap_skeleton("\n".join(lines))


def regex_skeleton(path: str, ext: str, text: str) -> str | None:
    """Declaration-line skeleton for the ratified regex families — rung 2.

    ``None`` when the extension is outside the v1 families or no
    declaration matched (the ladder then falls to the LLM rung).
    Captures the leading file comment and, per declaration, the
    immediately preceding comment/doc-comment block verbatim.
    """
    if ext not in _REGEX_FAMILY_EXTENSIONS:
        return None

    src_lines = text.splitlines()
    out: list[str] = [f"FILE: {path} ({ext} skeleton)"]
    leading = _leading_comment_block(text)
    if leading:
        out.append(leading)

    found = False
    for i, line in enumerate(src_lines):
        if not _DECL_PATTERN.match(line):
            continue
        found = True
        # Preceding comment / doc block (contiguous // # /* * lines).
        doc: list[str] = []
        j = i - 1
        while j >= 0:
            s = src_lines[j].strip()
            if s.startswith(("//", "*", "/*", "///", "#")) or s.endswith("*/"):
                doc.append(src_lines[j])
                j -= 1
            else:
                break
        out.extend(reversed(doc))
        out.append(line.rstrip())

    if not found:
        return None
    return cap_skeleton("\n".join(out))


def minimum_skeleton(path: str, text: str, *, size_bytes: int) -> str:
    """Terminal deterministic rung — never fails (R4 minimum)."""
    out = [f"FILE: {path} ({size_bytes} bytes; no structural skeleton available)"]
    leading = _leading_comment_block(text)
    if leading:
        out.append(leading)
    return cap_skeleton("\n".join(out))


def render_llm_skeleton(path: str, result: CodeSkeletonResult) -> str:
    """Render the LLM stage's :class:`CodeSkeletonResult` to skeleton text.

    Same rendered form as the deterministic rungs so the describe stage
    sees one uniform input shape; capped like every other rung.
    """
    out: list[str] = [f"FILE: {path} ({result.language} skeleton, LLM-extracted)"]
    if result.leading_comments:
        out.append(result.leading_comments)
    if result.module_namespace:
        out.append("namespace: " + ".".join(result.module_namespace))
    if result.imports:
        out.append("imports: " + ", ".join(result.imports))
    for decl in result.declarations:
        parent = f" [in {decl.parent}]" if decl.parent else ""
        out.append(f"{decl.kind} {decl.name}{parent}: {decl.signature}")
        if decl.docstring:
            out.append(f"  doc: {decl.docstring}")
    if result.truncated:
        out.append("[model reported truncation at a declaration boundary]")
    return cap_skeleton("\n".join(out))


def namespace_core(path: str, ext: str, text: str) -> list[str]:
    """Deterministic concept core: module namespace identifiers.

    Ratified segment model: ``concepts`` core = the module namespace
    from the skeleton — computed deterministically here; the describe
    LLM verifies/supplements, never invents. Best-effort: Python via
    AST top-level names; regex families via declaration identifiers;
    otherwise the file stem.
    """
    names: list[str] = []
    if ext == "py":
        try:
            tree = ast.parse(text)
            names = [
                n.name
                for n in tree.body
                if isinstance(n, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            ]
        except (SyntaxError, ValueError):
            names = []
    elif ext in _REGEX_FAMILY_EXTENSIONS:
        for line in text.splitlines():
            m = _DECL_PATTERN.match(line)
            if m:
                ident = re.search(
                    r"(?:class|interface|struct|enum|trait|record|namespace"
                    r"|function|func|const|let|var|type)[ \t]+(\w+)",
                    line,
                )
                if ident:
                    names.append(ident.group(1))
    if not names:
        stem = path.rsplit("/", 1)[-1]
        stem = stem.rsplit(".", 1)[0] if "." in stem else stem
        names = [stem]
    # Dedup preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique
