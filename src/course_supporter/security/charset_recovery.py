"""Recover the text of a file that is not UTF-8, or refuse to guess.

The old answer to a non-UTF-8 text file was one of two silences. Homework
was refused on the strength of a label from the format detector, with a
reason that named safety; authored material was decoded *with* that label
and, because the label is never the true encoding, quietly became noise
that the Methodist then read and the author paid for. This module
replaces both with a decision that can be defended: either the bytes are
read back as text written in the language of the course, or they are
refused with a reason.

Why the detector's label cannot be the answer. Measured across
libmagic 5.47 (production) and 5.48 (development), which agree
character-for-character: no Cyrillic single-byte encoding is ever named.
Depending on the text, cp1251 comes back as ``iso-8859-1`` or as
``unknown-8bit``; KOI8-U and ISO-8859-5 come back as ``iso-8859-1`` too.
``iso-8859-1`` maps all 256 byte values, so decoding by that label never
raises and never warns -- it just produces mojibake. The label is kept
for the log and for nothing else.

Why three checks and not the library's own confidence. ``charset_normalizer``
distinguishes what the detector cannot, but its scores do not decide
anything. On the live cp1251 submission it returns ``cp1251``, ``kz1048``
and ``ptcp154`` with *identical* chaos and coherence, and those three
readings differ in exactly thirteen characters out of 2318 -- every one of
them where Ukrainian ``ї`` or ``є`` belongs, replaced by a Kazakh or Azeri
letter. Picking the top score would be picking a tie-break. Measured the
same way, a near miss one family over (cp866 read as cp1125) is likewise
a thirteen-character difference the scores rank *higher* than the truth.

So a candidate is accepted only when all three agree:

1. the library offers it at all;
2. its text contains no letters of the language's own script that the
   language's alphabet lacks (this is what separates the tie above);
3. it uses more of the language's stop words than any equally clean rival,
   and not zero.

Anything short of that is a refusal with a named reason. A refusal costs
the author or the student one clear message; a wrong acceptance costs a
review of text that is not what they wrote.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import charset_normalizer

from course_supporter.linguistics import (
    count_foreign_letters,
    has_language_data,
    stop_word_share,
)

# Below this many bytes a guess stops meaning anything. Calibrated by
# sweeping excerpts of the live cp1251 submission -- cut at three offsets
# (start, middle, end) over the grid 14, 24, 32, 48, 64, 96, 128, 192,
# 240, 320, 480, 640, 960, 1280, 2318 characters, each re-encoded strictly
# into every single-byte Cyrillic encoding that can hold it -- and asking
# the whole rule below for each one. Two results set this number:
#
# * At 24 and 32 characters the rule ACCEPTED a mac_cyrillic reading of
#   cp1251 bytes -- a wrong text, silently. Nothing shorter may be tried.
# * 192 characters is the shortest length at which every excerpt in the
#   grid came back both verified and character-for-character correct.
#
# 240 ships rather than 192: the grid is one document, and one step of
# margin costs only that a very short non-UTF-8 file is refused instead of
# recovered -- the cheap direction to be wrong in. Between the two, every
# failure was an honest refusal (``no_stop_words``, ``ambiguous``), never
# a wrong text.
#
# The name says bytes and the grid was cut in characters: for the
# single-byte encodings this guards, the two are the same number. For
# UTF-16 the effective floor is 120 characters instead of 240 -- still far
# above the zone where wrong acceptances were measured (32 and below).
MIN_RECOVERABLE_BYTES = 240

# Letters of the language's own script that its alphabet lacks, per
# thousand characters, still tolerated in an accepted reading.
#
# Ranking is what resolves a tie -- fewest foreign letters wins -- so this
# is not the discriminator; it is the floor that stops a field of uniformly
# wrong readings from being accepted just because one of them is least bad.
# Measured over the same grid: a correct reading scored 0.00 at every
# length and offset, while a wrong reading that actually differs in text
# scored between 2.08 and 20.83 at lengths at or above the threshold. One
# per thousand sits below the whole of that range and still leaves room for
# what the check must not call an error -- a foreign name or a quoted word
# inside otherwise correct prose (two of them in a 2318-character document
# pass; three do not).
MAX_FOREIGN_LETTERS_PER_1000 = 1.0


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Outcome of reading bytes as text.

    ``verified`` is the only field a caller should branch on. ``reason``
    is operator language for the log -- never a phrase for a person; the
    words a person reads live in the interface dictionaries, keyed by the
    refusal category.
    """

    text: str
    encoding: str
    verified: bool
    reason: str | None = None


def _refused(reason: str) -> RecoveryResult:
    return RecoveryResult(text="", encoding="", verified=False, reason=reason)


@dataclass(frozen=True, slots=True)
class _Candidate:
    encoding: str
    text: str
    foreign: int
    share: float
    order: int


def recover_text(raw: bytes, *, languages: Sequence[str]) -> RecoveryResult:
    """Read ``raw`` as text, verified against ``languages``.

    Args:
        raw: The file's bytes.
        languages: ISO 639-3 codes the text is expected to be written in
            -- the course language, plus the student's review language
            when it differs. Codes without letter and word data are
            ignored; if none is left, nothing can be verified and the
            answer is a refusal rather than a guess.

    Returns:
        A :class:`RecoveryResult`. On success ``text`` is the decoded
        body and ``encoding`` names how it was read; on refusal both are
        empty and ``reason`` says which check failed.
    """
    # Valid UTF-8 is its own proof: the encoding has enough structure that
    # arbitrary bytes do not decode by accident, so no further check is
    # owed. This also closes the hole the old label gate left open -- a
    # file with 64 KiB of Latin text ahead of one Cyrillic byte was
    # labelled ``us-ascii``, passed, and then raised UnicodeDecodeError
    # from the decode nobody guarded.
    try:
        return RecoveryResult(text=raw.decode("utf-8"), encoding="utf-8", verified=True)
    except UnicodeDecodeError:
        pass
    # ``utf-8-sig`` is deliberately not a second attempt: a byte-order
    # mark is itself valid UTF-8, so anything it could rescue has already
    # been decoded above, and the mark it would strip is removed further
    # down the pipeline anyway (DD-SP-E).

    if len(raw) < MIN_RECOVERABLE_BYTES:
        return _refused("too_short")

    verifiable = [code for code in languages if has_language_data(code)]
    if not verifiable:
        return _refused("no_verification_for_language")

    candidates = _candidates(raw, verifiable)
    if not candidates:
        return _refused("no_candidates")

    # Fewest foreign letters first: that is the check with the resolving
    # power. Stop words break ties among equally clean readings; the
    # library's own order is the last resort, and it only ever decides
    # between readings that are indistinguishable by both signals.
    winner = min(candidates, key=lambda c: (c.foreign, -c.share, c.order))

    if winner.foreign * 1000 / len(winner.text) > MAX_FOREIGN_LETTERS_PER_1000:
        return _refused("foreign_letters")
    if winner.share <= 0:
        return _refused("no_stop_words")
    rivals = [c for c in candidates if c is not winner and c.foreign == winner.foreign]
    if any(rival.share >= winner.share for rival in rivals):
        return _refused("ambiguous")

    return RecoveryResult(text=winner.text, encoding=winner.encoding, verified=True)


def _candidates(raw: bytes, languages: Sequence[str]) -> list[_Candidate]:
    """Score every reading the library offers, one per distinct text.

    Encodings that produce the same characters are the same answer wearing
    different names -- ``cp1251`` and ``kz1048`` on a text with no ``ї`` in
    it, for instance. Merging them keeps a naming difference from being
    read as a disagreement about the text.
    """
    out: list[_Candidate] = []
    seen: set[str] = set()
    for order, match in enumerate(charset_normalizer.from_bytes(raw)):
        text = str(match)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(
            _Candidate(
                encoding=match.encoding,
                text=text,
                foreign=count_foreign_letters(text, languages),
                share=stop_word_share(text, languages),
                order=order,
            )
        )
    return out
