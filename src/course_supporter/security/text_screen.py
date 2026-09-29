"""The one text screen, for every text on both roads (task 11, decision 1).

Purpose:
    Every text that reaches a model through the platform is screened here --
    a file, an archive member, the text pulled out of a document, a file's
    name, a student's comment, an author's material -- by one function in one
    of two modes (:data:`~course_supporter.security.policies.ScreenMode`):

    * ``strict`` refuses on the first hit. The author's material, exactly as
      it always was, with one correction: a U+200D joining two emoji is not a
      hit in any mode (``unicode_check.is_emoji_joiner``).
    * ``signal`` refuses only what no legitimate text carries -- direction
      overrides, tag characters, control characters -- and turns the rest
      into :class:`~course_supporter.security.schemas.ScreenFlag` records:
      zero-width characters, and the phrases of the regex layer. A course
      about agents is full of "ignore previous instructions" in test files
      and agent prompts; refusing it at the door refuses the work, while a
      paraphrase walks past the regex anyway. The flags go to Stage 2 as a
      hint, and only Stage 2 refuses on an actual attack (decision 2).

Interface:
    :func:`screen_text` -- bytes or text in, :class:`ScreenedText` out, or
    :class:`~course_supporter.security.exceptions.SecurityRejectedError`.
    :func:`order_flags` and :func:`render_flags_for_stage2` are the two
    shapes a submission's flags take afterwards: the trace (capped at
    :data:`FLAG_TRAIL_LIMIT`) and the hint Stage 2 reads (capped at
    :data:`FLAG_STAGE2_LIMIT`).

Pipeline order (cheap-fail-fast, the order Stage 1 has always used):
    1. recovery of the encoding, for bytes (``charset_recovery``);
    2. NFKC for security, one leading BOM dropped (DD-SP-E);
    3. unicode: hard classes refuse in both modes; zero-width refuses in
       ``strict`` and is flagged in ``signal``;
    4. zero-width characters are cut out, then every regex match is found --
       an invisible space inside a trigger phrase does not hide it;
    5. NFC of the text as read, for storage and for the model.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import structlog

from course_supporter.security.charset_recovery import recover_text
from course_supporter.security.exceptions import ErrorCategory, SecurityRejectedError
from course_supporter.security.file_type import detect_charset
from course_supporter.security.normalization import nfc_for_storage, nfkc_for_security
from course_supporter.security.policies import ScreenMode
from course_supporter.security.regex_patterns import find_all, match_text
from course_supporter.security.schemas import ScreenFlag
from course_supporter.security.unicode_check import (
    ZERO_WIDTH,
    iter_suspicious,
    suspicious_unicode_error,
)

logger = structlog.get_logger(__name__)

FLAG_TRAIL_LIMIT: Final[int] = 200
"""Flags kept in a submission's trace (``safety_result.flags``)."""

FLAG_STAGE2_LIMIT: Final[int] = 50
"""Flags shown to Stage 2; the rest is one ``… N more`` line."""


@dataclass(frozen=True, slots=True)
class ScreenedText:
    """A text that passed the screen.

    Attributes:
        text: The text as read, in NFC -- what is stored and what a model
            reads. Never the security view (NFKC, zero-width cut out): that
            one exists only to be searched.
        flags: What the signal screen noticed, in text order. Always empty
            in ``strict`` mode, where a hit refuses instead.
        encoding: How bytes were read -- ``"utf-8"`` or the recovered
            encoding; ``None`` when the input was already text.
    """

    text: str
    flags: tuple[ScreenFlag, ...]
    encoding: str | None


def screen_text(
    content: bytes | str,
    *,
    name: str,
    mode: ScreenMode,
    languages: Sequence[str] = (),
    where: Literal["content", "name"] = "content",
    context: Literal["authored", "homework"] | None = None,
) -> ScreenedText:
    """Screen one text; refuse it, or return it with what was noticed.

    Args:
        content: Bytes of a file (the encoding is recovered first) or text
            already decoded -- a document's extracted text, a file's name, a
            comment.
        name: What the text is -- a path, a filename, ``student_note``. The
            flag's ``source`` and the refusal's detail name it.
        mode: ``strict`` or ``signal`` (module docstring).
        languages: ISO 639-3 codes the text is expected to be written in;
            used only to verify a recovered encoding, and fail-closed (see
            ``charset_recovery.recover_text``). Ignored for text input.
        where: ``name`` when ``content`` is a file's name: its flags carry no
            line.
        context: Set by a caller that screens text which is not a file -- a
            test's fields, a comment: a refusal is then logged as
            ``run_stage1`` logs one (``stage1.rejected``). ``run_stage1``
            itself leaves it unset and logs at its own boundary, once.

    Raises:
        SecurityRejectedError: ``CHARSET_VIOLATION`` (bytes that cannot be
            read in ``languages``), ``SUSPICIOUS_UNICODE`` or, in ``strict``
            mode only, ``PROMPT_INJECTION``.
    """
    try:
        return _screen(content, name=name, mode=mode, languages=languages, where=where)
    except SecurityRejectedError as exc:
        if context is not None:
            logger.warning(
                "stage1.rejected",
                category=exc.category.value,
                filename=name,
                context=context,
                detail=exc.detail,
            )
        raise


def _screen(
    content: bytes | str,
    *,
    name: str,
    mode: ScreenMode,
    languages: Sequence[str],
    where: Literal["content", "name"],
) -> ScreenedText:
    if isinstance(content, bytes):
        text, encoding = _recover(content, name=name, languages=languages)
    else:
        text, encoding = content, None

    security_view = nfkc_for_security(text)
    if security_view.startswith("﻿"):
        # DD-SP-E: one leading BOM is an encoding mark (a Google Docs export
        # prepends it), not an obfuscation attempt.
        security_view = security_view[1:]

    flags: list[ScreenFlag] = []
    zero_width_at: list[int] = []
    for index, kind, ch in iter_suspicious(security_view):
        if kind != ZERO_WIDTH or mode == "strict":
            raise suspicious_unicode_error(kind, ch, index)
        zero_width_at.append(index)

    searchable = _cut(security_view, zero_width_at) if zero_width_at else security_view
    if mode == "strict":
        matched = match_text(searchable)
        if matched is not None:
            raise SecurityRejectedError(
                ErrorCategory.PROMPT_INJECTION,
                f"matched pattern category {matched.category!r} in {name!r}",
            )
    hits = find_all(searchable)

    for index in zero_width_at:
        flags.append(_flag(name, where, security_view, index, ZERO_WIDTH))
    # Cutting characters out changes no line break, so a match's line in the
    # searchable view is its line in the text.
    for pattern, start in hits:
        flags.append(_flag(name, where, searchable, start, pattern.category))

    ordered = order_flags(flags)
    for flag in ordered:
        logger.info(
            "text_screen.flagged",
            source=flag.source,
            where=flag.where,
            line=flag.line,
            category=flag.category,
        )
    return ScreenedText(text=nfc_for_storage(text), flags=ordered, encoding=encoding)


def order_flags(flags: Iterable[ScreenFlag]) -> tuple[ScreenFlag, ...]:
    """One flag per (source, line, category), by source and then by line.

    A name's flags (no line) come before the content's of the same source.
    Stable and deterministic, so the trace and the hint read the same way on
    every run of the same submission.
    """
    unique = {(f.source, f.where, f.line, f.category): f for f in flags}
    return tuple(
        sorted(
            unique.values(),
            key=lambda f: (f.source, f.where != "name", f.line or 0, f.category),
        )
    )


def trail_flags(flags: Iterable[ScreenFlag]) -> tuple[list[ScreenFlag], int]:
    """The flags a submission's trace keeps, and how many the cap left out."""
    ordered = order_flags(flags)
    return list(ordered[:FLAG_TRAIL_LIMIT]), max(0, len(ordered) - FLAG_TRAIL_LIMIT)


def render_flags_for_stage2(flags: Iterable[ScreenFlag]) -> str:
    """The hint Stage 2 reads: one line per flag, then ``… N more``.

    Location and category only -- the fragment is never quoted, so the hint
    cannot itself carry what it points at. Empty when there is nothing.
    """
    ordered = order_flags(flags)
    lines = [_describe(flag) for flag in ordered[:FLAG_STAGE2_LIMIT]]
    rest = len(ordered) - FLAG_STAGE2_LIMIT
    if rest > 0:
        lines.append(f"… {rest} more")
    return "\n".join(lines)


def _describe(flag: ScreenFlag) -> str:
    place = "file name" if flag.where == "name" else f"line {flag.line}"
    return f"- {flag.source} · {place} · {flag.category}"


def _recover(content: bytes, *, name: str, languages: Sequence[str]) -> tuple[str, str]:
    recovered = recover_text(content, languages=languages)
    if not recovered.verified:
        # Operator language: the reason code is what the interface turns into
        # a phrase for a person, so this string exists for the log and support.
        raise SecurityRejectedError(
            ErrorCategory.CHARSET_VIOLATION,
            (
                f"{name!r} is not UTF-8 and its encoding could not be "
                f"established ({recovered.reason}); detector label "
                f"{detect_charset(content)!r}"
            ),
        )
    if recovered.encoding != "utf-8":
        logger.info(
            "stage1_charset_recovered",
            filename=name,
            encoding=recovered.encoding,
            detector_label=detect_charset(content),
            byte_length=len(content),
            char_length=len(recovered.text),
            languages=list(languages),
        )
    return recovered.text, recovered.encoding


def _cut(text: str, indices: list[int]) -> str:
    drop = set(indices)
    return "".join(ch for i, ch in enumerate(text) if i not in drop)


def _flag(
    source: str,
    where: Literal["content", "name"],
    text: str,
    index: int,
    category: str,
) -> ScreenFlag:
    line = None if where == "name" else text.count("\n", 0, index) + 1
    return ScreenFlag(source=source, where=where, line=line, category=category)
