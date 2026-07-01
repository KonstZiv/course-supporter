"""Shared helpers for the student-portal routes (Phase 6, KD17).

Small, behaviour-neutral projections reused across the portal route modules
(the submissions read-path and the materials-listing overlay). Extracted here
(Phase 6 T4a, Q3) so the curated verdict projection is neither imported as a
module-private ``_``-name across route modules nor duplicated — both modules
import the one public helper. The extraction is a pure refactor: the projection
logic is byte-identical to the prior ``portal_submissions._curated_verdict``.
"""

from __future__ import annotations

from course_supporter.api.schemas import PortalVerdict


def curated_verdict(review_result: dict[str, object] | None) -> PortalVerdict | None:
    """Extract ONLY the caller-facing verdict from ``review_result``.

    The full ``review_result`` JSONB (and ``safety_result`` / ``sanity_result``)
    is the internal trace and is never returned — only its ``verdict`` block,
    and only once a review has written it (``None`` otherwise).
    """
    if not review_result:
        return None
    verdict = review_result.get("verdict")
    if not isinstance(verdict, dict):
        return None
    return PortalVerdict(
        passed=bool(verdict.get("passed", False)),
        correctness=str(verdict.get("correctness", "incorrect")),
    )


def material_label(*, filename: str | None, source_type: str, order: int) -> str:
    """Display label for an authored document: the filename, else a derived
    ``{source_type} #{order}``.

    Byte-identical to the prior ``portal_courses._material_label`` (Phase 6
    T4a). Extracted here (6.HC) so the homework-cost drill-down composes the
    SAME task label the portal materials tree shows — one source, no
    duplicated format string (same house rule as :func:`curated_verdict`).
    Field-based rather than doc-based so the cost route, which holds only the
    aggregated raw columns, can call it without materialising an ORM object.
    """
    return filename or f"{source_type} #{order}"
