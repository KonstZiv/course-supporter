"""Webhook delivery for the homework pipeline.

Delivers three event types to external systems (T7 added the latter two):
- reviewed: after Mentor review (delivers score and feedback)
- mismatch: sanity gate rejected the submission as off-task (no review ran)
- failed: processing errored out (our fault, no review produced)

Retry strategy: inline exponential backoff, non-blocking on failure.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import structlog

from course_supporter.api.url_validation import validate_webhook_url
from course_supporter.config import get_settings
from course_supporter.models.webhook import (
    ReviewSummary,
    WebhookFailedPayload,
    WebhookMismatchPayload,
    WebhookPayload,
    WebhookReviewedPayload,
)
from course_supporter.service_logging import get_current_job_id
from course_supporter.storage.orm import ExternalServiceCall

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.storage.orm import HomeworkSubmission, Student, Tenant

logger = structlog.get_logger()


def resolve_webhook_url(
    submission: HomeworkSubmission,
    tenant: Tenant | None,
) -> str | None:
    """Return the webhook URL for this submission, or None if none applies.

    The ``delivery_mode`` marker is read FIRST (Phase 6 T2, KD17): a portal
    ``in_app`` submission has no webhook target, so this returns None even when
    the tenant has a default ``webhook_url``. Every webhook path (reviewed,
    mismatch, failed) gates on this function, so the marker isolates the portal
    submission from the caller webhook entirely — the submission terminates at
    ``completed`` and the student reads the review via the read-path. Mode-1
    (``delivery_mode='webhook'``) is unchanged: per-submission URL, then the
    tenant default.
    """
    if submission.delivery_mode == "in_app":
        return None
    return submission.webhook_url or (tenant.webhook_url if tenant else None)


def build_reviewed_payload(
    submission: HomeworkSubmission,
    student: Student,
) -> WebhookReviewedPayload:
    """Build the 'reviewed' webhook payload from stored review data.

    Per D1 (KD15) the headline ``score`` is read from its typed column, not
    from ``review_result`` — re-reading it out of the JSONB would re-duplicate
    it and reintroduce the sync drift KD15 warns against. ``passed`` and
    ``correctness`` come from the caller-facing ``verdict`` block, the only
    ``review_result`` key pinned in T3; its full layered shape lands with the
    review graph in T6. Until the graph writes a real verdict (the T6 stub
    does not), the reads degrade to safe defaults so the webhook still builds.
    """
    review_result = submission.review_result or {}
    verdict = review_result.get("verdict", {})

    return WebhookReviewedPayload(
        submission_id=str(submission.id),
        student_external_id=student.external_id,
        review=ReviewSummary(
            passed=verdict.get("passed", False),
            score=submission.score or 0,
            correctness=verdict.get("correctness", "incorrect"),
            review_text=submission.review_markdown or "",
            response_language=submission.response_language or "en",
        ),
        timestamp=datetime.now(UTC),
    )


def build_mismatch_payload(
    submission: HomeworkSubmission,
    student: Student,
    *,
    reason: str,
) -> WebhookMismatchPayload:
    """Build the 'mismatch' webhook payload (T7 sanity gate rejection).

    No review ran, so there is no score — only the sanity gate's ``reason``.
    """
    return WebhookMismatchPayload(
        submission_id=str(submission.id),
        student_external_id=student.external_id,
        reason=reason,
        timestamp=datetime.now(UTC),
    )


def build_failed_payload(
    submission: HomeworkSubmission,
    student: Student,
    *,
    reason: str,
) -> WebhookFailedPayload:
    """Build the 'failed' webhook payload (T7 processing error)."""
    return WebhookFailedPayload(
        submission_id=str(submission.id),
        student_external_id=student.external_id,
        reason=reason,
        timestamp=datetime.now(UTC),
    )


async def deliver_webhook(
    *,
    url: str,
    payload: WebhookPayload,
    session: AsyncSession,
) -> bool:
    """Deliver a webhook payload with retry and audit logging.

    Args:
        url: Target webhook URL.
        payload: Pydantic model to POST as JSON.
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
                        event=payload.event,
                        latency_ms=latency_ms,
                        success=True,
                    )
                    return True

                body_snippet = response.text[:200] if response.text else ""

                # 4xx (except 429) = permanent failure, no retry
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    last_error = f"HTTP {response.status_code}"
                    log.warning(
                        "webhook_permanent_failure",
                        status_code=response.status_code,
                        attempt=attempt + 1,
                        response_body=body_snippet,
                    )
                    break

                # 429 / 5xx = transient, retry
                last_error = f"HTTP {response.status_code}"
                log.warning(
                    "webhook_transient_failure",
                    status_code=response.status_code,
                    attempt=attempt + 1,
                    response_body=body_snippet,
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
                log.debug(
                    "webhook_retry_backoff",
                    delay_sec=delay,
                    next_attempt=attempt + 2,
                )
                await asyncio.sleep(delay)

    latency_ms = int((time.monotonic() - start) * 1000)
    log.error(
        "webhook_delivery_failed",
        last_error=last_error,
        latency_ms=latency_ms,
    )
    _record_attempt(
        session,
        event=payload.event,
        latency_ms=latency_ms,
        success=False,
        error=last_error,
    )
    return False


def _record_attempt(
    session: AsyncSession,
    *,
    event: str,
    latency_ms: int,
    success: bool,
    error: str | None = None,
) -> None:
    """Create an ExternalServiceCall audit record for a webhook attempt."""
    session.add(
        ExternalServiceCall(
            job_id=get_current_job_id(),
            action=f"webhook_{event}",
            strategy="exponential_backoff",
            provider="httpx",
            model_id="webhook_post",
            latency_ms=latency_ms,
            success=success,
            error_message=error,
        )
    )
