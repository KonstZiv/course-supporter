"""The code checks a student's own words meet before any of them is stored.

Purpose:
    Two texts a student writes reach paid prompts as they were written: the
    comment to a submission (``student_note``, hotfix 6) and a reply to a
    remark of a review (task 12a, K3). Both are brought to one form and pass
    the one text screen in the mode every text a student writes meets (task
    11, decision 1); the two doors keep their own limits and their own refusal
    codes, and share these two steps, so the two cannot come to read one text
    in two ways.

Interface:
    :func:`normalized_student_text` — line breaks to ``\\n``, edges trimmed:
    the text as it is counted and stored.
    :func:`screened_student_text` — the text through
    :func:`~course_supporter.security.text_screen.screen_text` in
    ``HOMEWORK_POLICY.text_screen_mode`` (``signal``): returned in NFC, or
    refused for the characters no legitimate text carries.

    The third guard of such a text is not here because it is not a check of
    the text: the slot lock of the prompt loader
    (:func:`~course_supporter.llm.prompt_loader_md.lock_slots`) neutralises,
    in every rendered prompt, any tag that would open or close one of that
    prompt's data slots — whatever value is rendered into it.
"""

from __future__ import annotations

from course_supporter.security.policies import HOMEWORK_POLICY
from course_supporter.security.text_screen import screen_text


def normalized_student_text(text: str) -> str:
    """The text as it is counted and stored: ``\\n`` line breaks, no edges.

    A browser sends every line break of a multipart field as ``\\r\\n``, so
    counting them as they arrive would give one text two lengths — one through
    a form, another through a JSON body. Then the edges are trimmed: what is
    left is what is counted, in code points, and a text of nothing but
    whitespace is no text.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def screened_student_text(text: str, *, name: str) -> str:
    """The text through the one text screen; what is stored, in NFC.

    ``name`` names the text in a refusal's detail and in the log
    (``student_note``, ``student_reply``). No languages: bytes encoded from a
    ``str`` are UTF-8, which is read without one. The one exception is a lone
    surrogate a JSON body can carry: ``surrogatepass`` turns it into bytes
    that are not UTF-8, and the screen refuses them rather than the encoder
    failing with a 500. A phrase addressed to a model or a zero-width
    character is only noticed (a flag for the safety check), never refused
    here.

    Raises:
        SecurityRejectedError: ``SUSPICIOUS_UNICODE`` (direction overrides,
            tag or control characters) or ``CHARSET_VIOLATION``.
    """
    return screen_text(
        text.encode("utf-8", "surrogatepass"),
        name=name,
        mode=HOMEWORK_POLICY.text_screen_mode,
        context="homework",
    ).text
