"""Pydantic schemas for webhook delivery payloads.

Two event types:
- matched: after task matching, tells the external system which task.
- reviewed: sent after Mentor review, delivers the final score and feedback.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MatchedTaskInfo(BaseModel):
    """Compact task descriptor included in the 'matched' webhook."""

    id: str = Field(description="Matched task UUID.")
    title: str
    node_type: str = Field(description="exercise, concept, lesson, etc.")


class ReviewSummary(BaseModel):
    """Key review fields included in the 'reviewed' webhook."""

    passed: bool
    score: int = Field(ge=0, le=100)
    correctness: Literal["correct", "partially_correct", "incorrect"]
    review_text: str = Field(description="Markdown review in mentor style.")
    response_language: str = Field(description="ISO 639-1 code.")


class WebhookMatchedPayload(BaseModel):
    """Payload sent when a submission is matched to a course task."""

    event: Literal["matched"] = "matched"
    submission_id: str
    student_external_id: str
    matched_task: MatchedTaskInfo
    timestamp: datetime


class WebhookReviewedPayload(BaseModel):
    """Payload sent when a submission review is complete."""

    event: Literal["reviewed"] = "reviewed"
    submission_id: str
    student_external_id: str
    review: ReviewSummary
    timestamp: datetime
