"""Course supporter security layer (vision §3 KD14).

Provides shared validation pipeline for all uploads -- authored
materials and homework submissions -- with policies that vary per
context. Public surface grows commit-by-commit through task 0.6;
later commits add the :class:`SecurityLayer` orchestrator and
context policy constants.
"""

from course_supporter.security.exceptions import (
    ErrorCategory,
    SafetyValidationError,
    SecurityRejectedError,
)
from course_supporter.security.normalization import (
    nfc_for_storage,
    nfkc_for_security,
    normalize_filename,
)
from course_supporter.security.schemas import SafetyResult, ViolationCategory
from course_supporter.security.unicode_check import check_text_unicode_safety

__all__ = [
    "ErrorCategory",
    "SafetyResult",
    "SafetyValidationError",
    "SecurityRejectedError",
    "ViolationCategory",
    "check_text_unicode_safety",
    "nfc_for_storage",
    "nfkc_for_security",
    "normalize_filename",
]
