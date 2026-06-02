"""Make course (root) default_language mandatory + convert to ISO 639-3.

Phase 2.4 task 2.4.13:
* Backfill NULL ``default_language`` on root ``course_nodes`` to ``'ukr'``
  (operator-ratified: all existing courses are Ukrainian; local DB only).
* Convert existing ISO 639-1 codes in ``course_nodes.default_language``
  and ``authored_documents.language`` to canonical ISO 639-3.
* Enforce ``course_nodes_root_language_required`` CHECK constraint —
  ``parent_id IS NOT NULL OR default_language IS NOT NULL``. Children
  remain nullable (their language is dead-data — runtime inheritance
  always reads ``get_root_for`` per ``api/tasks.py:185``).

Inline 639-1 → 639-3 mapping (no app-code import, per Task 2.4.13
corrective 2): one row per allowed-whitelist language that has a
639-1 form. Codes already in 639-3 form (or 3-letter macrolanguage
codes like ``zh`` → ``cmn``) are passed through the map too. Anything
outside the map raises so operator can decide explicitly.

Revision ID: phase24_root_lang_required
Revises: phase24_segment_visual_content
Create Date: 2026-06-02 16:00:00.000000
"""

from collections.abc import Sequence
from typing import Final

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "phase24_root_lang_required"
down_revision: str | Sequence[str] | None = "phase24_segment_visual_content"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ISO 639-1 (and a couple of common 2-letter macrolanguage shorthands) → 639-3.
# Coverage = the 57 whitelisted languages from ``config/languages.yaml``.
# Languages without a 639-1 form (yue, fil, cmn) are mapped only from their
# 639-3 to themselves; their 639-1 entries simply do not exist. ``zh`` is
# routed to ``cmn`` because Mandarin is the whitelisted variant.
_LANG_MAP: Final[dict[str, str]] = {
    # Excellent tier
    "be": "bel",
    "bs": "bos",
    "bg": "bul",
    "ca": "cat",
    "hr": "hrv",
    "cs": "ces",
    "da": "dan",
    "nl": "nld",
    "en": "eng",
    "et": "est",
    "fi": "fin",
    "fr": "fra",
    "gl": "glg",
    "de": "deu",
    "el": "ell",
    "hu": "hun",
    "is": "isl",
    "id": "ind",
    "it": "ita",
    "ja": "jpn",
    "kn": "kan",
    "lv": "lav",
    "mk": "mkd",
    "ms": "msa",
    "ml": "mal",
    "no": "nor",
    "pl": "pol",
    "pt": "por",
    "ro": "ron",
    "ru": "rus",
    "sk": "slk",
    "es": "spa",
    "sv": "swe",
    "tr": "tur",
    "uk": "ukr",
    "vi": "vie",
    # High tier
    "hy": "hye",
    "az": "aze",
    "bn": "ben",
    "ka": "kat",
    "gu": "guj",
    "hi": "hin",
    "kk": "kaz",
    "lt": "lit",
    "mt": "mlt",
    "zh": "cmn",
    "mr": "mar",
    "ne": "nep",
    "or": "ori",
    "fa": "fas",
    "sr": "srp",
    "sl": "slv",
    "sw": "swa",
    "ta": "tam",
    "te": "tel",
}

# Idempotency: codes already in the canonical 639-3 form pass through unchanged.
_PASSTHROUGH: Final[frozenset[str]] = frozenset(
    {
        "bel",
        "bos",
        "bul",
        "cat",
        "hrv",
        "ces",
        "dan",
        "nld",
        "eng",
        "est",
        "fin",
        "fra",
        "glg",
        "deu",
        "ell",
        "hun",
        "isl",
        "ind",
        "ita",
        "jpn",
        "kan",
        "lav",
        "mkd",
        "msa",
        "mal",
        "nor",
        "pol",
        "por",
        "ron",
        "rus",
        "slk",
        "spa",
        "swe",
        "tur",
        "ukr",
        "vie",
        "hye",
        "aze",
        "ben",
        "yue",
        "fil",
        "kat",
        "guj",
        "hin",
        "kaz",
        "lit",
        "mlt",
        "cmn",
        "mar",
        "nep",
        "ori",
        "fas",
        "srp",
        "slv",
        "swa",
        "tam",
        "tel",
    }
)


def _normalize(raw: str) -> str:
    """Canonical 639-3 for any supported input.

    Raises ``RuntimeError`` for unknown codes so the migration aborts
    loudly and the operator decides how to map the row by hand.
    """
    value = raw.strip().lower()
    if value in _PASSTHROUGH:
        return value
    if value in _LANG_MAP:
        return _LANG_MAP[value]
    raise RuntimeError(
        f"Cannot map language code {raw!r} to the project whitelist (see "
        f"config/languages.yaml). Backfill manually before re-running."
    )


# Reverse map for downgrade (639-3 → 639-1). Languages without a 639-1
# form (yue, fil, cmn) have no reverse entry and are left unchanged
# (retain 639-3) on downgrade — best-effort, irreversible by construction.
_REVERSE_MAP: Final[dict[str, str]] = {v: k for k, v in _LANG_MAP.items()}


def upgrade() -> None:
    """Backfill, normalize, then add the CHECK constraint."""
    conn = op.get_bind()

    # Step 1 — backfill NULL ``default_language`` on root nodes to 'ukr'.
    # Operator-ratified silent default (all existing courses Ukrainian per
    # Task 2.4.13). Children remain NULL by design (dead-data per §1).
    conn.execute(
        sa.text(
            "UPDATE course_nodes "
            "SET default_language = 'ukr' "
            "WHERE parent_id IS NULL AND default_language IS NULL"
        )
    )

    # Step 2 — convert existing 639-1 codes to 639-3 on both columns.
    for table, column in (
        ("course_nodes", "default_language"),
        ("authored_documents", "language"),
    ):
        # ``table``/``column`` are hard-coded loop literals — not user
        # input — so the f-string is safe; ruff S608 is a false positive.
        rows = conn.execute(
            sa.text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")  # noqa: S608
        ).fetchall()
        for row_id, raw in rows:
            normalized = _normalize(raw)
            if normalized != raw:
                conn.execute(
                    sa.text(f"UPDATE {table} SET {column} = :new WHERE id = :id"),  # noqa: S608
                    {"new": normalized, "id": row_id},
                )

    # Step 3 — enforce root-language invariant via CHECK constraint.
    op.create_check_constraint(
        "course_nodes_root_language_required",
        "course_nodes",
        "parent_id IS NOT NULL OR default_language IS NOT NULL",
    )


def downgrade() -> None:
    """Drop the CHECK and reverse-map 639-3 → 639-1 best-effort.

    Cannot restore NULL on root rows backfilled in upgrade Step 1
    (information lost by construction); those rows keep their 639-1 form
    after this downgrade. Documented as irreversible.
    """
    op.drop_constraint(
        "course_nodes_root_language_required",
        "course_nodes",
        type_="check",
    )

    conn = op.get_bind()
    for table, column in (
        ("course_nodes", "default_language"),
        ("authored_documents", "language"),
    ):
        # ``table``/``column`` are hard-coded loop literals — not user
        # input — so the f-string is safe; ruff S608 is a false positive.
        rows = conn.execute(
            sa.text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")  # noqa: S608
        ).fetchall()
        for row_id, raw in rows:
            mapped = _REVERSE_MAP.get(raw)
            if mapped is not None:
                conn.execute(
                    sa.text(f"UPDATE {table} SET {column} = :new WHERE id = :id"),  # noqa: S608
                    {"new": mapped, "id": row_id},
                )
