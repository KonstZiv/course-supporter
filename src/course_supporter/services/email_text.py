"""Ukrainian transactional-email templates (student portal password-recovery).

Email texts live here as a flat ``message-id -> template`` mapping rather than
inline f-strings in the send-service, so the send path stays text-agnostic and
a future i18n dimension keys by language beside the message-id (the selector
already exists as ``Student.preferred_language``). uk-only MVP; no i18n library.

Bodies carry ``str.format`` placeholders (e.g. ``{reset_url}``, ``{ttl}``)
filled at send time. R1 ships the texts only; the reset/confirm routes that
supply the URLs and TTLs land in R2.
"""

from __future__ import annotations

from typing import NamedTuple


class EmailTemplate(NamedTuple):
    """A subject/body pair carrying ``str.format`` placeholders."""

    subject: str
    body: str


# message-id -> uk template. The two ids mirror the token purposes in
# ``student_credential_tokens`` (R2): ``password_reset`` and ``email_confirm``.
_TEMPLATES: dict[str, EmailTemplate] = {
    "password_reset": EmailTemplate(
        subject="Відновлення пароля",
        body=(
            "Вітаємо!\n\n"
            "Ми отримали запит на відновлення пароля до вашого облікового "
            "запису на навчальному порталі.\n\n"
            "Щоб задати новий пароль, перейдіть за посиланням:\n"
            "{reset_url}\n\n"
            "Посилання дійсне {ttl}. Якщо ви не надсилали цей запит, просто "
            "проігноруйте цей лист — ваш пароль лишиться без змін.\n"
        ),
    ),
    "email_confirm": EmailTemplate(
        subject="Підтвердження адреси для відновлення пароля",
        body=(
            "Вітаємо!\n\n"
            "Підтвердіть цю адресу, щоб надалі мати змогу самостійно "
            "відновлювати пароль до навчального порталу:\n"
            "{confirm_url}\n\n"
            "Посилання дійсне {ttl}. Якщо ви не додавали цю адресу, просто "
            "проігноруйте цей лист.\n"
        ),
    ),
}


class RenderedEmail(NamedTuple):
    """A rendered subject/body pair, ready for the send-service."""

    subject: str
    body: str


def render_email(message_id: str, /, **context: str) -> RenderedEmail:
    """Render a uk email template by message-id, filling its placeholders.

    Args:
        message_id: One of the keys in ``_TEMPLATES``.
        **context: Placeholder values substituted into subject and body.

    Returns:
        The rendered ``(subject, body)``.

    Raises:
        KeyError: The message-id is unknown, or a body placeholder was not
            supplied. Both are programmer errors — the caller passes a fixed
            literal message-id and the placeholder set is part of the template
            contract — so failing loudly is correct.
    """
    template = _TEMPLATES[message_id]
    return RenderedEmail(
        subject=template.subject.format_map(context),
        body=template.body.format_map(context),
    )
