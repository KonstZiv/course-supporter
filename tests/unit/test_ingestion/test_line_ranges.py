"""Text/web Pass 2a by line ranges: numbering, cover check, processors, prompt.

The mapping model names segment boundaries by line number; the server
converts line ranges into the char offsets Pass 2b slices by. These tests
pin the conversion (exact and reversible), the line-cover check and its
retry feedback, both processors end to end against a stub router, and the
rendered prompt.
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from course_supporter.ingestion.line_ranges import (
    MAX_LINE_CHARS,
    NumberedLine,
    line_range_to_span,
    number_lines,
    render_numbered,
    span_to_line_range,
)
from course_supporter.ingestion.schemas import (
    DocumentSummaryDraft,
    TextMappingResponse,
)
from course_supporter.ingestion.text import TextProcessor
from course_supporter.ingestion.web import WebProcessor
from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.llm.prompt_loader_md import load_prompt
from course_supporter.llm.stage_router import StageResult
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)

_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


def _assert_partition(text: str, lines: list[NumberedLine]) -> None:
    """Lines tile ``[0, len(text))`` with non-empty spans, numbered 1..N."""
    assert [line.number for line in lines] == list(range(1, len(lines) + 1))
    assert lines[0].start == 0
    assert lines[-1].end == len(text)
    for prev, nxt in pairwise(lines):
        assert prev.end == nxt.start
    for line in lines:
        assert line.end > line.start
        assert line.display
        assert line.display == line.display.rstrip()
        assert line.display in text[line.start : line.end]


def _assert_reversible(lines: list[NumberedLine]) -> None:
    """Every line range converts to a span and back to the same range."""
    for start_line in range(1, len(lines) + 1):
        for end_line in range(start_line, len(lines) + 1):
            span = line_range_to_span(lines, start_line, end_line)
            assert span_to_line_range(lines, *span) == (start_line, end_line)


class TestNumberLines:
    def test_cyrillic_lines(self) -> None:
        text = "Змінна — це ім'я.\nЇї значення змінюється."
        lines = number_lines(text)

        assert [line.display for line in lines] == [
            "Змінна — це ім'я.",
            "Її значення змінюється.",
        ]
        assert text[lines[0].start : lines[0].end] == "Змінна — це ім'я.\n"
        assert text[lines[1].start : lines[1].end] == "Її значення змінюється."
        _assert_partition(text, lines)
        _assert_reversible(lines)

    def test_blank_lines_are_unnumbered_and_owned_by_the_line_above(self) -> None:
        text = "Перший абзац.\n\n\nДругий абзац.\n   \nТретій."
        lines = number_lines(text)

        assert [line.display for line in lines] == [
            "Перший абзац.",
            "Другий абзац.",
            "Третій.",
        ]
        assert text[lines[0].start : lines[0].end] == "Перший абзац.\n\n\n"
        assert text[lines[1].start : lines[1].end] == "Другий абзац.\n   \n"
        _assert_partition(text, lines)
        _assert_reversible(lines)

    def test_last_line_without_newline_ends_at_text_length(self) -> None:
        text = "один\nдва"
        lines = number_lines(text)

        assert lines[-1].end == len(text)
        assert text[lines[-1].start : lines[-1].end] == "два"

    def test_trailing_newline_belongs_to_the_last_line(self) -> None:
        text = "один\nдва\n\n"
        lines = number_lines(text)

        assert len(lines) == 2
        assert text[lines[-1].start : lines[-1].end] == "два\n\n"
        _assert_partition(text, lines)

    def test_leading_blank_lines_belong_to_line_one(self) -> None:
        text = "\n\n  Заголовок\nТекст"
        lines = number_lines(text)

        assert lines[0].start == 0
        assert lines[0].display == "  Заголовок"
        _assert_partition(text, lines)

    def test_single_line_text(self) -> None:
        text = "Лише один рядок без переносу"
        lines = number_lines(text)

        assert len(lines) == 1
        assert (lines[0].start, lines[0].end) == (0, len(text))
        assert line_range_to_span(lines, 1, 1) == (0, len(text))

    def test_blank_text_has_no_lines(self) -> None:
        assert number_lines("") == []
        assert number_lines(" \n\n\t") == []

    def test_chunk_separator_boundaries(self) -> None:
        """Lines of ``assemble_text`` start exactly where each chunk starts."""
        doc = _text_doc(["Вступ", "Перший абзац.", "Другий абзац\nу два рядки."])
        text = doc.assemble_text()
        lines = number_lines(text)

        chunk_starts = [0]
        for chunk in doc.chunks[:-1]:
            chunk_starts.append(chunk_starts[-1] + len(chunk.text) + 2)
        assert [line.start for line in lines][:3] == chunk_starts
        # The "\n\n" separator is owned by the line before it.
        assert text[lines[0].start : lines[0].end] == "Вступ\n\n"
        _assert_partition(text, lines)
        _assert_reversible(lines)

    def test_crlf_line_ends_stay_in_the_span_not_the_display(self) -> None:
        text = "перший\r\nдругий\r\n"
        lines = number_lines(text)

        assert [line.display for line in lines] == ["перший", "другий"]
        _assert_partition(text, lines)

    def test_long_line_is_wrapped_at_sentence_ends(self) -> None:
        sentence = "Це речення про змінні та їхні значення у програмі. "
        text = (sentence * 30).strip()
        lines = number_lines(text)

        assert len(lines) > 1
        for line in lines:
            assert len(line.display) <= MAX_LINE_CHARS
            assert line.display.endswith(".")
        _assert_partition(text, lines)
        _assert_reversible(lines)

    def test_long_line_without_spaces_is_cut_hard(self) -> None:
        text = "я" * (MAX_LINE_CHARS * 2 + 7)
        lines = number_lines(text)

        assert [len(line.display) for line in lines] == [
            MAX_LINE_CHARS,
            MAX_LINE_CHARS,
            7,
        ]
        _assert_partition(text, lines)

    def test_long_whitespace_run_never_yields_an_empty_line(self) -> None:
        text = " " * (MAX_LINE_CHARS + 20) + "слово " * 60
        lines = number_lines(text)

        _assert_partition(text, lines)
        _assert_reversible(lines)


class TestRenderNumbered:
    def test_numbers_every_line_and_keeps_blank_lines(self) -> None:
        text = "# Тема\n\nПерший рядок\nдругий рядок\n\n\nКінець"
        lines = number_lines(text)

        assert render_numbered(text, lines) == (
            "1| # Тема\n\n2| Перший рядок\n3| другий рядок\n\n\n4| Кінець"
        )

    def test_wrapped_line_continues_on_the_next_numbered_line(self) -> None:
        text = "Речення номер один про тему. " * 20
        lines = number_lines(text)
        rendered = render_numbered(text, lines)

        assert rendered.splitlines() == [
            f"{line.number}| {line.display}" for line in lines
        ]


def _response(*ranges: tuple[int, int]) -> str:
    return json.dumps(
        {
            "title": "T",
            "description": "D",
            "segments": [
                {"start_line": start, "end_line": end, "description": f"d{i}"}
                for i, (start, end) in enumerate(ranges)
            ],
        }
    )


def _validate(payload: str, line_count: int) -> TextMappingResponse:
    return TextMappingResponse.model_validate_json(
        payload, context={"line_count": line_count}
    )


def _cover_error(payload: str, line_count: int) -> str:
    with pytest.raises(ValidationError) as exc_info:
        _validate(payload, line_count)
    return str(exc_info.value.errors()[0]["msg"])


class TestLineCover:
    def test_valid_cover(self) -> None:
        response = _validate(_response((1, 4), (5, 5), (6, 10)), line_count=10)
        assert [s.start_line for s in response.segments] == [1, 5, 6]

    def test_gap(self) -> None:
        msg = _cover_error(_response((1, 4), (7, 10)), line_count=10)
        assert "gap: lines 5-6 are not covered" in msg
        assert "segment 2 must start at line 5" in msg

    def test_overlap(self) -> None:
        msg = _cover_error(_response((1, 4), (3, 10)), line_count=10)
        assert "overlap: segment 2 starts at line 3" in msg
        assert "segment 2 must start at line 5" in msg

    def test_last_end_is_not_n(self) -> None:
        msg = _cover_error(_response((1, 4), (5, 8)), line_count=10)
        assert "the last segment ends at line 8" in msg
        assert "the last segment must end at line 10" in msg

    def test_empty_range(self) -> None:
        msg = _cover_error(_response((1, 4), (5, 4), (5, 10)), line_count=10)
        assert "segment 2 is empty: end_line 4 is before start_line 5" in msg

    def test_first_segment_not_at_line_one(self) -> None:
        msg = _cover_error(
            _response(
                (2, 10),
            ),
            line_count=10,
        )
        assert "the first segment must start at line 1" in msg

    def test_out_of_range(self) -> None:
        msg = _cover_error(_response((0, 3), (4, 12)), line_count=10)
        assert "lines are numbered 1-10" in msg

    def test_feedback_lists_every_problem_ranges_and_n(self) -> None:
        """All boundaries are reported at once, with the ranges and N."""
        msg = _cover_error(_response((1, 40), (35, 90), (95, 120)), line_count=100)

        assert "The document has 100 numbered lines (1-100)" in msg
        assert "your ranges are 1-40, 35-90, 95-120" in msg
        assert "overlap: segment 2" in msg
        assert "gap: lines 91-94" in msg
        assert "the last segment must end at line 100" in msg
        assert "Rewrite start_line and end_line of EVERY segment" in msg

    def test_empty_segments_are_allowed(self) -> None:
        assert _validate(_response(), line_count=10).segments == []

    def test_check_is_skipped_without_context(self) -> None:
        response = TextMappingResponse.model_validate_json(_response((3, 1)))
        assert response.line_cover_problems(5)

    def test_one_line_document(self) -> None:
        """N = 1: the only non-empty answer is one segment 1-1."""
        assert _validate(_response((1, 1)), line_count=1).segments
        assert _validate(_response(), line_count=1).segments == []
        for bad in [_response((1, 2)), _response((1, 1), (2, 2))]:
            with pytest.raises(ValidationError):
                _validate(bad, line_count=1)

    def test_two_line_document(self) -> None:
        """N = 2: one segment 1-2, or two segments 1-1 and 2-2."""
        assert len(_validate(_response((1, 2)), line_count=2).segments) == 1
        assert len(_validate(_response((1, 1), (2, 2)), line_count=2).segments) == 2
        for bad in [_response((1, 1)), _response((1, 2), (2, 2))]:
            with pytest.raises(ValidationError):
                _validate(bad, line_count=2)


# ---------------------------------------------------------------------------
# Processors with a stub router
# ---------------------------------------------------------------------------

_MARKDOWN = """# Змінні

Змінна — це ім'я, яке посилається на значення.
Значення змінної можна змінити присвоєнням.

## Типи даних

Кожне значення має тип: int, str, list.
Тип визначає, які операції доступні.

**Практика**
Створіть три змінні різних типів і виведіть їх.
"""

_WEB_TEXT = (
    "Цикл for повторює блок коду для кожного елемента.\n\n"
    "Цикл while працює, поки умова істинна.\nНе забувайте змінювати умову.\n\n"
    "Домашнє завдання: напишіть обидва цикли."
)


def _text_doc(texts: list[str]) -> SourceDocument:
    return SourceDocument(
        source_type=SourceType.TEXT,
        source_url="file:///fixture.md",
        chunks=[
            ContentChunk(chunk_type=ChunkType.PARAGRAPH, text=text, index=i)
            for i, text in enumerate(texts)
        ],
    )


def _markdown_doc() -> SourceDocument:
    return SourceDocument(
        source_type=SourceType.TEXT,
        source_url="file:///fixture.md",
        chunks=TextProcessor._parse_markdown_text(_MARKDOWN),
    )


def _web_doc() -> SourceDocument:
    return SourceDocument(
        source_type=SourceType.WEB,
        source_url="https://example.com/loops",
        chunks=WebProcessor._text_to_chunks(_WEB_TEXT),
    )


def _router_returning(payload: str) -> AsyncMock:
    router = AsyncMock()

    async def _fake_execute(
        _stage_name: str,
        *,
        response_validator: Any | None = None,
        **_render_context: Any,
    ) -> StageResult:
        if response_validator is not None:
            response_validator(payload)
        return StageResult(
            content=payload,
            provider_used="deepseek",
            model_used="deepseek-flash",
            attempt_count=1,
        )

    router.execute_for_stage.side_effect = _fake_execute
    return router


def _char_draft(text: str, cuts: list[str]) -> DocumentSummaryDraft:
    """The correct old-style char-offset answer: segments start at ``cuts``."""
    starts = [0] + [text.index(cut) for cut in cuts]
    ends = [*starts[1:], len(text)]
    return DocumentSummaryDraft.model_validate(
        {
            "title": "T",
            "description": "D",
            "segments": [
                {
                    "order": i,
                    "start_pos": start,
                    "end_pos": end,
                    "description": f"d{i}",
                }
                for i, (start, end) in enumerate(zip(starts, ends, strict=True))
            ],
        },
        context={"reference_text_length": len(text)},
    )


_CASES = [
    pytest.param(
        TextProcessor,
        _markdown_doc,
        # Headings/paragraphs: 1 "Змінні", 2-3 para, 4 "Типи даних",
        # 5-6 para, 7-8 "**Практика**" para.
        [(1, 3), (4, 6), (7, 8)],
        ["Типи даних", "**Практика**"],
        id="text-markdown",
    ),
    pytest.param(
        WebProcessor,
        _web_doc,
        [(1, 1), (2, 3), (4, 4)],
        ["Цикл while", "Домашнє завдання"],
        id="web",
    ),
]


class TestProcessorsWithLineRanges:
    @pytest.mark.parametrize(("processor_cls", "make_doc", "ranges", "cuts"), _CASES)
    @pytest.mark.asyncio
    async def test_line_answer_matches_the_correct_char_answer(
        self,
        processor_cls: type[TextProcessor] | type[WebProcessor],
        make_doc: Any,
        ranges: list[tuple[int, int]],
        cuts: list[str],
    ) -> None:
        processor = processor_cls()
        doc = make_doc()
        text = doc.assemble_text()
        router = _router_returning(_response(*ranges))

        line_draft = await processor.process_macro(doc, router)
        char_draft = _char_draft(text, cuts)

        assert [(s.order, s.start_pos, s.end_pos) for s in line_draft.segments] == [
            (s.order, s.start_pos, s.end_pos) for s in char_draft.segments
        ]
        line_segments = await processor.process_detail(doc, line_draft)
        char_segments = await processor.process_detail(doc, char_draft)
        assert [s.content for s in line_segments] == [s.content for s in char_segments]
        assert [(s.start_paragraph, s.end_paragraph) for s in line_segments] == [
            (s.start_paragraph, s.end_paragraph) for s in char_segments
        ]
        assert "".join(s.content or "" for s in line_segments) == text

    @pytest.mark.parametrize(("processor_cls", "make_doc", "ranges", "cuts"), _CASES)
    @pytest.mark.asyncio
    async def test_router_gets_numbered_text_and_line_count(
        self,
        processor_cls: type[TextProcessor] | type[WebProcessor],
        make_doc: Any,
        ranges: list[tuple[int, int]],
        cuts: list[str],
    ) -> None:
        doc = make_doc()
        router = _router_returning(_response(*ranges))

        await processor_cls().process_macro(doc, router)

        kwargs = router.execute_for_stage.await_args.kwargs
        lines = number_lines(doc.assemble_text())
        assert kwargs["line_count"] == len(lines) == ranges[-1][1]
        assert kwargs["text"] == render_numbered(doc.assemble_text(), lines)
        assert kwargs["text"].startswith("1| ")

    @pytest.mark.parametrize(("processor_cls", "make_doc", "ranges", "cuts"), _CASES)
    @pytest.mark.asyncio
    async def test_bad_cover_raises_structural_retry_with_lines(
        self,
        processor_cls: type[TextProcessor] | type[WebProcessor],
        make_doc: Any,
        ranges: list[tuple[int, int]],
        cuts: list[str],
    ) -> None:
        n = ranges[-1][1]
        router = _router_returning(_response((1, 2), (4, n + 5)))

        with pytest.raises(StructuralRetryError) as exc_info:
            await processor_cls().process_macro(make_doc(), router)

        feedback = exc_info.value.feedback
        assert f"The document has {n} numbered lines" in feedback
        assert "gap: lines 3-3 are not covered" in feedback
        assert f"the last segment must end at line {n}" in feedback


class TestPromptRender:
    def test_prompt_shows_line_numbers_and_n_and_no_char_positions(self) -> None:
        doc = _markdown_doc()
        text = doc.assemble_text()
        lines = number_lines(text)
        prompt = load_prompt("pass_2a_mapping/v1.md", base_path=_PROMPTS_DIR)

        rendered = prompt.render(
            text=render_numbered(text, lines),
            line_count=len(lines),
            language="Ukrainian",
        )

        full = f"{rendered.system}\n{rendered.user}"
        assert rendered.user is not None
        assert "1| Змінні" in rendered.user
        assert f"{len(lines)}| Створіть три змінні" in rendered.user
        assert f"N = {len(lines)} numbered lines" in rendered.user
        assert '"start_line"' in full
        assert '"end_line"' in full
        for banned in ("start_pos", "end_pos", "char offset", "character position"):
            assert banned not in full
        # Document text reaches the model as-is: no \uXXXX escapes.
        assert "\\u" not in rendered.user
        assert "ім'я" in rendered.user
