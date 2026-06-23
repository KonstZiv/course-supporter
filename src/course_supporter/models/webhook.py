"""Pydantic schemas for webhook delivery payloads.

This module is the canonical contract for what the service POSTs back to the
caller after homework review — per decision 2.4.13 the contract lives in code
(these models), not in a parallel hand-written file. sprint-mentor T5 finalised
it; the field set below is locked by ``tests/unit/test_webhook_contract.py`` so
any change is a deliberate, test-breaking one.

One event type today:
- reviewed: sent after Mentor review, delivers the final score and feedback.

Failure/mismatch notifications are deferred to T7: their shape depends on the
sanity result and the mismatch-gating decision, both of which land there.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ReviewSummary(BaseModel):
    """The review outcome carried by the 'reviewed' webhook (KD15, D9).

    ``score`` is the final post-denoising grade (D1); ``passed`` and
    ``correctness`` are the caller-facing verdict the review graph (T6) writes
    into ``review_result['verdict']``. Together they carry the outcome — there
    is no separate lifecycle ``status`` field (that vocabulary is unsettled
    across KD13/KD15 and is a T7 concern once failure events exist).
    """

    passed: bool = Field(description="Whether the submission meets the bar.")
    score: int = Field(ge=0, le=100, description="Final 0-100 grade (D1).")
    correctness: Literal["correct", "partially_correct", "incorrect"] = Field(
        description="Coarse correctness verdict from the review graph."
    )
    review_text: str = Field(description="Markdown review in mentor style.")
    response_language: str = Field(description="ISO 639-1 code.")


class WebhookReviewedPayload(BaseModel):
    """Payload sent when a submission review is complete.

    ``event`` is the discriminator for the (currently single) outbound event
    type; new event types added in T7 widen this Literal rather than mutating
    this payload.
    """

    event: Literal["reviewed"] = "reviewed"
    submission_id: str
    student_external_id: str
    review: ReviewSummary
    timestamp: datetime
