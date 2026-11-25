"""Transactional-email templates (student portal password-recovery).

Email texts live here as a ``language -> message-id -> template`` mapping
rather than inline f-strings in the send-service, so the send path stays
text-agnostic. Templates are keyed by language (``ukr`` and ``eng``; any
other language falls back to Ukrainian), selected by
``Student.interface_language``.

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


# message-id -> template by language. Supported languages: "ukr" and "eng".
# Two ids mirror the token purposes in ``student_credential_tokens`` (R2):
# ``password_reset`` and ``email_confirm``; ``portal_signup_confirm`` is the
# confirm email of a self-enrollment by a course link (O-1).
_TEMPLATES: dict[str, dict[str, EmailTemplate]] = {
    "ukr": {
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
        "portal_signup_confirm": EmailTemplate(
            subject="Підтвердіть запис на курс «{course_title}»",
            body=(
                "Вітаємо!\n\n"
                "Ви записуєтеся на курс «{course_title}» автора {author_name} "
                "на навчальному порталі. Щоб завершити запис, підтвердіть цю "
                "адресу за посиланням:\n"
                "{confirm_url}\n\n"
                "Підтвердіть протягом {ttl}, інакше запис буде скасовано — "
                "доведеться записатися знову.\n\n"
                "Якщо у вас уже є вхід у цього автора, підтверджувати не "
                "обов'язково: увійдіть і запишіться на курс зі сторінки "
                "запису.\n\n"
                "Вхід до порталу автора: {author_login_url}\n"
                "Спільна сторінка входу: {shared_login_url}\n\n"
                "Якщо ви не записувалися на цей курс, просто проігноруйте цей "
                "лист.\n"
            ),
        ),
    },
    "eng": {
        "password_reset": EmailTemplate(
            subject="Password reset",
            body=(
                "Hello!\n\n"
                "We received a request to reset the password for your account on "
                "the learning portal.\n\n"
                "To set a new password, follow this link:\n"
                "{reset_url}\n\n"
                "The link is valid for {ttl}. If you did not make this request, "
                "simply ignore this email — your password will stay unchanged.\n"
            ),
        ),
        "email_confirm": EmailTemplate(
            subject="Confirm your address for password recovery",
            body=(
                "Hello!\n\n"
                "Confirm this address so that you can reset your password to "
                "the learning portal yourself in the future:\n"
                "{confirm_url}\n\n"
                "The link is valid for {ttl}. If you did not add this address, "
                "simply ignore this email.\n"
            ),
        ),
        "portal_signup_confirm": EmailTemplate(
            subject="Confirm your enrollment in “{course_title}”",
            body=(
                "Hello!\n\n"
                "You are enrolling in the course “{course_title}” by "
                "{author_name} on the learning portal. To complete the "
                "enrollment, confirm this address by following this link:\n"
                "{confirm_url}\n\n"
                "Please confirm within {ttl}, otherwise the enrollment will be "
                "cancelled and you will have to enroll again.\n\n"
                "If you already have a login with this author, you do not need "
                "to confirm: sign in and join the course from the enrollment "
                "page.\n\n"
                "Sign in to the author's portal: {author_login_url}\n"
                "Shared sign-in page: {shared_login_url}\n\n"
                "If you did not enroll in this course, simply ignore this "
                "email.\n"
            ),
        ),
    },
}


class RenderedEmail(NamedTuple):
    """A rendered subject/body pair, ready for the send-service."""

    subject: str
    body: str


def render_email(
    message_id: str,
    /,
    *,
    language: str | None = None,
    **context: str,
) -> RenderedEmail:
    """Render an email template by message-id, filling its placeholders.

    Args:
        message_id: One of the supported template keys in ``_TEMPLATES``.
        language: Language code (``eng`` for English, otherwise Ukrainian).
        **context: Placeholder values substituted into subject and body.

    Returns:
        The rendered ``(subject, body)``.

    Raises:
        KeyError: The message-id is unknown, or a body placeholder was not
            supplied. Both are programmer errors — the caller passes a fixed
            literal message-id and the placeholder set is part of the template
            contract — so failing loudly is correct.
    """
    lang_key = "eng" if language == "eng" else "ukr"
    template = _TEMPLATES[lang_key][message_id]
    return RenderedEmail(
        subject=template.subject.format_map(context),
        body=template.body.format_map(context),
    )
