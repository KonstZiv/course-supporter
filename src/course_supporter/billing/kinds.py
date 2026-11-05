"""The closed vocabularies of billing (BIL-A).

The values are the ones the ``billing_*`` tables' check constraints allow;
a new value is a migration. The kind of action is the one of the shadow
estimates (:class:`course_supporter.cost_estimate.core.ActionKind`), so an
estimate and its payer speak of the same action.
"""

from __future__ import annotations

from enum import StrEnum

from course_supporter.cost_estimate.core import ActionKind

__all__ = [
    "AccountKind",
    "AccountStatus",
    "ActionKind",
    "AuditAction",
    "Benefit",
    "ConsentChannel",
    "PayerSource",
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


class PayerSource(StrEnum):
    """Why an account was chosen to pay (:func:`.payer.resolve_payer`)."""

    STUDENT_RULE = "student_rule"
    COURSE_RULE = "course_rule"
    DEFAULT = "default"
    """Nobody else pays: the student for a review, the author otherwise."""


class AuditAction(StrEnum):
    """What an audit entry records."""

    AUTHOR_SETTINGS_UPDATE = "author_settings.update"
    PAYMENT_RULE_CREATE = "payment_rule.create"
    PAYMENT_RULE_END = "payment_rule.end"
    ACCOUNT_ANONYMIZE = "account.anonymize"
