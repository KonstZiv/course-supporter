"""Course supporter security layer (vision §3 KD14).

Provides shared validation pipeline for all uploads -- authored
materials and homework submissions -- with policies that vary per
context. Public surface grows commit-by-commit through task 0.6;
later commits add the :mod:`course_supporter.security` orchestrator
and context policy constants.
"""

from course_supporter.security.archive import (
    ExtractedFile,
    extract_archive_safely,
    extract_submission_content,
)
from course_supporter.security.exceptions import (
    ErrorCategory,
    SafetyValidationError,
    SecurityRejectedError,
)
from course_supporter.security.file_type import (
    detect_charset,
    detect_mime_type,
    extension_of,
    verify_extension_matches_content,
)
from course_supporter.security.normalization import (
    nfc_for_storage,
    nfkc_for_security,
    normalize_filename,
)
from course_supporter.security.policies import (
    AUTHORED_POLICY,
    HOMEWORK_POLICY,
    ContextPolicy,
    get_max_size_for_extension,
    policy_for,
)
from course_supporter.security.regex_patterns import (
    PROMPT_INJECTION_PATTERNS,
    CompiledPattern,
    match_text,
)
from course_supporter.security.schemas import (
    CourseContext,
    FileContent,
    SafetyResult,
    SecurityContext,
    SecurityWarning,
    Stage1RejectionResult,
    SubmissionContent,
    ViolationCategory,
)
from course_supporter.security.stage1 import Stage1Result, run_stage1
from course_supporter.security.stage2 import run_stage2_safety_check
from course_supporter.security.unicode_check import check_text_unicode_safety

__all__ = [
    "AUTHORED_POLICY",
    "HOMEWORK_POLICY",
    "PROMPT_INJECTION_PATTERNS",
    "CompiledPattern",
    "ContextPolicy",
    "CourseContext",
    "ErrorCategory",
    "ExtractedFile",
    "FileContent",
    "SafetyResult",
    "SafetyValidationError",
    "SecurityContext",
    "SecurityRejectedError",
    "SecurityWarning",
    "Stage1RejectionResult",
    "Stage1Result",
    "SubmissionContent",
    "ViolationCategory",
    "check_text_unicode_safety",
    "detect_charset",
    "detect_mime_type",
    "extension_of",
    "extract_archive_safely",
    "extract_submission_content",
    "get_max_size_for_extension",
    "match_text",
    "nfc_for_storage",
    "nfkc_for_security",
    "normalize_filename",
    "policy_for",
    "run_stage1",
    "run_stage2_safety_check",
    "verify_extension_matches_content",
]
