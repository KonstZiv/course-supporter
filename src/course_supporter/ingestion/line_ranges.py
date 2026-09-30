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
  lines — at a sentence end if possible, else at ``:`` / ``;``, else at a
  space, else hard; a markdown table row only between cells. A file
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

# Shortest wrapped piece: a break that would leave fewer chars than this on
# the piece is ignored, and the search falls to the next break kind (avoids
# a stub line of a few words).
_MIN_PIECE_CHARS = 80

# Break kinds for prose, best first; each is used only when the window has
# no break of the kinds before it. A sentence end or clause end may be
# followed by closing quotes / brackets.
_SENTENCE_END = re.compile(r"[.!?…][\"'»”)\]]*\s+")
_CLAUSE_END = re.compile(r"[:;][\"'»”)\]]*\s+")
_WHITESPACE = re.compile(r"\s+")
_PROSE_BREAKS = (_SENTENCE_END, _CLAUSE_END, _WHITESPACE)

# A markdown table row is cut only right after an unescaped cell bar, so
# no cell is split and every piece holds whole cells.
_CELL_BAR = re.compile(r"(?<!\\)\|\s*")


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
    """Cut ``text[start:end]`` (one non-blank line) into display pieces.

    Prose breaks at the last sentence end in the window, else the last
    ``:`` / ``;``, else the last space, else hard at the limit. A markdown
    table row (first non-space char ``|``) breaks only after a cell bar —
    the last one in the window, or the first one past it when a single cell
    is longer than the window — and stays whole when no bar is left.
    """
    pieces: list[tuple[int, int]] = []
    is_table_row = text[start:end].lstrip().startswith("|")
    while end - start > MAX_LINE_CHARS:
        window = text[start : start + MAX_LINE_CHARS + 1]
        if is_table_row:
            cut = _last_break(_CELL_BAR, window) or _next_cell_bar(text, start, end)
            if cut is None:
                break
        else:
            cut = _prose_cut(window)
        piece_end = start + len(text[start : start + cut].rstrip())
        if piece_end > start:
            pieces.append((start, piece_end))
        start += cut
        while text[start].isspace():
            start += 1
    pieces.append((start, end))
    return pieces


def _prose_cut(window: str) -> int:
    """Cut at the best break kind the window has, else hard at the limit."""
    for pattern in _PROSE_BREAKS:
        cut = _last_break(pattern, window)
        if cut is not None:
            return cut
    return MAX_LINE_CHARS


def _next_cell_bar(text: str, start: int, end: int) -> int | None:
    """Cut (relative to ``start``) after the first cell bar past the window.

    ``None`` when the only bars left close the row: the rest stays whole.
    """
    for match in _CELL_BAR.finditer(text, start + MAX_LINE_CHARS, end):
        if match.end() < end:
            return match.end() - start
    return None


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
