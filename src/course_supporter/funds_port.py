"""The funds port: where a submission asks about money (mentor-rebuild task 02).

Purpose:
    A submission's stages call paid models before anyone has paid for them.
    The port is the single place where the new path tells the money side what
    is happening to a submission — before its first paid call, after each
    stage, at the end — and hears back whether it may go on. The path speaks in
    its own terms: a ceiling estimate, a stage's actual price, how the
    submission ended. What those mean for an account — a hold, a debit, margin
    telemetry, a refund — is decided by the implementation, not by the port or
    its callers (KD19; ``03-BINDING.md``, amendment 2 and its note). Billing,
    when it comes, is one new implementation, not an edit of the path.

    Nothing calls the port yet: its live calls arrive with the new path's
    skeleton (task 03).

Input and output:
    Each operation takes a :class:`SubmissionContext` — tenant, student,
    submission and path key in one immutable value — and one thing more:

    * :meth:`FundsPort.check_and_reserve` — before the submission's first paid
      call, with the path's ceiling estimate. Returns a :class:`FundsAnswer`:
      allowed, or refused with a :class:`FundsRefusalReason`.
    * :meth:`FundsPort.account_stage_cost` — after a stage completes, with the
      stage's actual price from the call register. Returns nothing.
    * :meth:`FundsPort.release_remainder` — at the end, with the
      :class:`SubmissionOutcome`: completed or failed. Returns nothing.

    Work that is not a submission speaks through its own pair of operations and
    its own :class:`VersionWorkContext` — a generation has no student, no
    submission and no path (mentor-rebuild task 06, decision 1):

    * :meth:`FundsPort.check_and_reserve_for_version` — before the work's first
      paid call, with its ceiling estimate. Returns a :class:`FundsAnswer`.
    * :meth:`FundsPort.account_version_work_cost` — after the work finishes,
      with what it actually spent. It is FINAL: an implementation releases the
      unused part of any hold here, and there is no third operation, because
      the work has one result rather than two.

    Amounts are dollars as ``float``, the type of ``cost_usd`` in the register.
    A refusal is a returned value, never an exception, and the caller receives
    it exactly as the implementation built it. The port is told neither the
    stage, nor the provider, nor the model.

Replacing the implementation:
    The current implementation is :class:`AlwaysEnoughFundsPort`: it reserves
    nothing, allows every submission and records the estimate with its answer
    on the funds-port row of the call register
    (:func:`~course_supporter.service_logging.record_funds_decision`). A
    replacement that should keep those numbers calls the same writer.

    Anything with these three ``async`` methods satisfies :class:`FundsPort` —
    a structural ``Protocol``, no base class to inherit — and is passed where
    the current implementation is passed; no call site changes. A billing
    adapter, for example, checks the payer's balance and places a hold in
    ``check_and_reserve``, refusing with ``INSUFFICIENT_FUNDS`` when it falls
    short; debits, or only records, the stage price in ``account_stage_cost``;
    and returns the unused hold, or refunds a failed submission, in
    ``release_remainder``. It finds the payer — the author's account at the
    tenant — through ``tenant_id``, and ``submission_id`` is its key for acting
    once per submission.

    A new field of the context reaches every implementation without changing
    an operation's signature. A new refusal reason is a new
    :class:`FundsRefusalReason` member and a reaction to it in the caller.

    Worked cases, executed: :mod:`tests.unit.test_funds_port`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol

from course_supporter.call_outcome import FundsDecision
from course_supporter.homework.path_config import PathKey

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

FUNDS_PORT_ACTION: Final = "funds_port"
"""``ExternalServiceCall.action`` of the funds-port row."""


class FundsRefusalReason(StrEnum):
    """Why the port refused a submission — a closed vocabulary.

    The caller reacts to the code (the new path turns it into a submission
    state, task 03), so an implementation maps its own reasons onto these
    members instead of passing free text. The register keeps the code as text
    in ``funds_refusal_reason``; its CHECK (``ck_esc_funds_refusal_reason``)
    ties the reason to the decision, not to this vocabulary, so a new member
    needs no migration.

    * ``INSUFFICIENT_FUNDS`` — the payer cannot cover the path's ceiling
      estimate (the binding's funds decision; KD19: a short balance blocks the
      start).
    """

    INSUFFICIENT_FUNDS = "insufficient_funds"


class SubmissionOutcome(StrEnum):
    """How a submission ended, as the port hears it at the end.

    Completed or failed, nothing finer: a failed submission is what a billing
    adapter refunds (KD19), and which stage failed, and why, stays with the
    path.
    """

    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmissionContext:
    """Whose submission the port is being told about.

    Keyword-only: three of the fields are UUIDs, and a positional call could
    swap them without a type error.

    Attributes:
        tenant_id: The tenant; the payer is found through it.
        student_id: The student who submitted.
        submission_id: The submission (``HomeworkSubmission.id``).
        path_key: The path the submission takes.
    """

    tenant_id: uuid.UUID
    student_id: uuid.UUID
    submission_id: uuid.UUID
    path_key: PathKey


class VersionWorkKind(StrEnum):
    """Which work-once-per-version the port is being told about.

    Its own vocabulary rather than ``JobType``: the port speaks about money and
    must not learn how the work is scheduled (the same reason it is told
    neither the stage nor the provider of a submission's calls). A second
    member arrives with task 08's criteria decomposition.

    * ``KEY_EXPLANATION`` — the explanations of a test's answer key.
    """

    KEY_EXPLANATION = "key_explanation"


@dataclass(frozen=True, slots=True, kw_only=True)
class VersionWorkContext:
    """Whose work-once-per-version the port is being told about.

    The submission context does not fit this work and is not stretched to: a
    generation has no student, no submission and no path, and widening
    :class:`SubmissionContext` with three optional fields would leave every
    implementation guessing which shape it was handed (ratified 2026-09-19,
    decision 1 of ``06-reference/TASK.md``).

    What an implementation needs is here and nothing else. The payer is found
    through ``tenant_id``, exactly as for a submission; the pair
    (``authored_document_id``, ``source_content_hash``) is the key for acting
    once — the work belongs to ONE version of ONE task, and a second request
    for the same pair is the same work, not another one.

    Attributes:
        tenant_id: The tenant; the payer is found through it.
        authored_document_id: The task whose version the work is about.
        source_content_hash: That version — the task's content hash.
        work_kind: Which work-once-per-version this is.
    """

    tenant_id: uuid.UUID
    authored_document_id: uuid.UUID
    source_content_hash: str
    work_kind: VersionWorkKind


@dataclass(frozen=True, slots=True, kw_only=True)
class FundsAnswer:
    """The answer before the first paid call: allowed, or refused with a reason.

    Build it with :meth:`allowed` or :meth:`refused`. A refusal always carries a
    reason and an allowance never does — the rule the register holds as
    ``ck_esc_funds_refusal_reason`` — so an answer that breaks it cannot be
    constructed.
    """

    decision: FundsDecision
    refusal_reason: FundsRefusalReason | None = None

    def __post_init__(self) -> None:
        if (self.decision == FundsDecision.REFUSED) != (
            self.refusal_reason is not None
        ):
            raise ValueError(
                "A funds answer carries a refusal reason exactly when it is a "
                f"refusal: decision={self.decision!s}, "
                f"refusal_reason={self.refusal_reason!s}"
            )

    @classmethod
    def allowed(cls) -> FundsAnswer:
        """The submission may spend up to its estimate."""
        return cls(decision=FundsDecision.ALLOWED)

    @classmethod
    def refused(cls, reason: FundsRefusalReason) -> FundsAnswer:
        """The submission may not start spending, for ``reason``."""
        return cls(decision=FundsDecision.REFUSED, refusal_reason=reason)


class FundsPort(Protocol):
    """The replaceable port; the module docstring states the contract."""

    async def check_and_reserve(
        self, context: SubmissionContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        """Before the first paid call: may the submission spend up to the estimate?

        A refusal is returned, never raised.
        """
        ...

    async def account_stage_cost(
        self, context: SubmissionContext, stage_cost_usd: float
    ) -> None:
        """After a stage completes: the stage's actual price, in dollars."""
        ...

    async def release_remainder(
        self, context: SubmissionContext, outcome: SubmissionOutcome
    ) -> None:
        """At the end: how the submission ended, so the rest can be settled."""
        ...

    async def check_and_reserve_for_version(
        self, context: VersionWorkContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        """Before the first paid call of a work-once-per-version: may it spend?

        A refusal is returned, never raised — as for a submission. The caller
        (the generation job) leaves the version failed with the refusal as its
        reason, so the author reads why nothing was written.
        """
        ...

    async def account_version_work_cost(
        self, context: VersionWorkContext, actual_usd: float
    ) -> None:
        """After the work finishes: its actual price, and the end of it.

        This operation is FINAL for the work, and an implementation that placed
        a hold releases the unused part of it here. There is deliberately no
        third operation: a work-once-per-version has one result, not two, so
        there is no moment between "it cost this much" and "it is over" for a
        caller to get wrong (ratified 2026-09-19). A work that failed reports
        what it spent before failing — the same call, a smaller number.
        """
        ...


class AlwaysEnoughFundsPort:
    """The first implementation: no money exists yet, so every submission may spend.

    ``check_and_reserve`` holds nothing and always allows; what it keeps is the
    numbers — the path's estimate and the answer, on a funds-port row of the
    call register — so that estimates can be calibrated against actual costs
    before billing exists. ``account_stage_cost`` and ``release_remainder`` do
    nothing: no hold was placed to settle or release, and each stage's actual
    price is already in the register as the rows of its own calls.

    The write inherits the register's contract — skipped outside a job context
    and on a database error — and the answer is returned either way.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def check_and_reserve(
        self, context: SubmissionContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        """Allow, and record the estimate with the answer."""
        # Deferred: service_logging imports this module for FUNDS_PORT_ACTION.
        from course_supporter.service_logging import record_funds_decision

        answer = FundsAnswer.allowed()
        await record_funds_decision(
            self._session_factory,
            path_key=context.path_key,
            ceiling_estimate_usd=ceiling_estimate_usd,
            answer=answer,
        )
        return answer

    async def account_stage_cost(
        self, context: SubmissionContext, stage_cost_usd: float
    ) -> None:
        """Nothing was held, so nothing is settled."""

    async def release_remainder(
        self, context: SubmissionContext, outcome: SubmissionOutcome
    ) -> None:
        """Nothing was held, so nothing is released or refunded."""

    async def check_and_reserve_for_version(
        self, context: VersionWorkContext, ceiling_estimate_usd: float
    ) -> FundsAnswer:
        """Allow, and record the estimate with the answer.

        The same writer as a submission's answer, so both kinds of spending
        land in one series and can be compared without joining two shapes. The
        row carries no path: this work takes none, and ``path_key`` stays NULL
        rather than being filled with something that looks like one.
        """
        # Deferred: service_logging imports this module for FUNDS_PORT_ACTION.
        from course_supporter.service_logging import record_funds_decision

        answer = FundsAnswer.allowed()
        await record_funds_decision(
            self._session_factory,
            path_key=None,
            ceiling_estimate_usd=ceiling_estimate_usd,
            answer=answer,
        )
        return answer

    async def account_version_work_cost(
        self, context: VersionWorkContext, actual_usd: float
    ) -> None:
        """Nothing was held, so there is nothing to settle or release."""
