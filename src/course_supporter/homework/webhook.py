"""Webhook delivery for the homework pipeline.

Delivers two event types to external systems:
- matched: after task matching (tells which task was identified)
- reviewed: after Mentor review (delivers score and feedback)

Retry strategy: inline exponential backoff, non-blocking on failure.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import structlog

from course_supporter.api.url_validation import validate_webhook_url
from course_supporter.config import get_settings
from course_supporter.models.webhook import (
    MatchedTaskInfo,
    ReviewSummary,
    WebhookMatchedPayload,
    WebhookReviewedPayload,
)
from course_supporter.storage.orm import ExternalServiceCall

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.storage.orm import HomeworkSubmission, Student, Tenant

logger = structlog.get_logger()


def resolve_webhook_url(
    submission: HomeworkSubmission,
    tenant: Tenant | None,
) -> str | None:
    """Return the webhook URL for this submission (per-submission or tenant default)."""
    return submission.webhook_url or (tenant.webhook_url if tenant else None)


def build_matched_payload(
    submission: HomeworkSubmission,
    student: Student,
    matched_task_id: uuid.UUID,
    matched_task_title: str,
    matched_task_type: str,
) -> WebhookMatchedPayload:
    """Build the 'matched' webhook payload from pipeline state."""
    return WebhookMatchedPayload(
        submission_id=str(submission.id),
        student_external_id=student.external_id,
        matched_task=MatchedTaskInfo(
            id=str(matched_task_id),
            title=matched_task_title,
            node_type=matched_task_type,
        ),
        timestamp=datetime.now(UTC),
    )


def build_reviewed_payload(
    submission: HomeworkSubmission,
    student: Student,
) -> WebhookReviewedPayload:
    """Build the 'reviewed' webhook payload from stored review result."""
    review_result = submission.review_result or {}
    analysis = review_result.get("analysis", {})

    return WebhookReviewedPayload(
        submission_id=str(submission.id),
        student_external_id=student.external_id,
        review=ReviewSummary(
            passed=analysis.get("passed", False),
            score=analysis.get("score", 0),
            correctness=analysis.get("correctness", "incorrect"),
            review_text=submission.review_markdown or "",
            response_language=submission.response_language or "en",
        ),
        timestamp=datetime.now(UTC),
    )


async def deliver_webhook(
    *,
    url: str,
    payload: WebhookMatchedPayload | WebhookReviewedPayload,
    tenant_id: uuid.UUID,
    session: AsyncSession,
) -> bool:
    """Deliver a webhook payload with retry and audit logging.

    Args:
        url: Target webhook URL.
        payload: Pydantic model to POST as JSON.
        tenant_id: For audit trail.
        session: DB session for ExternalServiceCall record.

    Returns:
        True if delivery succeeded (2xx), False otherwise.
    """
    settings = get_settings()
    log = logger.bind(
        webhook_url=url,
        event=payload.event,
        submission_id=payload.submission_id,
    )

    # SSRF re-validation (tenant default URL bypasses API validation)
    try:
        await validate_webhook_url(url)
    except Exception:
        log.warning("webhook_ssrf_blocked")
        _record_attempt(
            session,
            tenant_id=tenant_id,
            event=payload.event,
            latency_ms=0,
            success=False,
            error="SSRF validation failed",
        )
        return False

    start = time.monotonic()
    last_error: str = ""

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(float(settings.webhook_timeout_seconds)),
    ) as client:
        for attempt in range(settings.webhook_max_retries):
            try:
                response = await client.post(
                    url,
                    json=payload.model_dump(mode="json"),
                    headers={"Content-Type": "application/json"},
                )

                if response.is_success:
                    latency_ms = int((time.monotonic() - start) * 1000)
                    log.info(
                        "webhook_delivered",
                        status_code=response.status_code,
                        attempt=attempt + 1,
                        latency_ms=latency_ms,
                    )
                    _record_attempt(
                        session,
                        tenant_id=tenant_id,
                        event=payload.event,
                        latency_ms=latency_ms,
                        success=True,
                    )
                    return True

                # 4xx (except 429) = permanent failure, no retry
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    last_error = f"HTTP {response.status_code}"
                    log.warning(
                        "webhook_permanent_failure",
                        status_code=response.status_code,
                        attempt=attempt + 1,
                    )
                    break

                # 429 / 5xx = transient, retry
                last_error = f"HTTP {response.status_code}"
                log.warning(
                    "webhook_transient_failure",
                    status_code=response.status_code,
                    attempt=attempt + 1,
                )

            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "webhook_network_error",
                    error=last_error,
                    attempt=attempt + 1,
                )

            # Exponential backoff before next attempt
            if attempt < settings.webhook_max_retries - 1:
                delay = 2**attempt  # 1s, 2s, 4s
                await asyncio.sleep(delay)

    latency_ms = int((time.monotonic() - start) * 1000)
    log.error(
        "webhook_delivery_failed",
        last_error=last_error,
        latency_ms=latency_ms,
    )
    _record_attempt(
        session,
        tenant_id=tenant_id,
        event=payload.event,
        latency_ms=latency_ms,
        success=False,
        error=last_error,
    )
    return False


def _record_attempt(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event: str,
    latency_ms: int,
    success: bool,
    error: str | None = None,
) -> None:
    """Create an ExternalServiceCall audit record for a webhook attempt."""
    session.add(
        ExternalServiceCall(
            tenant_id=tenant_id,
            action=f"webhook_{event}",
            strategy="exponential_backoff",
            provider="httpx",
            model_id="webhook_post",
            latency_ms=latency_ms,
            success=success,
            error_message=error,
        )
    )
