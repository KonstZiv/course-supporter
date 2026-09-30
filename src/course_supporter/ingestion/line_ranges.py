"""Numbered lines for the text/web Pass 2a mapping prompt.

The mapping model names segment boundaries by line number; the server turns
line ranges into char offsets. A model cannot count characters — it guesses
them (the first answer failed the cover check on 148 of 149 files of a real
course), while a line number is written in front of every line it reads.
Architectural principle: never ask the LLM for quantities we can compute
deterministically.

Numbering, over the exact string of :meth:`SourceDocument.assemble_text`:

* Only non-blank lines get a number. Blank lines (the ``"\\n\\n"`` chunk
  separator, spacing inside a chunk) are shown unnumbered, so a boundary
  never lands on emptiness and the prompt spends nothing on dead numbers.
* A line longer than :data:`MAX_LINE_CHARS` is wrapped into several numbered
  lines — at a sentence end if possible, else at a space, else hard. A file
  whose paragraphs are single long lines (web pages, plain text) can then
  still be split inside a paragraph.
* Numbered line ``k`` owns the chars from its first char up to the first
  char of line ``k + 1``: its trailing newline and any blank lines after it.
  Line 1 also owns any blank lines before it; the last line ends at
  ``len(text)``. The lines therefore partition ``[0, len(text))`` without
  gaps or overlaps, and any contiguous cover by line ranges converts to a
  contiguous cover by char offsets — exactly, in both directions.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass

# Longest line shown to the model as one numbered line. Well under the
# prompt's 300-char minimum segment, so wrapping never forces a segment to
# be coarser than the rules ask; well over a typical sentence.
MAX_LINE_CHARS = 250

# A wrapped piece is not cut shorter than this when a better cut exists
# further on (avoids a stub line of a few words).
_MIN_PIECE_CHARS = 80

_SENTENCE_END = re.compile(r"[.!?…:;](?:[\"'»”)\]]*)\s+")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class NumberedLine:
    """One numbered line and the char span of the reference text it owns.

    Attributes:
        number: 1-based line number shown to the model.
        start: Inclusive char offset where the owned span begins.
        end: Exclusive char offset where the owned span ends
            (the next line's ``start``, or ``len(text)`` for the last).
        display: The line as shown to the model — no newline, no
            trailing whitespace.
    """

    number: int
    start: int
    end: int
    display: str


def number_lines(text: str) -> list[NumberedLine]:
    """Split ``text`` into numbered lines that partition it (module docstring).

    Returns an empty list for blank ``text``.
    """
    # (display_start, display_end) per numbered line, in text order.
    pieces: list[tuple[int, int]] = []
    offset = 0
    for raw_line in text.split("\n"):
        stripped = raw_line.rstrip()
        if stripped.strip():
            pieces.extend(_wrap(text, offset, offset + len(stripped)))
        offset += len(raw_line) + 1

    lines: list[NumberedLine] = []
    for i, (display_start, display_end) in enumerate(pieces):
        start = 0 if i == 0 else display_start
        end = pieces[i + 1][0] if i + 1 < len(pieces) else len(text)
        lines.append(
            NumberedLine(
                number=i + 1,
                start=start,
                end=end,
                display=text[display_start:display_end],
            )
        )
    return lines


def _wrap(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Cut ``text[start:end]`` (one non-blank line) into display pieces."""
    pieces: list[tuple[int, int]] = []
    while end - start > MAX_LINE_CHARS:
        window = text[start : start + MAX_LINE_CHARS + 1]
        cut = _last_break(_SENTENCE_END, window) or _last_break(_WHITESPACE, window)
        if cut is None:
            cut = MAX_LINE_CHARS
        piece_end = start + len(window[:cut].rstrip())
        if piece_end > start:
            pieces.append((start, piece_end))
        start += cut
        while text[start].isspace():
            start += 1
    pieces.append((start, end))
    return pieces


def _last_break(pattern: re.Pattern[str], window: str) -> int | None:
    """Offset just after the last ``pattern`` match past the minimum piece."""
    best: int | None = None
    for match in pattern.finditer(window):
        if match.end() >= len(window):
            break
        if match.start() >= _MIN_PIECE_CHARS:
            best = match.end()
    return best


def render_numbered(text: str, lines: list[NumberedLine]) -> str:
    """The text as the model reads it: ``"<number>| <line>"`` per line.

    Blank lines between numbered lines are kept (unnumbered) so the
    paragraph structure stays visible; a wrapped long line simply continues
    on the next numbered line.
    """
    parts: list[str] = []
    for line in lines[:-1]:
        owned = text[line.start : line.end]
        breaks = max(1, owned.count("\n"))
        parts.append(f"{line.number}| {line.display}" + "\n" * breaks)
    if lines:
        parts.append(f"{lines[-1].number}| {lines[-1].display}")
    return "".join(parts)


def line_range_to_span(
    lines: list[NumberedLine], start_line: int, end_line: int
) -> tuple[int, int]:
    """Inclusive 1-based line range → ``(start_pos, end_pos)`` char span.

    The caller has already checked ``1 <= start_line <= end_line <= N``.
    """
    return lines[start_line - 1].start, lines[end_line - 1].end


def span_to_line_range(
    lines: list[NumberedLine], start_pos: int, end_pos: int
) -> tuple[int, int]:
    """Inverse of :func:`line_range_to_span` for spans on line boundaries."""
    starts = [line.start for line in lines]
    start_line = bisect_right(starts, start_pos)
    end_line = bisect_right(starts, end_pos - 1)
    return start_line, end_line
