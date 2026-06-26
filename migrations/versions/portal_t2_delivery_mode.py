"""Phase 6 T2: homework submission delivery_mode marker.

Revision ID: portal_t2_delivery_mode
Revises: portal_t1_identity
Create Date: 2026-06-26 00:00:00.000000

Adds the delivery marker that routes a reviewed submission to its destination
(KD17 §3 portal contract):

  - ``homework_submissions.delivery_mode`` — ``'webhook'`` (mode-1, the
    DEFAULT — caller-owned student, review POSTed back; unchanged) or
    ``'in_app'`` (mode-2 — portal session submission, read via the read-path).
    For ``'in_app'`` no webhook fires even when the tenant has a default
    ``webhook_url``, and the submission terminates at ``'completed'`` (never
    ``'delivered'``). Guarded by ``ck_homework_delivery_mode``.

Forward-only: ``delivery_mode`` is NOT NULL with a ``server_default`` of
``'webhook'``, so existing rows backfill to mode-1 (the only mode that existed)
and mode-1's behaviour is byte-identical. The downgrade drops the column and
the check for round-trip integrity only.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "portal_t2_delivery_mode"
down_revision = "portal_t1_identity"
branch_labels = None
depends_on = None

_HOMEWORK = "homework_submissions"
_DELIVERY_CHECK = "ck_homework_delivery_mode"


def upgrade() -> None:
    """Add delivery_mode (server_default 'webhook') + its IN-check."""
    op.add_column(
        _HOMEWORK,
        sa.Column(
            "delivery_mode",
            sa.String(length=20),
            nullable=False,
            server_default="webhook",
            comment=(
                "Where the review is delivered (Phase 6 T2, KD17). 'webhook' "
                "(mode-1, the default — caller-owned student, review POSTed "
                "back; unchanged) or 'in_app' (mode-2 — portal session "
                "submission). For 'in_app' NO webhook fires even when the "
                "tenant has a default webhook_url (resolve_webhook_url returns "
                "None), and the submission terminates at 'completed' for the "
                "student to read via the read-path (it never reaches "
                "'delivered'). Enforced by ck_homework_delivery_mode."
            ),
        ),
    )
    op.create_check_constraint(
        _DELIVERY_CHECK,
        _HOMEWORK,
        "delivery_mode IN ('webhook', 'in_app')",
    )


def downgrade() -> None:
    """Reverse: drop the check and the column (round-trip only)."""
    op.drop_constraint(_DELIVERY_CHECK, _HOMEWORK, type_="check")
    op.drop_column(_HOMEWORK, "delivery_mode")
