"""The expected cost E of an action, computed in memory for charging (BIL-E).

The shadow estimate (BIL-D) writes its row beside the action and swallows
every failure; charging needs the number itself. These functions call the
same pure estimate functions (:mod:`course_supporter.cost_estimate.actions`)
with the same catalog, ladders and settings, and return E — so a lost row
costs only the calibration, never the price (operator 2026-11-15, S6).

When the computation itself fails, a review says so — ``None``: its path's
money ceiling stands for E (BIL-E2, 4a), with one WARNING
``billing_quote_ceiling`` naming the reason; a processing or a preparation
returns ``None`` and is not charged (no estimate, no charge — operator
2026-11-15, S2).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

import structlog

from course_supporter.cost_estimate.actions import (
    LadderStage,
    MaterialFactors,
    estimate_processing,
    estimate_review,
    estimate_task_preparation,
)
from course_supporter.cost_estimate.settings import get_estimate_settings
from course_supporter.cost_estimate.shadow import load_catalog, load_ladder_stages

logger = structlog.get_logger(__name__)


def _now(at: datetime | None) -> datetime:
    return at or datetime.now(UTC)


CEILING_WARNING: Final = "billing_quote_ceiling"
"""The one WARNING of a review priced at its ceiling; ``reason`` says why."""


def review_e_usd(
    *,
    stage_names: Sequence[str],
    path_stages: Mapping[str, LadderStage],
    submission_chars: int,
    fallback_usd: Decimal,
    at: datetime | None = None,
) -> Decimal | None:
    """E of one review; ``None`` — it cannot be made, the ceiling stands for it.

    ``None`` is the mark "fell to the ceiling" (BIL-E2, 4a): the funds port
    reads it as ``SubmissionContext.expected_usd`` does — ``fallback_usd``, the
    path's money ceiling, is the price, the threshold and the reserve. The
    WARNING names the reason (the error's class), never the student's work.
    """
    try:
        return estimate_review(
            stage_names=stage_names,
            path_stages=path_stages,
            settings=get_estimate_settings(),
            catalog=load_catalog(),
            submission_chars=submission_chars,
            at=_now(at),
        ).e_usd
    except Exception as exc:
        logger.warning(
            CEILING_WARNING,
            reason="estimate_failed",
            error=type(exc).__name__,
            ceiling_usd=str(fallback_usd),
        )
        return None


def preparation_e_usd(
    *, kind: str, task_chars: int, at: datetime | None = None
) -> Decimal | None:
    """E of one task preparation of ``kind``; ``None`` if it cannot be made."""
    try:
        return estimate_task_preparation(
            kind=kind,
            ladders=load_ladder_stages(),
            settings=get_estimate_settings(),
            catalog=load_catalog(),
            task_chars=task_chars,
            at=_now(at),
        ).e_usd
    except Exception:
        logger.warning(
            "billing_estimate_failed", action_kind="task_preparation", exc_info=True
        )
        return None


def key_explanation_task_chars(task_text: str, answers: object) -> int:
    """The size of what the explanations of a test's key are shown, in characters.

    The test's text as the model reads it and its key as JSON. One rule for the
    two places that price the work: the route that shows the author the price
    before asking (``POST /documents/{document_id}/reference/estimate``) and
    the worker that reserves E before writing them (``workers/key_explain.py``,
    step 2) — so the price shown is the E reserved.
    """
    return len(task_text) + len(json.dumps(answers, default=str))


def processing_e_usd(
    *, kind: str, factors: MaterialFactors, at: datetime | None = None
) -> Decimal | None:
    """E of processing one material of ``kind``; ``None`` if it cannot be made."""
    try:
        return estimate_processing(
            kind=kind,
            factors=factors,
            ladders=load_ladder_stages(),
            settings=get_estimate_settings(),
            catalog=load_catalog(),
            at=_now(at),
        ).e_usd
    except Exception:
        logger.warning(
            "billing_estimate_failed", action_kind="processing", exc_info=True
        )
        return None
