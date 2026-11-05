"""Why a billing operation was refused (BIL-A).

A future administrator route maps them: :class:`BillingNotFoundError` to 404,
:class:`BillingInvalidError` to 422, :class:`BillingConflictError` to 409.
The script prints the message and exits with 1.
"""

from __future__ import annotations


class BillingError(Exception):
    """A billing operation was refused; the message says why."""


class BillingNotFoundError(BillingError):
    """The tenant, student, course, account or rule named does not exist."""


class BillingInvalidError(BillingError):
    """The request contradicts a billing rule (e.g. a student as a rule's payer)."""


class BillingConflictError(BillingError):
    """The request clashes with the current state (an active rule already
    exists, the rule is already ended, the terms revision is not current, the
    owner is not deleted)."""
