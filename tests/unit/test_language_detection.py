"""Tests for Mentor language detection cascade."""

from __future__ import annotations

from unittest.mock import MagicMock

from course_supporter.homework.language import (
    _detect_from_text,
    _extract_human_text,
    detect_response_language,
)
from course_supporter.security.schemas import FileContent, SubmissionContent


def _make_student(preferred_language: str | None = None) -> MagicMock:
    s = MagicMock()
    s.preferred_language = preferred_language
    return s


def _make_node(description: str | None = None) -> MagicMock:
    n = MagicMock()
    n.description = description
    return n


def _make_content(text: str) -> SubmissionContent:
    return SubmissionContent(
        files=[FileContent(filename="main.py", content=text, size=len(text))],
        total_size=len(text),
    )


class TestExtractHumanText:
    """Extract comments and docstrings from code."""

    def test_python_comments(self) -> None:
        code = "# Це функція\nx = 1\n# Повертає результат"
        result = _extract_human_text(code)
        assert "Це функція" in result
        assert "Повертає результат" in result

    def test_python_docstring(self) -> None:
        code = '"""Клас для обробки даних."""\nclass Foo: pass'
        result = _extract_human_text(code)
        assert "Клас для обробки даних" in result

    def test_js_comments(self) -> None:
        code = "// This is a function\nconst x = 1;"
        result = _extract_human_text(code)
        assert "This is a function" in result

    def test_no_comments(self) -> None:
        code = "x = 1\ny = 2\nz = x + y"
        result = _extract_human_text(code)
        assert result == ""

    def test_whitespace_only(self) -> None:
        result = _extract_human_text("   \n\t\n  ")
        assert result == ""

    def test_empty_comments_ignored(self) -> None:
        code = "x = 1  # \ny = 2  # \nz = x + y"
        result = _extract_human_text(code)
        assert result == ""


class TestDetectFromText:
    """Language detection from human-readable text."""

    def test_ukrainian(self) -> None:
        assert _detect_from_text("функція повертає результат перевірки") == "uk"

    def test_russian(self) -> None:
        assert _detect_from_text("функция возвращает результат проверки") == "ru"

    def test_german(self) -> None:
        assert _detect_from_text("Funktion Variable Ergebnis Aufgabe") == "de"

    def test_english_by_script(self) -> None:
        text = "This function returns the validation result for input"
        assert _detect_from_text(text) == "en"

    def test_cyrillic_fallback_to_ukrainian(self) -> None:
        # Cyrillic text without specific markers → defaults to Ukrainian
        assert _detect_from_text("абвгдеєжзийклмнопрст") == "uk"

    def test_mixed_uk_ru_markers(self) -> None:
        # UK markers: функція, повертає, результат (3)
        # RU markers: результат (1, shared word)
        # UK wins by marker count
        text = "функція повертає результат проверки"
        assert _detect_from_text(text) == "uk"

    def test_latin_with_cyrillic_marker(self) -> None:
        # "функція" is a UK marker — hits MARKER branch, not script detection
        text = "This is a функція for processing data"
        assert _detect_from_text(text) == "uk"

    def test_no_false_positive_on_substring(self) -> None:
        # "рядок" (UK marker) must NOT match inside "порядок" (different word)
        # "строка" is RU-only, "цикл" is shared UK+RU
        # With word boundaries: UK=1 (цикл), RU=2 (строка+цикл) → RU wins
        # Without word boundaries: UK=2 (рядок+цикл), RU=2 → ambiguous
        text = "встановити порядок використання строка та цикл"
        assert _detect_from_text(text) == "ru"

    def test_too_short_returns_none(self) -> None:
        assert _detect_from_text("ok") is None

    def test_empty_returns_none(self) -> None:
        assert _detect_from_text("") is None


class TestDetectResponseLanguage:
    """Full cascade logic."""

    def test_explicit_request_wins(self) -> None:
        result = detect_response_language(
            request_language="de",
            student=_make_student("uk"),
            submission_content=_make_content("# функція"),
            course_node=_make_node("English course"),
        )
        assert result == "de"

    def test_student_preference_second(self) -> None:
        result = detect_response_language(
            request_language=None,
            student=_make_student("uk"),
            submission_content=_make_content("x = 1"),
            course_node=_make_node("English course"),
        )
        assert result == "uk"

    def test_auto_detect_third(self) -> None:
        result = detect_response_language(
            request_language=None,
            student=_make_student(None),
            submission_content=_make_content("# функція повертає результат"),
            course_node=_make_node("English course"),
        )
        assert result == "uk"

    def test_course_language_fourth(self) -> None:
        result = detect_response_language(
            request_language=None,
            student=_make_student(None),
            submission_content=_make_content("x = 1"),
            course_node=_make_node("Курс Python для початківців з результатами"),
        )
        assert result == "uk"

    def test_fallback_to_english(self) -> None:
        result = detect_response_language(
            request_language=None,
            student=None,
            submission_content=None,
            course_node=None,
        )
        assert result == "en"

    def test_no_student_no_content(self) -> None:
        result = detect_response_language(
            request_language=None,
            student=None,
            submission_content=_make_content("x = 1\ny = 2"),
            course_node=None,
        )
        assert result == "en"

    def test_empty_submission_content(self) -> None:
        result = detect_response_language(
            request_language=None,
            student=None,
            submission_content=_make_content(""),
            course_node=None,
        )
        assert result == "en"
