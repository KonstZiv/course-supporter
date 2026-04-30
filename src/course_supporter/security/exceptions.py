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
    * ``CHARSET_VIOLATION`` -- the active context policy has
      ``enable_charset_strict=True`` and libmagic reports a charset
      that is neither ``utf-8`` nor ``us-ascii``. Modern submission
      contexts (homework) baseline UTF-8; legacy encodings reach
      this category. Authored content keeps the strict flag off and
      never raises this.
    """

    SIZE_LIMIT = "size_limit"
    FORBIDDEN_TYPE = "forbidden_type"
    MAGIC_MISMATCH = "magic_mismatch"
    ARCHIVE_VIOLATION = "archive_violation"
    SUSPICIOUS_UNICODE = "suspicious_unicode"
    PROMPT_INJECTION = "prompt_injection"
    CHARSET_VIOLATION = "charset_violation"


class SecurityRejectedError(Exception):
    """Stage 1 synchronous rejection, categorized for HTTP layer mapping.

    Attributes:
        category: One of :class:`ErrorCategory`. The HTTP layer
            translates this to a 400 reason code.
        detail: Human-readable explanation of the specific failure
            (e.g. ``"file size 31MB exceeds homework limit 20MB"``).
            Safe for inclusion in client-facing error responses.
    """

    def __init__(self, category: ErrorCategory, detail: str) -> None:
        self.category = category
        self.detail = detail
        super().__init__(f"{category.value}: {detail}")


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
