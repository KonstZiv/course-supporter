"""Stage 1 hard-reject for suspicious unicode characters (vision §KD14).

Four hostile-character classes per vision §KD14:

1. **Zero-width** (U+200B, U+200C, U+200D, U+FEFF, U+2060): used to
   bypass the regex pre-screen by inserting invisible separators
   inside trigger keywords. One use is legitimate and exempt in every
   mode: U+200D joining two emoji into one (``👨‍💻``) -- see
   :func:`is_emoji_joiner`. What a hit means is the caller's: the
   strict screen refuses it, the signal screen flags it and strips it
   before the regex pass (``security/text_screen.py``).

2. **Bidirectional overrides** (U+202A--U+202E, U+2066--U+2069):
   CVE-2021-42574 (Trojan Source). Reorders rendered text away
   from logical reading order; legitimate code or prose never
   needs explicit overrides.

3. **Tag characters** (U+E0000--U+E007F): the Unicode Tags block,
   used as a steganography channel. No legitimate use case in
   user-supplied content.

4. **C0/C1 controls** (Cc category) outside the ``\\t``/``\\n``/``\\r``
   whitelist: NUL, BEL, ESC, DEL, etc. -- not meaningful in
   user-supplied prose.

Detection is via ``unicodedata.category()`` for the Cc class
(controls) plus explicit codepoint sets for zero-width and bidi
overrides, plus a codepoint range for the Tag block.

Why explicit sets rather than blanket ``Cf`` (format) category:
``Cf`` would over-reject legitimate format characters spec does
NOT list -- SOFT HYPHEN U+00AD, LEFT-TO-RIGHT MARK U+200E,
RIGHT-TO-LEFT MARK U+200F. The vision §KD14 spec is conservative;
this module follows it character-by-character, not category.

A single forward pass over the input with early exit on the first
suspicious codepoint. The error detail names the codepoint, the
zero-based index, and the class -- enough for log correlation
with attack patterns without leaking content.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator
from typing import Final

from course_supporter.security.exceptions import (
    ErrorCategory,
    SecurityRejectedError,
)

# Whitelist for legitimate C0 controls in prose. Anything in the
# Cc category outside this set triggers SUSPICIOUS_UNICODE.
_ALLOWED_CONTROL_CHARS: frozenset[str] = frozenset("\n\r\t")

# Zero-width characters per vision §KD14. Cf-category but the spec
# enumerates these specific codepoints; blanket Cf rejection would
# over-reject SOFT HYPHEN, LRM, RLM, etc.
_ZERO_WIDTH_CHARS: frozenset[str] = frozenset(
    "​‌‍﻿⁠",
)

# Bidirectional override characters per vision §KD14.
_BIDI_OVERRIDE_CHARS: frozenset[str] = frozenset(
    "‪‫‬‭‮⁦⁧⁨⁩",
)

# Unicode Tags block. Steganography channel; hard reject.
_TAG_BLOCK_START = 0xE0000
_TAG_BLOCK_END = 0xE007F

ZERO_WIDTH: Final[str] = "zero_width"
"""The one class a signal screen flags instead of refusing."""

_ZWJ: Final[str] = "\u200d"
# What may sit between an emoji and the joiner that follows it: the emoji
# presentation selector (``🏳️‍🌈``) and the five skin-tone modifiers
# (``👩🏽‍💻``). Both belong to the emoji before them.
_EMOJI_TAIL: Final[frozenset[str]] = frozenset(
    "\ufe0f" + "".join(chr(cp) for cp in range(0x1F3FB, 0x1F400))
)


def _classify_suspicious(ch: str) -> str | None:
    """Return suspicious-class name for ``ch`` or ``None`` if benign.

    Class names land in the SecurityRejectedError detail so log
    aggregation can correlate attack patterns. The four currently
    returnable class names -- ``zero_width``, ``bidi_override``,
    ``tag_character``, ``control_character`` -- are stable within
    the v1 contract. Future extensions (e.g. archive filename
    context, additional codepoint families) may add new class names
    via versioning if the Stage 1 orchestrator needs richer detail;
    the four values above will not be renamed or removed without a
    concurrent log-pipeline update.
    """
    cp = ord(ch)

    if _TAG_BLOCK_START <= cp <= _TAG_BLOCK_END:
        return "tag_character"

    if ch in _ZERO_WIDTH_CHARS:
        return ZERO_WIDTH

    if ch in _BIDI_OVERRIDE_CHARS:
        return "bidi_override"

    if unicodedata.category(ch) == "Cc" and ch not in _ALLOWED_CONTROL_CHARS:
        return "control_character"

    return None


def _is_emoji(ch: str) -> bool:
    # Every emoji a person types sits in "Symbol, other" -- the pictographs,
    # ``♀`` and ``♂`` of the gendered sequences, ``❤``. A letter, a digit or
    # a space on either side means the joiner joins nothing pictorial.
    return unicodedata.category(ch) == "So"


def is_emoji_joiner(text: str, index: int) -> bool:
    """Is ``text[index]`` a U+200D joining two emoji into one?

    A composed emoji (``👨‍💻``, ``🏳️‍🌈``, ``👩🏽‍💻``) is written with a
    zero-width joiner between its parts. It is not an attack and not a
    signal: it is exempt in every mode, strict and signal alike (task 11,
    decision 3). The one function both modes ask, so the exemption cannot
    mean one thing for the author and another for the student.
    """
    if text[index] != _ZWJ or index + 1 >= len(text):
        return False
    before = index - 1
    while before >= 0 and text[before] in _EMOJI_TAIL:
        before -= 1
    return before >= 0 and _is_emoji(text[before]) and _is_emoji(text[index + 1])


def iter_suspicious(text: str) -> Iterator[tuple[int, str, str]]:
    """Every suspicious character of ``text`` as ``(index, class, char)``.

    The whole pass, not the first hit: the signal screen needs to know where
    each zero-width character sits, and whether anything harder sits behind
    them. A U+200D joining two emoji is not yielded (:func:`is_emoji_joiner`).
    """
    for index, ch in enumerate(text):
        kind = _classify_suspicious(ch)
        if kind is None:
            continue
        if kind == ZERO_WIDTH and is_emoji_joiner(text, index):
            continue
        yield index, kind, ch


def suspicious_unicode_error(kind: str, ch: str, index: int) -> SecurityRejectedError:
    """The refusal for one suspicious character, in the one detail format."""
    return SecurityRejectedError(
        ErrorCategory.SUSPICIOUS_UNICODE,
        f"{kind} U+{ord(ch):04X} at index {index}",
    )


def check_text_unicode_safety(text: str) -> None:
    """Raise on the first suspicious unicode character; otherwise return.

    Single forward pass with early exit; a U+200D joining two emoji is
    exempt (:func:`is_emoji_joiner`). The caller is responsible
    for passing NFKC-normalized text -- compatibility-encoded
    variants (e.g. full-width zero-width joiner) collapse to
    canonical forms that this check then catches.

    Args:
        text: NFKC-normalized text to validate.

    Raises:
        SecurityRejectedError: with category
            :attr:`ErrorCategory.SUSPICIOUS_UNICODE` on the first
            suspicious character. The detail string carries the
            offending codepoint formatted as ``U+XXXX``, its
            zero-based index in ``text``, and one of the four
            class names (``zero_width``, ``bidi_override``,
            ``tag_character``, ``control_character``).
    """
    for index, kind, ch in iter_suspicious(text):
        raise suspicious_unicode_error(kind, ch, index)
