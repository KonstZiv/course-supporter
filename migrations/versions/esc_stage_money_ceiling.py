"""mentor-rebuild task 03: a rung can be skipped because the stage cannot pay for it.

Revision ID: esc_stage_money_ceiling
Revises: esc_funds_port
Create Date: 2026-09-16

Two changes, in two tables, for one idea — the rebuilt Mentor's path can stop
before it spends:

1. ``external_service_calls.skip_reason`` admits ``money_ceiling_exceeded``.
   A submission path's stage carries a money ceiling (task 02); a rung whose
   single attempt costs more than what is left of it is skipped WITHOUT a call,
   and the register says why. It is the money twin of ``input_budget_exceeded``:
   one counts tokens a rung's window cannot hold, the other dollars a stage's
   ceiling cannot pay. The CHECK is re-issued rather than altered — Postgres has
   no "widen a CHECK" — and the value list is spelled out LITERALLY on purpose:
   the ORM builds the same list from ``SkipReason``, and a migration must not
   change meaning when the code later does.

2. ``homework_submissions.status`` gets a comment that names ``awaiting_funds``.
   The column is ``VARCHAR(30)`` with no CHECK, so the new state itself needs no
   schema change; the comment is the only place in the database that lists the
   states, and a comment that lists eight of nine is worse than one that lists
   none. NO back-fill and no data change: no row can carry the new state yet.

   The comment this replaces in the DATABASE is older than the one in the ORM:
   measured before writing this file, the column carried
   "received → safety_check → matching → matched → reviewing → completed →
   delivered | rejected | failed" — the vocabulary from before the sprint-mentor
   T7 reconciliation, naming three states that no longer exist. That is one cell
   of the whole-schema comment drift (``DD-L4-A``), and the upgrade closes it on
   the way past. The downgrade therefore restores the ORM's task-02 text, NOT
   that stale line: putting back a comment that names states the code does not
   have would be restoring a lie, and nothing reads the difference.

DELIBERATELY NOT included: the whole-schema comment and index drift that
``alembic revision --autogenerate`` reports on this database (``DD-L4-A``).
Autogenerate on this schema emits ~200 operations, of which the ones belonging
to this task are two; this file is hand-written (SPRINT rule 6) and carries only
the changes above.

Downgrade restores both: the three-value skip-reason CHECK and the task-02
comment on the submission status.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "esc_stage_money_ceiling"
down_revision: str | Sequence[str] | None = "esc_funds_port"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ESC = "external_service_calls"
_SUBMISSIONS = "homework_submissions"
_SKIP_CHECK = "ck_esc_skip_reason"

# Spelled out, not derived from SkipReason: see the module docstring.
_SKIP_REASONS_BEFORE = (
    "provider_not_configured",
    "provider_disabled",
    "input_budget_exceeded",
)
_SKIP_REASONS_AFTER = (*_SKIP_REASONS_BEFORE, "money_ceiling_exceeded")

_STATUS_COMMENT_BEFORE = (
    "Lifecycle milestone (KD15 §1298): received → safety_ok → sanity_ok → "
    "reviewing → completed → delivered; terminals rejected (safety) | "
    "mismatch (sanity) | failed (error)."
)
_STATUS_COMMENT_AFTER = (
    _STATUS_COMMENT_BEFORE + " awaiting_funds (mentor-rebuild task 03) is a "
    "hold, not a milestone: the funds port refused before the new path's first "
    "paid call, so nothing was spent; a top-up re-activates it through received."
)


def _skip_reason_check(values: Sequence[str]) -> str:
    listed = ", ".join(f"'{v}'" for v in values)
    return f"skip_reason IS NULL OR skip_reason IN ({listed})"


def _reissue_skip_check(values: Sequence[str]) -> None:
    op.drop_constraint(_SKIP_CHECK, _ESC, type_="check")
    op.create_check_constraint(_SKIP_CHECK, _ESC, _skip_reason_check(values))


def _set_status_comment(comment: str, existing_comment: str) -> None:
    op.alter_column(
        _SUBMISSIONS,
        "status",
        existing_type=sa.String(length=30),
        existing_nullable=False,
        comment=comment,
        existing_comment=existing_comment,
    )


def upgrade() -> None:
    """Admit the money-ceiling skip reason; name awaiting_funds in the comment."""
    _reissue_skip_check(_SKIP_REASONS_AFTER)
    _set_status_comment(_STATUS_COMMENT_AFTER, _STATUS_COMMENT_BEFORE)


def downgrade() -> None:
    """Restore the three-value skip reason and the task-02 status comment."""
    _reissue_skip_check(_SKIP_REASONS_BEFORE)
    _set_status_comment(_STATUS_COMMENT_BEFORE, _STATUS_COMMENT_AFTER)
