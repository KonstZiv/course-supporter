"""Email send ARQ task (student portal password-recovery, R1).

Fourth ARQ enqueue form. Unlike the ingestion / s3-cleanup / homework tasks,
this job creates NO ``Job`` row: a transactional email is fire-and-forget
notification, not a durable user-facing artifact with queryable status. It is
enqueued directly (``arq.enqueue_job("arq_send_email", ...)``) on the default
queue and carries no DB tracking. Retry is ARQ's built-in ``max_tries``:
transient SMTP failures propagate (and are retried); permanent ones are logged
and swallowed so the single-worker queue is not blocked burning retries on a
dead address.
"""

from __future__ import annotations

from typing import Any

import aiosmtplib
import structlog

from course_supporter.services.email_service import send_email
from course_supporter.services.email_text import render_email

logger = structlog.get_logger(__name__)


async def arq_send_email(
    ctx: dict[str, Any],
    *,
    message_id: str,
    to: str,
    context: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Render a uk email template by message-id and send it via SMTP.

    ``ctx`` is unused — the SMTP connection is opened per-send inside
    ``send_email`` — but is required by the ARQ task signature.

    Transient-vs-permanent policy mirrors the s3_cleanup ClientError split:

    * A permanent SMTP rejection (5xx response, or all recipients refused)
      will not be fixed by retrying — log it and return so ARQ does not burn
      ``max_tries`` on it.
    * Any other failure (4xx transient response, connection-level error)
      propagates so ARQ's ``max_tries`` retry fires.

    Returns:
        ``{"sent": True}`` on success, or ``{"sent": False, "error": <str>}``
        when a permanent rejection was swallowed.
    """
    log = logger.bind(message_id=message_id)
    rendered = render_email(message_id, **(context or {}))

    try:
        await send_email(to=to, subject=rendered.subject, body=rendered.body)
    except aiosmtplib.SMTPRecipientsRefused as exc:
        # All recipients rejected (bad / unknown address) — permanent.
        log.warning("email_send.recipients_refused", error=str(exc))
        return {"sent": False, "error": str(exc)}
    except aiosmtplib.SMTPResponseException as exc:
        if 500 <= exc.code < 600:
            log.warning("email_send.permanent_failure", code=exc.code, error=str(exc))
            return {"sent": False, "error": f"{exc.code}: {exc}"}
        raise  # 4xx transient — let ARQ retry

    log.info("email_send.complete")
    return {"sent": True}
