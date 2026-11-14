"""The closed vocabularies of billing (BIL-A, BIL-B, BIL-PAY).

The values are the ones the ``billing_*`` tables' check constraints allow;
a new value is a migration. The kind of action is the one of the shadow
estimates (:class:`course_supporter.cost_estimate.core.ActionKind`), so an
estimate and its payer speak of the same action.
"""

from __future__ import annotations

from enum import StrEnum

from course_supporter.cost_estimate.core import ActionKind

__all__ = [
    "OUTFLOW_KINDS",
    "AccountKind",
    "AccountStatus",
    "ActionKind",
    "AuditAction",
    "BatchSource",
    "Benefit",
    "ConsentChannel",
    "DifferenceState",
    "EventKind",
    "EventState",
    "MovementKind",
    "PayerSource",
    "RefundKind",
    "RefundState",
    "ReservationState",
    "RuleScope",
]


class AccountKind(StrEnum):
    """Whose account it is."""

    STUDENT = "student"
    """A student of one tenant; pays for the reviews of their own work."""

    AUTHOR = "author"
    """The tenant; pays for processing and task preparation."""


class AccountStatus(StrEnum):
    """An account is active until its owner is deleted."""

    ACTIVE = "active"
    CLOSED = "closed"


class RuleScope(StrEnum):
    """What a payment rule is about."""

    COURSE = "course"
    """Every action of its kind on one course (a root ``CourseNode``)."""

    STUDENT = "student"
    """Every action of its kind for one student."""


class Benefit(StrEnum):
    """What the administrator granted an author account."""

    NONE = "none"
    MONTHLY_LIMIT = "monthly_limit"
    """Free of charge up to ``monthly_limit_units`` a month."""

    UNLIMITED = "unlimited"


class ConsentChannel(StrEnum):
    """Where the holder of an account agreed to the terms."""

    PORTAL = "portal"
    """A student, in the portal (their portal session)."""

    API = "api"
    """An author, through the API with the tenant's key (the author's app
    uses it); the server cannot tell the app from another client of the key."""


class PayerSource(StrEnum):
    """Why an account was chosen to pay.

    :func:`.payer.resolve_payer` returns the first three; the journal (BIL-B)
    adds the two that mean the payer's units were not taken.
    """

    STUDENT_RULE = "student_rule"
    COURSE_RULE = "course_rule"
    DEFAULT = "default"
    """Nobody else pays: the student for a review, the author otherwise."""

    BENEFIT = "benefit"
    """The author's benefit covered it; set by :func:`.ledger.settle` only."""

    PLATFORM = "platform"
    """Re-processing at the platform's initiative: never the author's units."""


class AuditAction(StrEnum):
    """What an audit entry records."""

    AUTHOR_SETTINGS_UPDATE = "author_settings.update"
    PAYMENT_RULE_CREATE = "payment_rule.create"
    PAYMENT_RULE_END = "payment_rule.end"
    ACCOUNT_ANONYMIZE = "account.anonymize"
    BATCH_ADMIN_CREDIT = "batch.admin_credit"
    BATCH_INVOICE_CREDIT = "batch.invoice_credit"
    """A contract invoice paid (B2B), credited by the administrator."""

    DISPUTE_LOST = "dispute.lost"
    """A dispute (chargeback) lost: its held units leave the batch."""

    DISPUTE_WON = "dispute.won"
    """A dispute (chargeback) won: its held units are free again."""


# ── The journal of units (BIL-B) ──


class BatchSource(StrEnum):
    """Where a batch of units came from."""

    PURCHASE = "purchase"
    """Paid through a payment provider, or by a contract invoice (its
    ``provider`` says which); the unused free units of a provider's purchase
    are refundable through it."""

    ADMIN_CREDIT = "admin_credit"
    """Granted by the administrator; never refunded."""


class MovementKind(StrEnum):
    """What one journal movement did; its ``units`` are always positive."""

    CREDIT = "credit"
    """A batch arrived (its units)."""

    RESERVE = "reserve"
    """Free units of a batch held for an action."""

    RESERVE_RELEASE = "reserve_release"
    """Held units of a batch free again (the rest after a settle, or a failure)."""

    DEBIT = "debit"
    """Units charged for an action — out of the batch."""

    DIFFERENCE_ARISEN = "difference_arisen"
    """An action cost more than was available; no batch."""

    DIFFERENCE_REPAID = "difference_repaid"
    """Free units of a batch repaid a difference — out of the batch."""

    BATCH_EXPIRED = "batch_expired"
    """Free units of an expired batch burned — out of the batch."""

    DIFFERENCE_ANNULLED = "difference_annulled"
    """What was left of a difference annulled after its term; no batch."""

    BENEFIT_COVERED = "benefit_covered"
    """Units of an action the author's benefit covered; no batch."""

    PLATFORM_COVERED = "platform_covered"
    """Units of a platform-initiated re-processing; no batch."""

    REFUND_HOLD = "refund_hold"
    """Free units of a purchase batch blocked while the provider reviews a
    refund."""

    REFUND_COMPLETED = "refund_completed"
    """Refund approved: the held units leave the batch."""

    REFUND_REJECTED = "refund_rejected"
    """Refund rejected: the held units are free again."""


OUTFLOW_KINDS: frozenset[MovementKind] = frozenset(
    {
        MovementKind.DEBIT,
        MovementKind.DIFFERENCE_REPAID,
        MovementKind.BATCH_EXPIRED,
        MovementKind.REFUND_COMPLETED,
    }
)
"""The kinds that take units OUT of a batch: its remaining units are its
``units`` minus these."""


class ReservationState(StrEnum):
    """Where the reservation of one action stands."""

    ACTIVE = "active"
    SETTLED = "settled"
    RELEASED = "released"


class DifferenceState(StrEnum):
    """Where a difference stands."""

    OPEN = "open"
    REPAID = "repaid"
    ANNULLED = "annulled"


class RefundKind(StrEnum):
    """Why units of a purchase batch are held or gone back (``billing_refunds``)."""

    REFUND = "refund"
    """A refund: asked of the provider (held while it reviews), or made at the
    provider (in its dashboard) and followed by the journal."""

    DISPUTE = "dispute"
    """A dispute (chargeback): the free units are held until it ends — lost,
    they leave the batch; won, they are free again (operator 2026-11-12)."""


class RefundState(StrEnum):
    """Where a refund through the batch's provider — or a dispute — stands.

    For a dispute: ``held`` while it lasts, ``approved`` when it was lost
    (the units are gone), ``rejected`` when it was won (the units are back).
    """

    HELD = "held"
    APPROVED = "approved"
    REJECTED = "rejected"


# ── Webhook events of payment providers (BIL-PAY) ──


class EventKind(StrEnum):
    """A provider's event, as billing reads it (``billing.payments.port``)."""

    PURCHASE_COMPLETED = "purchase_completed"
    """A package was paid: its units are credited."""

    REFUND_APPROVED = "refund_approved"
    """The provider refunded: the held units leave the batch."""

    REFUND_REJECTED = "refund_rejected"
    """The provider refused the refund: the held units are free again."""

    CHARGEBACK = "chargeback"
    """The payer disputed a payment with their bank: the free units of the
    batch are held until the dispute ends."""

    UNKNOWN = "unknown"
    """Any other event of the provider: ignored."""


class EventState(StrEnum):
    """How a provider's event was handled."""

    PROCESSED = "processed"
    """The journal did what the event asks."""

    IGNORED = "ignored"
    """Not an event billing acts on."""

    RECORDED = "recorded"
    """Kept for a person to decide; the journal is untouched. Chargebacks
    before BIL-PAY part 2 were recorded so; none is now (they are processed)."""

    FAILED = "failed"
    """Not handled (``error`` says why); handled again when it comes again."""
