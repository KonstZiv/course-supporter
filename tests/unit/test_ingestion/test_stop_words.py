"""Tests for dynamic stop words module."""

from __future__ import annotations

import pytest

from course_supporter.ingestion.stop_words import (
    build_stop_words,
    supported_natural_languages,
    supported_programming_languages,
)
from course_supporter.language import InvalidLanguageError, LanguageNotAllowedError


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

    def test_spanish(self) -> None:
        sw = build_stop_words(natural_lang="es", programming_lang="python")
        assert "función" in sw
        assert "pero" in sw

    def test_croatian(self) -> None:
        sw = build_stop_words(natural_lang="hr", programming_lang="python")
        assert "funkcija" in sw

    def test_montenegrin_extends_serbian(self) -> None:
        sr = build_stop_words(natural_lang="sr", programming_lang="python")
        cnr = build_stop_words(natural_lang="cnr", programming_lang="python")
        assert cnr >= sr

    def test_turkish(self) -> None:
        sw = build_stop_words(natural_lang="tr", programming_lang="python")
        assert "fonksiyon" in sw

    def test_romanian(self) -> None:
        sw = build_stop_words(natural_lang="ro", programming_lang="python")
        assert "funcție" in sw

    def test_bulgarian(self) -> None:
        sw = build_stop_words(natural_lang="bg", programming_lang="python")
        assert "функция" in sw

    def test_polish(self) -> None:
        sw = build_stop_words(natural_lang="pl", programming_lang="python")
        assert "funkcja" in sw

    def test_lithuanian(self) -> None:
        sw = build_stop_words(natural_lang="lt", programming_lang="python")
        assert "funkcija" in sw

    def test_swedish(self) -> None:
        sw = build_stop_words(natural_lang="sv", programming_lang="python")
        assert "funktion" in sw

    def test_french(self) -> None:
        sw = build_stop_words(natural_lang="fr", programming_lang="python")
        assert "fonction" in sw

    def test_rust(self) -> None:
        sw = build_stop_words(programming_lang="rust")
        assert "mut" in sw
        assert "unwrap" in sw

    def test_go(self) -> None:
        sw = build_stop_words(programming_lang="go")
        assert "func" in sw
        assert "goroutine" in sw

    def test_css(self) -> None:
        sw = build_stop_words(programming_lang="css")
        assert "display" in sw
        assert "margin" in sw

    def test_html(self) -> None:
        sw = build_stop_words(programming_lang="html")
        assert "div" in sw
        assert "href" in sw

    def test_elixir(self) -> None:
        sw = build_stop_words(programming_lang="elixir")
        assert "defmodule" in sw
        assert "genserver" in sw

    def test_c(self) -> None:
        sw = build_stop_words(programming_lang="c")
        assert "malloc" in sw
        assert "printf" in sw

    def test_cpp_extends_c(self) -> None:
        c = build_stop_words(programming_lang="c")
        cpp = build_stop_words(programming_lang="cpp")
        assert cpp > c
        assert "vector" in cpp
        assert "namespace" in cpp

    def test_unknown_natural_language_is_refused(self) -> None:
        # Used to return the Python keywords and no error, so a caller
        # passing the 639-3 code the database stores got a set that looked
        # usable and matched nothing.
        with pytest.raises(InvalidLanguageError):
            build_stop_words(natural_lang="xx")

    def test_language_outside_the_whitelist_is_refused(self) -> None:
        with pytest.raises(LanguageNotAllowedError):
            build_stop_words(natural_lang="lat")

    def test_stored_639_3_code_is_accepted(self) -> None:
        # The form the database actually holds.
        assert "це" in build_stop_words(natural_lang="ukr")
        assert "це" in build_stop_words(natural_lang="Ukrainian")

    def test_russian_set_is_present(self) -> None:
        sw = build_stop_words(natural_lang="rus")
        assert "это" in sw
        assert "функция" in sw
        assert "це" not in sw  # not Ukrainian

    def test_unknown_programming_language_contributes_nothing(self) -> None:
        sw = build_stop_words(natural_lang="en", programming_lang="cobol")
        assert "the" in sw
        assert "def" not in sw

    def test_combined_set(self) -> None:
        sw = build_stop_words(natural_lang="en", programming_lang="python")
        assert "the" in sw and "def" in sw


class TestSupportedLanguages:
    def test_natural_languages(self) -> None:
        langs = supported_natural_languages()
        for code in [
            "eng",
            "ukr",
            "rus",
            "deu",
            "srp",
            "hrv",
            "cnr",
            "spa",
            "tur",
            "ron",
            "bul",
            "pol",
            "lit",
            "swe",
            "fra",
        ]:
            assert code in langs

    def test_programming_languages(self) -> None:
        langs = supported_programming_languages()
        for name in [
            "python",
            "javascript",
            "typescript",
            "csharp",
            "java",
            "sql",
            "rust",
            "go",
            "css",
            "html",
            "elixir",
            "c",
            "cpp",
        ]:
            assert name in langs
