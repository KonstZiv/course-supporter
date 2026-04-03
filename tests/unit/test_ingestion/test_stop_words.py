"""Tests for dynamic stop words module."""

from __future__ import annotations

from course_supporter.ingestion.stop_words import (
    build_stop_words,
    supported_natural_languages,
    supported_programming_languages,
)


class TestBuildStopWords:
    def test_english_python_default(self) -> None:
        sw = build_stop_words()
        assert "the" in sw  # English
        assert "def" in sw  # Python
        assert "це" not in sw  # not Ukrainian

    def test_ukrainian_javascript(self) -> None:
        sw = build_stop_words(natural_lang="uk", programming_lang="javascript")
        assert "це" in sw  # Ukrainian
        assert "const" in sw  # JavaScript
        assert "the" not in sw  # not English
        assert "def" not in sw  # not Python

    def test_german_csharp(self) -> None:
        sw = build_stop_words(natural_lang="de", programming_lang="csharp")
        assert "der" in sw  # German
        assert "namespace" in sw  # C#

    def test_serbian_java(self) -> None:
        sw = build_stop_words(natural_lang="sr", programming_lang="java")
        assert "је" in sw  # Serbian
        assert "extends" in sw  # Java

    def test_sql(self) -> None:
        sw = build_stop_words(programming_lang="sql")
        assert "select" in sw
        assert "varchar" in sw

    def test_typescript_extends_javascript(self) -> None:
        js = build_stop_words(programming_lang="javascript")
        ts = build_stop_words(programming_lang="typescript")
        assert "interface" in ts
        assert "interface" not in js
        assert ts > js  # TS is superset of JS

    def test_unknown_language_empty(self) -> None:
        sw = build_stop_words(natural_lang="xx", programming_lang="cobol")
        assert len(sw) == 0

    def test_combined_set(self) -> None:
        sw = build_stop_words(natural_lang="en", programming_lang="python")
        assert "the" in sw and "def" in sw


class TestSupportedLanguages:
    def test_natural_languages(self) -> None:
        langs = supported_natural_languages()
        assert "en" in langs
        assert "uk" in langs
        assert "de" in langs
        assert "sr" in langs

    def test_programming_languages(self) -> None:
        langs = supported_programming_languages()
        assert "python" in langs
        assert "javascript" in langs
        assert "typescript" in langs
        assert "csharp" in langs
        assert "java" in langs
        assert "sql" in langs
