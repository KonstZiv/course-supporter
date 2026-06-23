"""Pydantic schemas for webhook delivery payloads.

One event type:
- reviewed: sent after Mentor review, delivers the final score and feedback.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ReviewSummary(BaseModel):
    """Key review fields included in the 'reviewed' webhook."""

    passed: bool
    score: int = Field(ge=0, le=100)
    correctness: Literal["correct", "partially_correct", "incorrect"]
    review_text: str = Field(description="Markdown review in mentor style.")
    response_language: str = Field(description="ISO 639-1 code.")


class WebhookReviewedPayload(BaseModel):
    """Payload sent when a submission review is complete."""

    event: Literal["reviewed"] = "reviewed"
    submission_id: str
    student_external_id: str
    review: ReviewSummary
    timestamp: datetime
