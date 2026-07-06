"""Normalizer failure hierarchy (KD18 P1).

A single base (:class:`NormalizerError`) so callers (P2 base-normalize
ARQ job, P3 submit) can catch one type and route it -- base -> failed
state with reason, submission -> surfaced error. Structural archive
violations (traversal / zip-bomb / symlink / malformed / mislabeled
kind) are NOT translated here: they surface as the security layer's
``SecurityRejectedError`` with its ``ErrorCategory``, preserving that
granularity. This module covers only normalizer-specific failures.
"""

from __future__ import annotations


class NormalizerError(Exception):
    """Base for deterministic normalizer failures (KD18 P1)."""


class NormalizerLimitError(NormalizerError):
    """The cleaned snapshot exceeds a level-2 content limit.

    Raised when the aggregate size of the kept (denylist-pruned) entries
    exceeds ``NormalizerLimits.kept_total_max_bytes``. A single oversized
    file does NOT trigger this -- per KD18 P1, ``kept_single_max_bytes``
    is a downstream (P4) review threshold, not a normalizer filter.
    """
