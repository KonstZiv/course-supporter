"""Per-context security policy constants (vision §KD14).

Two upload contexts -- ``authored`` (course materials uploaded by a
trusted author / instructor) and ``homework`` (submissions by
students) -- have distinct trust profiles and therefore distinct
allow-lists, size limits, archive rules, and Stage 2 LLM safety
gating.

## URL exclusion

URL ingestion has a separate security path (Phase 2.x web fetch).
``ContextPolicy`` applies to **file uploads only**; web URLs are
not modeled here.

## MVP design choice: Final constants, not YAML

Security policies evolve slowly; every change is reviewed. KD16's
YAML-driven philosophy targets fast-changing items (LLM models,
prices). Security policy lives as ``Final`` constants until a real
driver for tenant-specific overrides emerges.

## Vision §KD14 policy table (this module's source of truth)

* **authored**: video (mp4/mov/avi/mkv/webm) + audio
  (mp3/wav/m4a/ogg/flac) + documents (pdf/pptx/md/docx/txt/html)
  + source code (:data:`CODE_EXTENSIONS`, task-code-materials R2)
  + ``zip`` for code project archives (R1). 100 MB default cap,
  5 GB for video, 200 MB unzipped / depth 1 for archives. LLM
  safety check enabled (KD-2.1-P defense-in-depth).
  Charset-strict off (legacy encodings tolerated).
* **homework**: prose (:data:`_PROSE`) + source code
  (:data:`CODE_EXTENSIONS`) + containers (:data:`_ARCHIVES`) +
  documents (:data:`_DOCUMENTS`), as a derived union rather than a
  list; :data:`HOMEWORK_CONVEYORS` names the checking pipeline for
  each. 1 MB cap on a prose or code file submitted on its own; 10 MB
  on a primary format (an archive or a document), matching what the
  route door accepts. Archives capped at 10 MB unzipped, 3 levels
  nesting. LLM safety check enabled (Stage 2). Charset-strict on
  (modern UTF-8 baseline). See
  ``refactoring-vision/sprint/tasks/student-path/gates/FORMATS.md``
  for the ratified product statement behind each group.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

# Video extensions that get the dedicated video-upload cap
# (``max_video_upload_bytes``, resolved by
# ``get_max_size_for_extension``) when present on the active policy.
# Audio extensions stay on the default ``max_file_size_bytes``; they
# are bounded enough not to need a separate ceiling.
_VIDEO_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {"mp4", "mov", "avi", "mkv", "webm"}
)

# Presentation extensions get the dedicated ``max_presentation_size_bytes``
# cap (50 MB) when present on the active policy -- tighter than the 100 MB
# document default. ``None`` on the policy means no presentation override
# applies (e.g. homework, where ``pdf`` stays on the default cap).
_PRESENTATION_EXTENSIONS: Final[frozenset[str]] = frozenset({"pdf", "pptx", "ppt"})

# Source-code extensions accepted in the authored context for
# ``source_type=code`` materials (task-code-materials, ratified R2:
# deterministic typicality downstream, broad language list day 1).
# Public: the upload light-gate (api/routes/documents.py) and the
# typicality filter consume this set; policy whitelist membership is
# derived from it so the two stay in lock-step. Every entry must exist
# in ``_EXTENSION_TO_MIME_FAMILIES`` (TestPolicyConsistency gate).
# Inclusion-vs-description-only is NOT decided here — that is the
# typicality filter's job; this set only bounds the transport surface.
CODE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        # Python
        "py",
        "ipynb",
        # JavaScript / TypeScript
        "js",
        "mjs",
        "cjs",
        "jsx",
        "ts",
        "tsx",
        # Java / Kotlin
        "java",
        "kt",
        "kts",
        # C#
        "cs",
        # Go
        "go",
        # Rust
        "rs",
        # PHP
        "php",
        # Ruby
        "rb",
        # C / C++
        "c",
        "h",
        "cpp",
        "hpp",
        "cc",
        # Swift
        "swift",
        # Dart
        "dart",
        # Web / markup — html-as-code is the ratified defect-#9 case
        # (a lesson .html whose intent is a code example, taken verbatim);
        # the author declares the intent via source_type=code.
        "html",
        "htm",
        # Web / styling companions of code lessons
        "css",
        "scss",
        # Data / config companions of code lessons
        "json",
        "xml",
        "yaml",
        "yml",
        "toml",
        "sql",
        # Shell
        "sh",
    }
)


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """Per-context security policy applied at Stage 1 entry.

    Attributes:
        name: Discriminator -- ``"authored"`` or ``"homework"``.
        allowed_extensions: Lower-cased extension whitelist. Every
            entry must exist in
            :data:`course_supporter.security.file_type._EXTENSION_TO_MIME_FAMILIES`
            (regression-tested in
            :class:`tests.unit.security.test_policies.TestPolicyConsistency`).
        max_file_size_bytes: Default per-file cap for any extension
            that does not match the video override.
        max_video_upload_bytes: Cap on a video the author uploads
            through the create route -- bounded by the route reading
            the whole body into memory, so it is deliberately tighter
            than the download cap. Resolved for
            :data:`_VIDEO_EXTENSIONS` by
            :func:`get_max_size_for_extension`. ``None`` means video
            uploads are not allowed in this context.
        max_video_download_bytes: Cap on a video the worker fetches
            from a source URL -- bounded by disk and worker runtime,
            not by the route's memory, so it stays larger. Read
            directly by the yt-dlp download path, never through the
            resolver. ``None`` means video is not allowed in this
            context.
        max_presentation_size_bytes: Override cap applied to
            extensions in :data:`_PRESENTATION_EXTENSIONS` (pdf /
            pptx / ppt). ``None`` means no presentation override
            applies; the extension falls back to
            ``max_file_size_bytes``.
        max_archive_unzipped_bytes: Cumulative byte budget for
            archive extraction (KD-A); ``None`` means archives are
            not allowed in this context.
        max_archive_nesting_depth: Maximum archive-within-archive
            recursion depth; ``None`` means archives are not allowed
            in this context.
        max_primary_format_bytes: Override cap for extensions the
            conveyor table routes to ``archive`` or ``document`` --
            the PRIMARY formats, the ones a student submits as the
            work itself rather than as one file inside it. A Word
            export with two screenshots, or a project archive with a
            virtual environment left in, passes a megabyte without
            trying, and the student has no way to make it smaller.
            They are bounded at the door size instead; what reaches
            the model is bounded separately, by the text budget.
            ``None`` means no primary-format override applies, and
            everything falls back to ``max_file_size_bytes``.
        conveyors: Which pipeline verifies each accepted extension --
            ``text`` / ``archive`` / ``document``. ``None`` means the
            context runs no document conveyor: authored documents are
            opened later by the ingestion processors, and Stage 1 has
            no business extracting their text.
        enable_llm_safety_check: When ``True``, Stage 1 dispatches
            to Stage 2 LLM safety check after sync validation
            passes. Both authored and homework run Stage 2 post
            Phase 2.1 C6 (KD-2.1-P defense-in-depth — authored
            ingestion calls Stage 2 before Pass 2a to protect the
            downstream LLM from prompt injection in raw text).
        archive_soft_exclude: When ``True``, an archive member the
            checker cannot read is NAMED and set aside instead of
            failing the whole upload, and an archive with nothing
            readable left is rejected as empty rather than passed on.
            Formatting problems (unknown extension, magic mismatch,
            nested archive, non-UTF-8 text) are the student's mistake,
            not an attack; a real project archive almost always holds
            one. Deliberately does NOT soften the structural guards
            (traversal, bomb, symlink, depth) or the two hostility
            signals (unicode hard-reject, prompt-injection pre-screen)
            — naming and skipping those would be a ready-made bypass.
        enable_charset_strict: When ``True``, Stage 1 rejects text
            content whose libmagic-detected charset is neither
            UTF-8 nor US-ASCII. Modern submissions baseline UTF-8;
            authored content tolerates legacy encodings (Cyrillic
            in Windows-1251, etc.).
    """

    name: Literal["authored", "homework"]
    allowed_extensions: frozenset[str]
    max_file_size_bytes: int
    max_video_upload_bytes: int | None
    max_video_download_bytes: int | None
    max_presentation_size_bytes: int | None
    max_archive_unzipped_bytes: int | None
    max_archive_nesting_depth: int | None
    max_primary_format_bytes: int | None
    conveyors: Mapping[str, Conveyor] | None
    archive_soft_exclude: bool
    enable_llm_safety_check: bool
    enable_charset_strict: bool


AUTHORED_POLICY: Final[ContextPolicy] = ContextPolicy(
    name="authored",
    allowed_extensions=frozenset(
        {
            # video (5)
            "mp4",
            "mov",
            "avi",
            "mkv",
            "webm",
            # audio (5)
            "mp3",
            "wav",
            "m4a",
            "ogg",
            "flac",
            # documents (9)
            "pdf",
            "pptx",
            "ppt",
            "md",
            "docx",
            "txt",
            "html",
            # htm == html, markdown == md — identical MIME (text/) and the same
            # alias-aware TextProcessor handler (text.py {.md,.markdown} /
            # {.html,.htm}). Re-added in task 2.4.8 B1, deliberately reversing
            # the Phase 0.6 "one canonical extension per MIME" contraction
            # (Phase 1.2 dropped both as legacy aliases, see
            # phase-1-2/POST-MERGE-NOTES.md §1): the service ingests legacy
            # course material too, so fewer upload rejections is preferred over
            # the canonical-alias hygiene. _EXTENSION_TO_MIME_FAMILIES carries
            # both so the policy/MIME consistency gate stays green.
            "htm",
            "markdown",
            # code archive (task-code-materials R1: single file OR .zip).
            # zip only — tar.gz/tgz/gz stay excluded from authored; the
            # code upload light-gate accepts the zip kind exclusively.
            "zip",
        }
    )
    # Source-code extensions (R2 broad list) ride the same whitelist so
    # Stage 1 and the code light-gate share one source of truth.
    | CODE_EXTENSIONS,
    max_file_size_bytes=100 * 1024 * 1024,
    # Two video caps, deliberately different (ARC.md §9): the upload cap is
    # bounded by the create route reading the whole body into memory (8 GB
    # machine); the download cap is bounded by disk and worker runtime.
    # Merging them into one number would let the upload limit silently govern
    # the download surface, for which a gibibyte may not be enough.
    max_video_upload_bytes=1024 * 1024 * 1024,
    max_video_download_bytes=5 * 1024 * 1024 * 1024,
    max_presentation_size_bytes=50 * 1024 * 1024,
    # Archive knobs mirror _PROJECT_NORMALIZE_LIMITS (normalizer/models.py):
    # same author-curated-project envelope as KD18 base archives. Depth 1 =
    # top-level archive only; a nested archive rejects (strict Stage 1) or
    # is excluded (classify mode), never recursed — bomb vector unreachable.
    max_archive_unzipped_bytes=200 * 1024 * 1024,
    max_archive_nesting_depth=1,
    max_primary_format_bytes=None,
    conveyors=None,
    # Authored uploads stay all-or-nothing: the author is present, iterating,
    # and a half-read course archive is worse for them than a clear refusal.
    archive_soft_exclude=False,
    enable_llm_safety_check=True,
    enable_charset_strict=False,
)


# ── Homework surface: four groups, one reason each ────────────────
#
# The product answer to "what may a student submit" is a UNION OF GROUPS,
# not a list. A hand-written list is the class that produced four defects
# out of four (DD-19-B): it drifts against its twins silently, and its
# incompleteness degrades the result rather than raising. Each group below
# carries the reason it exists; membership follows from the reason.
#
# gates/FORMATS.md is the ratified product statement these mirror.

# Prose a student writes directly. ``markdown`` / ``rst`` deliberately
# absent: no consumer, and ``rst`` would need a new family-map entry.
_PROSE: Final[frozenset[str]] = frozenset({"md", "txt"})

# Containers. ``gz`` is present because ``extension_of`` reduces
# ``work.tar.gz`` to the token ``gz`` (lowest suffix only, by contract) --
# admitting ``tar.gz`` therefore admits the token. A bare single-file gzip
# is refused later, by content, not by extension.
_ARCHIVES: Final[frozenset[str]] = frozenset({"zip", "gz", "tgz"})

# Documents a student exports from the editor they have at home (Word,
# Google Docs, Pages, LibreOffice). ``odt`` is absent by measurement, not
# by preference: neither PyMuPDF nor python-docx opens it, so it would
# need a third extraction branch.
_DOCUMENTS: Final[frozenset[str]] = frozenset({"docx", "pdf"})

Conveyor = Literal["text", "archive", "document"]

# Which pipeline verifies each accepted format. Product policy ("may a
# student send this?") and security mechanism ("how is it checked?") are
# separate questions, so they are separate structures -- but every accepted
# format must answer BOTH, which is what TestConveyorTable locks. Derived
# from the same groups, so a format cannot enter the policy without also
# acquiring a conveyor here.
HOMEWORK_CONVEYORS: Final[dict[str, Conveyor]] = {
    **{ext: "text" for ext in _PROSE | CODE_EXTENSIONS},
    **{ext: "archive" for ext in _ARCHIVES},
    **{ext: "document" for ext in _DOCUMENTS},
}


HOMEWORK_POLICY: Final[ContextPolicy] = ContextPolicy(
    name="homework",
    # Derived union, mirroring AUTHORED_POLICY's ``| CODE_EXTENSIONS`` form:
    # widening CODE_EXTENSIONS widens the student surface automatically, and
    # the two contours cannot drift apart by anyone forgetting a list.
    allowed_extensions=_PROSE | CODE_EXTENSIONS | _ARCHIVES | _DOCUMENTS,
    max_file_size_bytes=1 * 1024 * 1024,
    max_video_upload_bytes=None,
    max_video_download_bytes=None,
    max_presentation_size_bytes=None,
    max_archive_unzipped_bytes=10 * 1024 * 1024,
    max_archive_nesting_depth=3,
    # The door accepts 10 MiB (``MAX_HOMEWORK_SIZE``); archives and documents
    # are bounded by that same number, not by the 1 MiB per-text-file rule.
    # The unzipped budget below is a separate, unchanged KD14 guard: a 10 MiB
    # archive may still not expand past 10 MiB.
    max_primary_format_bytes=10 * 1024 * 1024,
    conveyors=HOMEWORK_CONVEYORS,
    archive_soft_exclude=True,
    enable_llm_safety_check=True,
    enable_charset_strict=True,
)


def policy_for(context: Literal["authored", "homework"]) -> ContextPolicy:
    """Resolve the active :class:`ContextPolicy` for an upload context.

    The ``Literal`` annotation catches typos at type-check time;
    the runtime ``ValueError`` adds defense-in-depth for callers
    bypassing the type system (e.g. policy resolution from
    user-controlled string input).

    Args:
        context: ``"authored"`` or ``"homework"``.

    Raises:
        ValueError: on any other value.
    """
    match context:
        case "authored":
            return AUTHORED_POLICY
        case "homework":
            return HOMEWORK_POLICY
        case _:
            raise ValueError(f"unknown context {context!r}")


def get_max_size_for_extension(extension: str, policy: ContextPolicy) -> int:
    """Return the per-file size cap applicable to ``extension`` under ``policy``.

    Resolution order (overrides are keyed on disjoint extension sets):

    * ``policy.max_video_upload_bytes`` when the extension is in
      :data:`_VIDEO_EXTENSIONS` and the policy provides a video-upload
      override. (The download cap ``max_video_download_bytes`` is read
      directly by the yt-dlp path, not through this resolver.)
    * ``policy.max_presentation_size_bytes`` when the extension is in
      :data:`_PRESENTATION_EXTENSIONS` and the policy provides a
      presentation override.
    * ``policy.max_primary_format_bytes`` when the policy's conveyor
      table routes the extension to ``archive`` or ``document``. Keyed
      off the table rather than a fourth hand-written extension set --
      adding a container or a document format to the policy must not
      silently leave it on the per-text-file cap.
    * ``policy.max_file_size_bytes`` otherwise.

    The extension argument is lower-cased internally for whitelist
    comparison; callers may pass either case.
    """
    ext = extension.lower()
    if ext in _VIDEO_EXTENSIONS and policy.max_video_upload_bytes is not None:
        return policy.max_video_upload_bytes
    if (
        ext in _PRESENTATION_EXTENSIONS
        and policy.max_presentation_size_bytes is not None
    ):
        return policy.max_presentation_size_bytes
    if (
        policy.conveyors is not None
        and policy.conveyors.get(ext) in {"archive", "document"}
        and policy.max_primary_format_bytes is not None
    ):
        return policy.max_primary_format_bytes
    return policy.max_file_size_bytes
