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
* **homework**: pdf/md/txt/py/ipynb/zip/gz/tgz. 1 MB cap. Archives
  capped at 10 MB unzipped, 3 levels nesting. LLM safety check
  enabled (Stage 2). Charset-strict on (modern UTF-8 baseline).
"""

from __future__ import annotations

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
        enable_llm_safety_check: When ``True``, Stage 1 dispatches
            to Stage 2 LLM safety check after sync validation
            passes. Both authored and homework run Stage 2 post
            Phase 2.1 C6 (KD-2.1-P defense-in-depth — authored
            ingestion calls Stage 2 before Pass 2a to protect the
            downstream LLM from prompt injection in raw text).
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
    enable_llm_safety_check=True,
    enable_charset_strict=False,
)


HOMEWORK_POLICY: Final[ContextPolicy] = ContextPolicy(
    name="homework",
    allowed_extensions=frozenset(
        {
            "pdf",
            "md",
            "txt",
            "py",
            "ipynb",
            "zip",
            "gz",
            "tgz",
        }
    ),
    max_file_size_bytes=1 * 1024 * 1024,
    max_video_upload_bytes=None,
    max_video_download_bytes=None,
    max_presentation_size_bytes=None,
    max_archive_unzipped_bytes=10 * 1024 * 1024,
    max_archive_nesting_depth=3,
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
    return policy.max_file_size_bytes
