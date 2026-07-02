"""SMTP email delivery via aiosmtplib (provider-agnostic).

Transactional email is sent through a generic SMTP endpoint configured by the
``SMTP_*`` settings group; the provider (Resend in prod) is an operational
choice with zero code coupling. Mirrors the outbound-delivery shape of
``homework/webhook.py``: a module-level async send, ``get_settings()`` read
inside, and a fresh connection per send (``aiosmtplib.send`` opens and closes
the connection each call — no pooled client).

Delivery is log-only: no ``ExternalServiceCall`` row is written. The send task
creates no ``Job`` row, so there is no ``job_id`` anchor to attribute a call to,
and the recipient address is deliberately kept out of the DB trail (PII-lean).
The transient-vs-permanent retry policy lives in the ARQ task that calls this
(``workers/email_send.py``); this function is pure transport and lets SMTP
failures propagate.
"""

from __future__ import annotations

from email.message import EmailMessage

import aiosmtplib
import structlog

from course_supporter.config import get_settings

logger = structlog.get_logger(__name__)


async def send_email(*, to: str, subject: str, body: str) -> None:
    """Send a plain-text email through the configured SMTP endpoint.

    Opens a fresh connection per send (mirrors the webhook per-call client).
    Any SMTP failure propagates unchanged — the caller classifies it as
    transient (retry) or permanent (swallow).

    Args:
        to: Recipient address.
        subject: Rendered subject line.
        body: Rendered plain-text body.
    """
    settings = get_settings()

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        # Empty dev defaults mean "no auth": pass None so aiosmtplib skips the
        # AUTH exchange entirely against a local capture server.
        username=settings.smtp_user or None,
        password=settings.smtp_password.get_secret_value() or None,
        start_tls=settings.smtp_start_tls,
    )

    # PII-lean: log the recipient domain only — never the full mailbox and
    # never the rendered subject/body. Enough for delivery audit without
    # writing recipient or message content into the app-log trail; the "which
    # email" signal lives in the worker's ``message_id`` binding.
    _, _, domain = to.rpartition("@")
    logger.info("email_sent", recipient_domain=domain or "?")
