"""Shared helpers for the student-portal routes (Phase 6, KD17).

Small, behaviour-neutral projections reused across the portal route modules
(the submissions read-path and the materials-listing overlay). Extracted here
(Phase 6 T4a, Q3) so the curated verdict projection is neither imported as a
module-private ``_``-name across route modules nor duplicated — both modules
import the one public helper. The extraction is a pure refactor: the projection
logic is byte-identical to the prior ``portal_submissions._curated_verdict``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog
from pydantic import ValidationError

from course_supporter.api.schemas import (
    PortalNotOpened,
    PortalPresentation,
    PortalRejection,
    PortalVerdict,
)
from course_supporter.homework.test_scoring import Correctness, correctness
from course_supporter.models.review_schema import (
    REVIEW_SCHEMA_VERSION,
    review_schema_version,
)
from course_supporter.models.review_structure import ReviewStructureV1
from course_supporter.models.source import MaterialRole
from course_supporter.security.exceptions import ErrorCategory
from course_supporter.security.schemas import ViolationCategory
from course_supporter.storage.orm import HomeworkStatus

if TYPE_CHECKING:
    from course_supporter.storage.orm import HomeworkSubmission


logger = structlog.get_logger(__name__)


def curated_structure(
    review_result: dict[str, object] | None,
) -> ReviewStructureV1 | None:
    """The review structure, when the stored review is one (task 04).

    One projection for two surfaces — the portal detail and the ``reviewed``
    webhook both come through here, so neither can drift into reading the
    column its own way.

    Unlike :func:`curated_verdict`, this is not a slice of the trace: a
    version-1 review IS the structure, written for the student, and it goes out
    whole. Which of the two a row holds is answered by the version key, not by
    guessing from content — a review written before the rebuild reads as
    ``pre-rebuild`` and gets ``None`` here.

    A row that claims version 1 and does not validate is our own bad data, not
    the student's problem: it is logged and read as absent, so the rest of the
    page still renders. Raising would turn one bad row into a broken page;
    dropping it without a word would make it unfindable.
    """
    if not review_result:
        return None
    if review_schema_version(review_result) != REVIEW_SCHEMA_VERSION:
        return None
    try:
        return ReviewStructureV1.model_validate(review_result)
    except ValidationError as exc:
        logger.error(
            "review_structure_invalid",
            errors=exc.error_count(),
            detail=str(exc),
        )
        return None


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


def version_1_outcome(
    structure: ReviewStructureV1, score: int | None
) -> tuple[bool, Correctness]:
    """``passed`` and ``correctness`` of a version-1 review (task 07, decision 14).

    One function for both surfaces — the portal's verdict below and the
    ``reviewed`` webhook — so the two cannot read the same row two ways.

    * ``passed`` is the review's own verdict. A test's verdict is its pass mark
      applied to its score (``homework/test_scoring.py``), so a TEST with no
      verdict is a test with no pass mark: nobody set a bar to fail, and a
      caller gating on the boolean must not be held back — ``True``. Any other
      review without a verdict reads ``False``: the safe value a missing
      verdict read as before task 07, and not a pass nobody decided.
    * ``correctness`` comes from the score column, the same number the caller
      is sent as ``score``: 100 correct, 0 incorrect, anything between
      partially correct. A row without a score reads as 0, as the webhook's
      ``score`` does. For a version-1 review that is not a test, this is the
      rule only until task 08, which revisits it.
    """
    if structure.verdict is not None:
        passed = structure.verdict.passed
    else:
        passed = structure.test is not None
    return passed, correctness(score or 0)


def curated_verdict(
    review_result: dict[str, object] | None, *, score: int | None
) -> PortalVerdict | None:
    """Extract ONLY the caller-facing verdict from ``review_result``.

    For a pre-rebuild review, ``review_result`` is the internal trace and never
    goes out; only its ``verdict`` block does, and only once a review has
    written it (``None`` otherwise). That branch is today's Mentor, read as it
    always was.

    A version-1 review is a structure written for the student, not a trace; its
    outcome is :func:`version_1_outcome` of the structure and ``score`` — the
    submission's score column, which is why every caller passes it. A row that
    claims version 1 and does not validate reads as no verdict, as it does in
    :func:`curated_structure`.
    """
    if not review_result:
        return None
    if review_schema_version(review_result) == REVIEW_SCHEMA_VERSION:
        structure = curated_structure(review_result)
        if structure is None:
            return None
        passed, correct = version_1_outcome(structure, score)
        return PortalVerdict(passed=passed, correctness=correct)
    verdict = review_result.get("verdict")
    if not isinstance(verdict, dict):
        return None
    return PortalVerdict(
        passed=bool(verdict.get("passed", False)),
        correctness=str(verdict.get("correctness", "incorrect")),
    )


# The single normalizer category the read-path phrases (DD-6-Z, partial).
_NORMALIZER_CODED: Final[str] = ErrorCategory.OVER_BUDGET.value


_PRESENTATION_STATE: Final[dict[str, str]] = {
    # Nothing was opened: the doors refused it, or the run broke.
    HomeworkStatus.REJECTED.value: "not_opened",
    HomeworkStatus.FAILED.value: "not_opened",
    # It was read, and it did not look like an answer to this task.
    HomeworkStatus.MISMATCH.value: "not_an_attempt",
    # Nothing was spent, and nothing will be until the account is funded.
    HomeworkStatus.AWAITING_FUNDS.value: "awaiting_funds",
    # Being checked. WHICH gate it has passed is internal.
    HomeworkStatus.RECEIVED.value: "in_progress",
    HomeworkStatus.SAFETY_OK.value: "in_progress",
    HomeworkStatus.SANITY_OK.value: "in_progress",
    HomeworkStatus.REVIEWING.value: "in_progress",
    # A review exists, whether or not it reached the caller.
    HomeworkStatus.COMPLETED.value: "reviewed",
    HomeworkStatus.DELIVERED.value: "reviewed",
}
"""Every stored milestone, mapped to the one thing the student is told.

Total by construction and guarded below: a milestone without a decision about
what to say would otherwise be discovered as a ``KeyError`` on someone's
attempt, or — worse — quietly default to the wrong sentence.
"""

_OFF_TOPIC_STATE: Final = "not_an_attempt"
"""The state a read-but-off-topic submission gets, whatever its status says.

Named against the map above rather than written as a literal at the use site,
and checked below: a state nobody has a phrase for would reach a student as
"Стан невідомий".
"""

if _OFF_TOPIC_STATE not in set(_PRESENTATION_STATE.values()):  # pragma: no cover
    msg = f"Off-topic state {_OFF_TOPIC_STATE!r} is not one the portal knows"
    raise RuntimeError(msg)

_unmapped_statuses = {s.value for s in HomeworkStatus} - set(_PRESENTATION_STATE)
if _unmapped_statuses:  # pragma: no cover — test-locked
    msg = (
        f"HomeworkStatus values without a presentation state: "
        f"{sorted(_unmapped_statuses)}. Decide what the student is told before "
        f"the status can be written."
    )
    raise RuntimeError(msg)

OFF_TOPIC_REASON_CODE: Final = ViolationCategory.OFF_TOPIC.value
"""A submission the checker read and found to be about something else.

Given ONLY when off-topic is the whole of what Stage 2 found. The gate can
return several categories at once, and beside a real violation this phrase
would make light of it — so a submission that is both off-topic and, say, a
prompt injection keeps the phrase about safety.

It also decides the STATE (below), and that is the half that matters. Measured
against today's portal: with the state left at ``not_opened``, the student reads
"Роботу не перевірено. Спробуйте надіслати ще раз." — an invitation to send the
same foreign file again. With ``not_an_attempt`` they read "Надіслане не схоже
на рішення цього завдання. Перевірте, що подаєте правильний файл.", which is
the sentence this is for, and it is already in the portal's state family: no
interface change is needed for it, today or before this code is known there.
"""

FAILED_REASON_CODE: Final = "processing_failed"
"""The code for a run that broke — the surface pairs it with "send it again"."""

AWAITING_FUNDS_REASON_CODE: Final = "awaiting_funds"
"""The code for a revision held before its first paid call."""


def curated_presentation(submission: HomeworkSubmission) -> PortalPresentation:
    """The one answer to "what do we say about this attempt?" (task 03).

    Computed here, once, and carried by all three portal responses, so the tree
    and the lists cannot phrase the same attempt differently — and so the rule
    stops living in two repositories at once (``DD-SP-AS``).

    The reason code comes from whoever knows it:

    * ``rejected`` — from :func:`curated_rejection`, unchanged: the doors and
      the safety check already answer "why" in a vocabulary the surface has
      articles for;
    * ``mismatch`` — the same ``mismatch`` code it has carried since the doors
      pass, so the article keyed on it keeps working;
    * ``failed`` — a new code, because a run that broke told the student
      nothing before: the surface pairs it with "send it again";
    * ``awaiting_funds`` — a new code for a state that did not exist;
    * everything in flight, and everything reviewed — no code: the state is the
      whole answer.
    """
    state = _PRESENTATION_STATE[submission.status]
    if submission.status == HomeworkStatus.FAILED.value:
        return PortalPresentation(state=state, reason_code=FAILED_REASON_CODE)
    if submission.status == HomeworkStatus.AWAITING_FUNDS.value:
        return PortalPresentation(state=state, reason_code=AWAITING_FUNDS_REASON_CODE)
    rejection = curated_rejection(submission)
    if rejection is not None and rejection.code == OFF_TOPIC_REASON_CODE:
        # The one case where the code decides the state rather than the status
        # doing it (task 04). Stage 2 stores this as a rejection, and every
        # rejection maps to "not opened" — but it WAS opened and read, and that
        # is exactly how we know it is about something else. Derived from the
        # code and not decided a second time, so the two cannot disagree.
        state = _OFF_TOPIC_STATE
    return PortalPresentation(
        state=state, reason_code=rejection.code if rejection else None
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

    * Normalizer — ``safety_result`` with ``source='normalizer'`` carries a
      category only for the project branch's oversize refusal (step E); that
      one is phrased, and its ``details`` is the character pair rather than a
      filename.

    Anything else (the remaining normalizer rejections, ``DD-6-Z``) returns
    ``None`` and the interface falls back to its status phrase — the same
    behaviour as today, rather than inventing a code this function cannot
    honestly derive.

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
        code = (
            OFF_TOPIC_REASON_CODE
            if _off_topic_alone(safety.get("violations"))
            else ErrorCategory.STAGE2_REJECTED.value
        )
        return PortalRejection(code=code, details=submission.original_filename)
    if source == "normalizer" and safety.get("category") == _NORMALIZER_CODED:
        # Partial close of DD-6-Z (step E): the project branch's oversize
        # refusal is the one normalizer rejection that has both a category the
        # dictionary already phrases and specifics worth showing, so it gets a
        # code instead of the bare status phrase. Its ``details`` is the pair of
        # numbers the refusal is about, not a filename -- the sentence that
        # wraps them lives in the portal dictionary. The other normalizer
        # reasons still return None: their text is library vocabulary and DD-6-D
        # keeps it off the wire.
        details = safety.get("details")
        return PortalRejection(
            code=_NORMALIZER_CODED,
            details=details if isinstance(details, str) else None,
        )
    return None


def _off_topic_alone(violations: object) -> bool:
    """Whether off-topic is the WHOLE of what Stage 2 found.

    Read off the stored verdict, not out of a column of its own: the category
    is already in ``safety_result``, and a fourth place to keep in step is what
    ``DD-SP-Q`` records the cost of.

    Several categories at once means a real violation stands beside this one,
    and the sentence about a foreign file would make light of it. So this is
    deliberately an equality, not a membership test.
    """
    if not isinstance(violations, list):
        return False
    return [str(v) for v in violations] == [OFF_TOPIC_REASON_CODE]


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
