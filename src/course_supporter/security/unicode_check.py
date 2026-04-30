"""Stage 1 hard-reject for suspicious unicode characters (vision §KD14).

Four hostile-character classes per vision §KD14:

1. **Zero-width** (U+200B, U+200C, U+200D, U+FEFF, U+2060): no
   legitimate use case in submission text. Used to bypass regex
   pre-screen by inserting invisible separators inside trigger
   keywords.

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
        return "zero_width"

    if ch in _BIDI_OVERRIDE_CHARS:
        return "bidi_override"

    if unicodedata.category(ch) == "Cc" and ch not in _ALLOWED_CONTROL_CHARS:
        return "control_character"

    return None


def check_text_unicode_safety(text: str) -> None:
    """Raise on the first suspicious unicode character; otherwise return.

    Single forward pass with early exit. The caller is responsible
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
    for index, ch in enumerate(text):
        kind = _classify_suspicious(ch)
        if kind is not None:
            cp_hex = f"U+{ord(ch):04X}"
            raise SecurityRejectedError(
                ErrorCategory.SUSPICIOUS_UNICODE,
                f"{kind} {cp_hex} at index {index}",
            )
