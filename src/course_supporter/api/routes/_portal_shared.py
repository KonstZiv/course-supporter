"""Shared helpers for the student-portal routes (Phase 6, KD17).

Small, behaviour-neutral projections reused across the portal route modules
(the submissions read-path and the materials-listing overlay). Extracted here
(Phase 6 T4a, Q3) so the curated verdict projection is neither imported as a
module-private ``_``-name across route modules nor duplicated — both modules
import the one public helper. The extraction is a pure refactor: the projection
logic is byte-identical to the prior ``portal_submissions._curated_verdict``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from course_supporter.api.schemas import (
    PortalNotOpened,
    PortalRejection,
    PortalVerdict,
)
from course_supporter.models.source import MaterialRole
from course_supporter.security.exceptions import ErrorCategory

if TYPE_CHECKING:
    from course_supporter.storage.orm import HomeworkSubmission


def role_visible_to_student(material_role: str) -> bool:
    """Whether a material of this ROLE may be seen by a student in the portal.

    Allowlist by design: visible ⇔ the role is exactly ``educational``. A
    methodological material (mentor-check instructions, reference solutions) is
    hidden from students on every portal surface that addresses a document.

    Written as an allowlist, NOT ``!= 'methodological'``, on purpose: when a
    third role is later added to the enum it stays hidden until a visibility
    rule is chosen for it explicitly, rather than leaking to students by
    default. The completeness test
    (``test_portal_role_visibility.py``) pins one assertion per enum member, so
    a new role cannot enter the vocabulary without a deliberate decision here.

    Scoped to the material's ROLE only — the name says "role", not "visibility"
    in general, so the future publication predicate (DD-6-F) sits beside this
    rather than dissolving into it. Compares against the canonical
    :class:`MaterialRole` member the author side writes (``update_material_role``
    stores ``MaterialRole.<X>.value`` into the ``str`` column), never a bare
    string literal.
    """
    return material_role == MaterialRole.EDUCATIONAL.value


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


def curated_rejection(
    submission: HomeworkSubmission,
) -> PortalRejection | None:
    """Derive the caller-facing reason code, or ``None`` if there is none.

    Three sources write a terminal outcome and each says "why" in its own
    shape, so the code is read from whichever one applies rather than from a
    fourth column that would have to be kept in step (``DD-SP-Q`` records the
    choice and the debt of the two families differing):

    * Stage 1 — ``safety_result`` with ``source='stage1'`` carries an
      ``ErrorCategory`` in ``category``: the extension, the magic, the
      encoding, the budget.
    * Sanity — a ``mismatch`` status means the work did not answer the task;
      the code is the verdict itself.
    * Stage 2 — ``safety_result`` with ``source='stage2'`` and ``is_safe``
      false is the LLM safety refusal.

    Anything else (a normalizer rejection, ``DD-6-Z``) returns ``None`` and the
    interface falls back to its status phrase — the same behaviour as today,
    rather than inventing a code this function cannot honestly derive.

    ``details`` carries only the filename. The internal ``error_message`` is
    never read here: it is a developer string, and putting it on the wire is
    exactly what ``DD-6-D`` forbids.
    """
    if submission.status == "mismatch":
        return PortalRejection(code="mismatch", details=submission.original_filename)

    safety = submission.safety_result
    if not isinstance(safety, dict):
        return None

    source = safety.get("source")
    if source == "stage1":
        category = safety.get("category")
        if isinstance(category, str):
            return PortalRejection(code=category, details=submission.original_filename)
        return None
    if source == "stage2" and safety.get("is_safe") is False:
        return PortalRejection(
            code=ErrorCategory.STAGE2_REJECTED.value,
            details=submission.original_filename,
        )
    return None


def curated_not_opened(
    submission: HomeworkSubmission,
) -> list[PortalNotOpened]:
    """List the files the checker skipped, on a passing attempt as on a refused one.

    Read from ``safety_result``, where Stage 1 recorded them alongside the
    verdict. A student whose archive was reviewed still needs to know that
    three of their files were not part of that review.
    """
    safety = submission.safety_result
    if not isinstance(safety, dict):
        return []
    raw = safety.get("not_opened")
    if not isinstance(raw, list):
        return []
    out: list[PortalNotOpened] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        arcname, reason, size = (
            item.get("arcname"),
            item.get("reason"),
            item.get("size"),
        )
        if (
            isinstance(arcname, str)
            and isinstance(reason, str)
            and isinstance(size, int)
        ):
            out.append(PortalNotOpened(path=arcname, reason=reason, size=size))
    return out


def curated_recovered_encoding(submission: HomeworkSubmission) -> str | None:
    """The encoding the submitted file was actually read as, if that is known.

    Same column and same reason as :func:`curated_not_opened`: Stage 1 knows
    how the file was read, and the student is the one who needs told. A name
    other than ``utf-8`` means the bytes were not UTF-8, an encoding was
    established and verified, and the review was written from that reading —
    which the student should hear once, so the next file is saved right.

    Three values, three different facts, and the interface has to tell them
    apart: ``"utf-8"`` (read directly — the ordinary case), another name
    (recovered), and ``None`` (the question does not apply — an archive
    recovers its members one by one, and a document arrives already decoded
    from the extractor).

    Not covered by DD-6-D: that ban is on ``error_message``, a developer
    string with library vocabulary in it. This is an encoding name.
    """
    safety = submission.safety_result
    if not isinstance(safety, dict):
        return None
    value = safety.get("recovered_encoding")
    return value if isinstance(value, str) and value else None


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
