"""Paragraph positional-anchor bridge for text/web Pass 2b (Phase 3.3a).

The text/web mapping LLM (Pass 2a) emits char offsets; this module turns a
segment's ``[start_pos, end_pos)`` char range into a ``[start_paragraph,
end_paragraph]`` ordinal range over the authored chunks. It is the
paragraph-axis sibling of ``presentation.chars_per_slide_cumsum`` — a
deliberately SEPARATE bridge (the word-axis ``chars_per_word_cumsum`` is a
different concern, DD-2.4-E).

Only PARAGRAPH (text) / WEB_CONTENT (web) chunks carry a paragraph ordinal;
HEADING chunks are skipped (they do not advance the counter). The cumsum is
built over the EMITTED (non-empty-text) chunks so its offsets line up with
``SourceDocument.assemble_text`` (which joins ``c.text for c in chunks if
c.text`` with the ``"\\n\\n"`` separator).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence

from course_supporter.models.source import ChunkType, ContentChunk

# Non-AUDIO assemble_text separator ("\n\n"), owned by each non-last chunk.
_SEPARATOR_LEN = 2


def chars_per_paragraph_cumsum(emitted_chunks: Sequence[ContentChunk]) -> list[int]:
    """Char-offset cumsum over the EMITTED chunks (Phase 3.3a).

    ``cumsum[i]`` is the char offset where emitted chunk ``i`` begins;
    ``cumsum[len]`` equals ``len(assemble_text(doc))``. Parallel to
    ``presentation.chars_per_slide_cumsum`` (same ``"\\n\\n"``-separator
    mechanics). Empty input returns ``[0]``.

    >>> chars_per_paragraph_cumsum([])
    [0]
    """
    cumsum = [0]
    last_idx = len(emitted_chunks) - 1
    for i, chunk in enumerate(emitted_chunks):
        separator_len = 0 if i == last_idx else _SEPARATOR_LEN
        cumsum.append(cumsum[-1] + len(chunk.text) + separator_len)
    return cumsum


def compute_paragraph_anchors(
    emitted_chunks: Sequence[ContentChunk],
    paragraph_types: frozenset[ChunkType],
    start_pos: int,
    end_pos: int,
) -> tuple[int | None, int | None]:
    """Resolve a ``[start_pos, end_pos)`` char range to a paragraph range.

    Returns ``(start_paragraph, end_paragraph)`` as 0-indexed inclusive
    ordinals counted over ``paragraph_types`` chunks only (headings
    skipped). Boundary that lands in a non-paragraph (HEADING) chunk falls
    back to the nearest paragraph chunk WITHIN the segment's chunk span —
    forward for the start, backward for the end. A segment that spans no
    paragraph chunk at all yields ``(None, None)``.

    Args:
        emitted_chunks: Non-empty-text chunks, in ``assemble_text`` order.
        paragraph_types: Chunk types that count as paragraphs
            (``{PARAGRAPH}`` for text, ``{WEB_CONTENT}`` for web).
        start_pos: Inclusive start char offset.
        end_pos: Exclusive end char offset (``> start_pos``).
    """
    n = len(emitted_chunks)
    if n == 0:
        return (None, None)

    cumsum = chars_per_paragraph_cumsum(emitted_chunks)

    # Paragraph ordinal per chunk index (None for non-paragraph chunks).
    para_ordinal: list[int | None] = [None] * n
    counter = 0
    for i, chunk in enumerate(emitted_chunks):
        if chunk.chunk_type in paragraph_types:
            para_ordinal[i] = counter
            counter += 1

    def chunk_index_for(offset: int) -> int:
        # cumsum[idx] <= offset < cumsum[idx + 1]; clamp to a valid chunk.
        idx = bisect_right(cumsum, offset) - 1
        return max(0, min(idx, n - 1))

    start_chunk = chunk_index_for(start_pos)
    end_chunk = chunk_index_for(end_pos - 1)

    # Start: nearest paragraph chunk scanning FORWARD within the span.
    start_paragraph: int | None = None
    for i in range(start_chunk, end_chunk + 1):
        if para_ordinal[i] is not None:
            start_paragraph = para_ordinal[i]
            break

    # End: nearest paragraph chunk scanning BACKWARD within the span.
    end_paragraph: int | None = None
    for i in range(end_chunk, start_chunk - 1, -1):
        if para_ordinal[i] is not None:
            end_paragraph = para_ordinal[i]
            break

    return (start_paragraph, end_paragraph)
