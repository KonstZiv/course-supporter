"""Exception types and Stage 1 rejection categories for the security layer.

Two distinct exception types serve different audiences:

* :class:`SecurityRejectedError` -- synchronous Stage 1 rejection
  (size / magic / archive / unicode / regex). The HTTP layer maps
  ``category`` to a 400 reason code per vision §KD14. Zero ESC --
  this never reaches the LLM.

* :class:`SafetyValidationError` -- terminal failure of Stage 2
  parsing. Raised when ``StageRouter.execute_for_stage("safety_check")``
  returns successfully but ``StageResult.content`` does not parse
  to a valid :class:`SafetyResult`. The underlying ESC row stays
  ``success=True`` because transport succeeded; semantic
  interpretation is the caller's policy.

Limitations carried forward to Phase 1+:

* The structural-retry hook in :class:`StageRouter` is deferred.
  When it lands, :class:`SafetyValidationError` may convert into
  a validator-driven retry trigger inside the ladder, replacing
  terminal failure. See vision §3 KD16 forward-looking notes and
  the 0.6 acceptance trade-off (Stage 2 chose terminal failure to
  avoid extending the sealed 0.5 contract).

This module intentionally lives outside ``llm.error_categories``
to keep Stage 1 (synchronous, pre-LLM) and the LLM ladder error
taxonomy in separate namespaces; the two ``ErrorCategory`` enums
serve different domains and are not interchangeable.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from course_supporter.security.schemas import SecurityContext


class ErrorCategory(StrEnum):
    """Categorical reason for Stage 1 rejection.

    Each value maps to a 400 reason code in the eventual HTTP
    layer wiring (Phase 1.2 / 4.x). :class:`SecurityRejectedError`
    carries one of these categories.

    * ``SIZE_LIMIT`` -- payload bytes exceed the active context
      policy's ``max_size``.
    * ``FORBIDDEN_TYPE`` -- magic-bytes detected MIME type is not
      on the active context policy's whitelist.
    * ``MAGIC_MISMATCH`` -- the file's claimed extension and its
      magic-bytes-derived MIME type disagree (e.g. ``.pdf`` named
      file with PNG magic header).
    * ``ARCHIVE_VIOLATION`` -- archive structural check failed:
      path traversal, depth cap, total unzipped size cap, or a
      non-whitelisted member.
    * ``SUSPICIOUS_UNICODE`` -- input contains zero-width / bidi /
      tag / disallowed control characters per vision §KD14.
    * ``PROMPT_INJECTION`` -- regex pre-screen matched a known
      injection pattern after NFKC normalization.
    * ``CHARSET_VIOLATION`` -- the bytes are not UTF-8 and could not
      be read back as text of the languages the upload is expected to
      be written in. Both contexts raise it: recovery either
      establishes an encoding and verifies the result, or refuses.
      What the detector calls the charset is not part of that
      decision -- it names no Cyrillic single-byte encoding at all.
    * ``ARCHIVE_BOMB`` -- archive decompression exceeds size, file
      count, or nesting depth limits per ``safety_archive_*``
      settings. Added Phase 2.1 C2 per KD-2.1-I migration of legacy
      ``safety.exceptions.ArchiveBombError`` raisers (load-bearing
      at ``safety/archive.py:177, 204, 219, 270, 310``).
    * ``SYMLINK_VIOLATION`` -- submission file or archive entry is
      a symbolic link. Added Phase 2.1 C2 per KD-2.1-I migration of
      legacy ``safety.exceptions.SymlinkViolationError`` raiser
      (load-bearing at ``safety/archive.py:130``).
    * ``STAGE2_REJECTED`` -- LLM-judged Stage 2 rejection (off-topic
      relative to course, harmful content, policy violation). Added
      Phase 2.1 C6 per KD-2.1-P (defense-in-depth Stage 2 for
      authored materials). Superset of :data:`PROMPT_INJECTION`:
      that value covers regex-detected attacks; this one covers any
      LLM verdict ``is_safe=False`` regardless of violation category.
    * ``SLIDE_COUNT_LIMIT`` -- a presentation upload exceeds the
      per-deck slide cap (100), checked HTTP-side via PyMuPDF (PDF)
      or python-pptx (PPTX) as fast pre-validation. Added Phase 2.3
      sub-area #6 per KD-2.3-M; reuses :class:`SecurityRejectedError`
      (no new exception class). ``.ppt`` is skipped HTTP-side
      (python-pptx reads only OOXML ``.pptx``) and enforced
      worker-side after LibreOffice convert in Phase 2.3 sub-area #7.
    """

    SIZE_LIMIT = "size_limit"
    FORBIDDEN_TYPE = "forbidden_type"
    MAGIC_MISMATCH = "magic_mismatch"
    ARCHIVE_VIOLATION = "archive_violation"
    SUSPICIOUS_UNICODE = "suspicious_unicode"
    PROMPT_INJECTION = "prompt_injection"
    CHARSET_VIOLATION = "charset_violation"
    ARCHIVE_BOMB = "archive_bomb"
    SYMLINK_VIOLATION = "symlink_violation"
    STAGE2_REJECTED = "stage2_rejected"
    SLIDE_COUNT_LIMIT = "slide_count_limit"
    # Async ingestion structural codes (task-code-materials F4 /
    # DD-2.3-AH): persisted to Job/AuthoredDocument.error_category by the
    # failure callback so the author UI can map them to stable messages.
    EMPTY_DOCUMENT = "empty_document"
    PRESENTATION_EMPTY_SEGMENT = "presentation_empty_segment"
    # DD-SP-D (student-path step V, phase 0): the two failure-classifier async
    # classes. EXTERNAL_SOURCE_UNAVAILABLE is declared explicitly at the
    # external-fetch points (yt-dlp download, web scrape); PIPELINE_FAILURE is
    # the execution seam's default so a failed ingestion Job never persists a
    # NULL category (the seam-default invariant).
    EXTERNAL_SOURCE_UNAVAILABLE = "external_source_unavailable"
    PIPELINE_FAILURE = "pipeline_failure"
    # An archive inside an archive is never opened (the bomb vector stays
    # unreachable). ``EntryVerdict`` has carried this outcome since the
    # classify mode existed, but only as an extractor-internal value; a
    # submission that names the entry to the student needs it in the same
    # code vocabulary as every other reason it shows.
    NESTED_ARCHIVE = "nested_archive"
    # Distinct from SIZE_LIMIT on purpose: that one is about bytes at the
    # door, this one about how much text the reading models can hold. A
    # submission can be comfortably under the upload cap and still carry more
    # text than the tightest context window accepts, and the two ask the
    # student for different things.
    OVER_BUDGET = "over_budget"


class SecurityRejectedError(Exception):
    """Stage 1 synchronous rejection, categorized for HTTP layer mapping.

    Carries optional :class:`SecurityContext` for audit logging,
    attached via :meth:`enrich` after library-side raise (KD-2.1-N
    Phase 2.1 C2 — mirrors legacy
    ``safety.exceptions.SecurityViolationError`` API).

    Attributes:
        category: One of :class:`ErrorCategory`. The HTTP layer
            translates this to a 400 reason code.
        detail: Human-readable explanation of the specific failure
            (e.g. ``"file size 31MB exceeds homework limit 20MB"``).
            Safe for inclusion in client-facing error responses.
        ctx: Optional audit context attached post-raise via
            :meth:`enrich`. ``None`` until consumer-side enriches.
    """

    def __init__(self, category: ErrorCategory, detail: str) -> None:
        self.category = category
        self.detail = detail
        self.ctx: SecurityContext | None = None
        super().__init__(f"{category.value}: {detail}")

    def enrich(self, ctx: SecurityContext) -> None:
        """Attach caller-side audit context (tenant, student, submission).

        Library-side code (e.g.
        :func:`course_supporter.security.archive.extract_submission_content`)
        raises without ``ctx``; the consumer (e.g.
        ``api/tasks.py:1705``) catches and calls :meth:`enrich` to
        attach the audit metadata before re-raising or logging.

        Mirrors legacy ``safety.exceptions.SecurityViolationError.enrich``
        signature per KD-2.1-N — drop-in API replacement.
        """
        self.ctx = ctx

    def as_log_dict(self) -> dict[str, Any]:
        """Return structured payload for log emission.

        Combines category + detail with attached :class:`SecurityContext`
        fields (when enriched). Format mirrors legacy
        ``safety.exceptions.SecurityViolationError.as_log_dict``.
        """
        result: dict[str, Any] = {
            "category": self.category.value,
            "detail": self.detail,
        }
        if self.ctx is not None:
            result.update(self.ctx.as_log_dict())
        return result


class SafetyValidationError(Exception):
    """Stage 2 terminal parse failure.

    Raised when the LLM safety classifier returned a successful
    response, but its content does not parse to a valid
    :class:`SafetyResult`. Per 0.6 acceptance, this is terminal --
    no ladder reuse, no structural retry. The underlying ESC row
    stays ``success=True`` because transport succeeded; this
    exception captures the semantic interpretation.

    The structural-retry hook in :class:`StageRouter` is a deferred
    Phase 1+ extension; once available, callers may convert this
    error into a retry trigger.

    Attributes:
        raw_content: The raw LLM response text that failed parsing.
            Kept for log forensics; do not feed back into a retry
            loop until the structural-retry hook is in place.
        parse_error: Short summary of the validation error, suitable
            for log records. Not safe for verbatim client return.
    """

    def __init__(self, raw_content: str, parse_error: str) -> None:
        self.raw_content = raw_content
        self.parse_error = parse_error
        super().__init__(f"Stage 2 safety check parse failed: {parse_error}")
