"""Course supporter security layer (vision §3 KD14).

Provides shared validation pipeline for all uploads -- authored
materials and homework submissions -- with policies that vary per
context. Currently exposes types only; subsequent commits in
task 0.6 add the :class:`SecurityLayer` orchestrator, policy
constants, and Stage 1 / Stage 2 entry points.
"""

from course_supporter.security.exceptions import (
    ErrorCategory,
    SafetyValidationError,
    SecurityRejectedError,
)
from course_supporter.security.schemas import SafetyResult, ViolationCategory

__all__ = [
    "ErrorCategory",
    "SafetyResult",
    "SafetyValidationError",
    "SecurityRejectedError",
    "ViolationCategory",
]
