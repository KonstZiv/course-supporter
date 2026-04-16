"""Helpers for Stage 5/6 of the Content Ingestion pipeline.

Shared by text and web sources — the Stage-5 LLM output and Stage-6
paragraph split are identical for both once the raw content is
converted to Markdown. Video / audio / presentation processors will
bring their own stages in later PRs.

Two public entry points:

- :func:`resolve_macro_sections` — turn LLM snippet output into drafts
  with absolute char offsets; enforces full-coverage invariants.
- :func:`split_into_segments` — paragraph-split a macro's content slice
  with a character cap to keep downstream context windows manageable.
"""

from __future__ import annotations

from course_supporter.models.macro_segment import (
    MacroSectionCandidate,
    MacroSectionDraft,
    SegmentDraft,
)

# Cap for a single segment. Paragraphs longer than this are further
# split on the nearest whitespace boundary. Tuned for Architect context
# budgets — may be revisited when the generation layer consumes segments.
DEFAULT_SEGMENT_MAX_CHARS = 2000


class SnippetResolutionError(ValueError):
    """A section snippet could not be resolved in ``processed_content``."""


def resolve_macro_sections(
    *,
    candidates: list[MacroSectionCandidate],
    processed_content: str,
) -> list[MacroSectionDraft]:
    """Resolve LLM-provided snippets into absolute char-offset drafts.

    Rules (confirmed design decisions for PR #4c):

    - Snippets are searched **monotonically forward** from the previous
      section's end position — guarantees correct ordering and disambiguates
      snippets that happen to appear multiple times in the document.
    - The first section's ``start_pos`` is auto-extended to 0 so the
      document is covered from the very beginning.
    - The last section's ``end_pos`` is ``len(processed_content)`` —
      full coverage to the end.
    - Any gap between ``section[i].end_pos`` and ``section[i+1].start_pos``
      is absorbed into ``section[i]`` (its ``end_pos`` is extended).
    - A snippet that is not found anywhere after the previous section's
      end raises :class:`SnippetResolutionError` — the caller should
      mark the material as failed and allow the user to retry. No
      fuzzy matching is attempted in this MVP.

    Args:
        candidates: LLM output, ordered by document position.
        processed_content: Source text in which to resolve snippets.

    Returns:
        One :class:`MacroSectionDraft` per input candidate: ordered,
        contiguous, and covering the full document.

    Raises:
        SnippetResolutionError: On the first snippet that cannot be
            located after the previous section's end position.
        ValueError: If ``candidates`` is empty.
    """
    if not candidates:
        msg = "LLM returned no macro sections"
        raise ValueError(msg)

    total_len = len(processed_content)

    raw_starts: list[int] = []
    search_from = 0
    for idx, candidate in enumerate(candidates):
        found = processed_content.find(candidate.start_snippet, search_from)
        if found == -1:
            msg = (
                f"Section {idx} ('{candidate.title}'): start_snippet not "
                f"found in processed_content at or after offset "
                f"{search_from}. Snippet was: {candidate.start_snippet!r}"
            )
            raise SnippetResolutionError(msg)
        raw_starts.append(found)
        search_from = found + len(candidate.start_snippet)

    drafts: list[MacroSectionDraft] = []
    for idx, candidate in enumerate(candidates):
        start_pos = 0 if idx == 0 else raw_starts[idx]
        end_pos = raw_starts[idx + 1] if idx + 1 < len(raw_starts) else total_len
        drafts.append(
            MacroSectionDraft(
                order=idx,
                title=candidate.title,
                start_pos=start_pos,
                end_pos=end_pos,
            )
        )
    return drafts


def split_into_segments(
    *,
    macro_content: str,
    macro_start_pos: int,
    max_chars: int = DEFAULT_SEGMENT_MAX_CHARS,
) -> list[SegmentDraft]:
    """Split a macro's content slice into paragraph-based segments.

    Splits on blank lines (``\\n\\n`` — the Markdown paragraph separator
    after normalisation). Paragraphs exceeding ``max_chars`` are further
    split on the nearest whitespace boundary so no segment grows
    unbounded.

    Absolute positions (``start_pos`` / ``end_pos``) are returned — i.e.
    offsets into the full ``processed_content``, not relative to
    ``macro_start_pos``. The sum of segment ranges equals the macro
    range; no gaps.

    Args:
        macro_content: The slice ``processed_content[start:end]`` of
            the parent macro section. Must be non-empty.
        macro_start_pos: Absolute offset of ``macro_content[0]`` in the
            full source. Used to produce absolute segment positions.
        max_chars: Soft cap for a single segment.

    Returns:
        Ordered list of :class:`SegmentDraft` covering ``macro_content``
        without gaps.
    """
    if not macro_content:
        msg = "macro_content must be non-empty"
        raise ValueError(msg)

    paragraph_ranges = _paragraph_ranges(macro_content)
    capped = _enforce_char_cap(macro_content, paragraph_ranges, max_chars)

    segments: list[SegmentDraft] = []
    for idx, (rel_start, rel_end) in enumerate(capped):
        segments.append(
            SegmentDraft(
                order=idx,
                start_pos=macro_start_pos + rel_start,
                end_pos=macro_start_pos + rel_end,
                content=macro_content[rel_start:rel_end],
            )
        )
    return segments


# ── Internal helpers ────────────────────────────────────────────────


def _paragraph_ranges(text: str) -> list[tuple[int, int]]:
    """Split ``text`` into contiguous paragraph ranges ``[start, end)``.

    Separator is one or more blank lines. The separator itself is
    included as trailing whitespace of the **preceding** paragraph so
    consecutive ranges are contiguous and the sum covers ``text`` fully.
    A single-paragraph (no blank line) input yields one range covering
    the full text.
    """
    n = len(text)
    if n == 0:
        return []

    ranges: list[tuple[int, int]] = []
    pos = 0
    while pos < n:
        sep = text.find("\n\n", pos)
        if sep == -1:
            ranges.append((pos, n))
            break
        # Extend separator through any run of additional newlines so the
        # next paragraph starts on non-newline content.
        sep_end = sep + 2
        while sep_end < n and text[sep_end] == "\n":
            sep_end += 1
        ranges.append((pos, sep_end))
        pos = sep_end
    return ranges


def _enforce_char_cap(
    text: str,
    ranges: list[tuple[int, int]],
    max_chars: int,
) -> list[tuple[int, int]]:
    """Sub-split ranges longer than ``max_chars`` on whitespace boundaries.

    Output preserves absolute offsets and full coverage of the input
    ranges. Guarantees every output range is ``≤ max_chars`` unless the
    whole range consists of non-whitespace characters with no possible
    cut (extreme edge case — we hard-cut at ``max_chars``).
    """
    out: list[tuple[int, int]] = []
    for start, end in ranges:
        pos = start
        while end - pos > max_chars:
            cut = _cut_on_whitespace(text, pos, pos + max_chars)
            if cut <= pos:
                cut = pos + max_chars
            out.append((pos, cut))
            pos = cut
        if pos < end:
            out.append((pos, end))
    return out


def _cut_on_whitespace(text: str, start: int, ideal: int) -> int:
    """Return the position of the last whitespace character in
    ``text[start:ideal]`` plus one, or ``start`` if no whitespace exists.

    Callers treat a return value of ``<= start`` as "no safe cut" and
    fall back to a hard cut at ``ideal``.
    """
    for j in range(ideal - 1, start - 1, -1):
        if text[j] in (" ", "\t", "\n", "\r"):
            return j + 1
    return start
