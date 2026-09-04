"""Tests for the per-language letter and word data.

Every alphabet is locked by a real sentence in that language, per
``impl-rules#13``: a table of letters is exactly the kind of hand-written
list that rots silently, and the only thing that catches a missing letter
is text that uses it. The sentences are short on purpose -- each one has
to contain at least one stop word of its own language, so both signals
are exercised by the same fixture.
"""

from __future__ import annotations

import pytest

from course_supporter.linguistics import (
    ALPHABETS,
    NATURAL_STOP_WORDS,
    alphabet_for,
    count_foreign_letters,
    has_language_data,
    script_of,
    stop_word_share,
    supported_languages,
)

# One real sentence per row of ALPHABETS. Not translations of each other:
# each is written to use letters specific to its own alphabet, which is
# what makes it a lock rather than a decoration.
EXCERPTS: dict[str, str] = {
    "eng": "The function returns a value when the variable is not empty.",
    "ukr": "Ця функція повертає значення, якщо змінна не порожня.",
    "rus": "Эта функция возвращает значение, если переменная не пустая.",
    "deu": "Die Funktion gibt einen Wert zurück, wenn die Variable nicht leer ist.",
    "srp": "Ова функција враћа вредност када променљива није празна.",
    "hrv": "Ova funkcija vraća vrijednost kada varijabla nije prazna.",
    "cnr": "Ova funkcija śutra vraća vrijednost kad varijabla nije prazna.",
    "spa": "Esta función devuelve un valor cuando la variable no está vacía.",
    "tur": "İşlem bittiğinde bu fonksiyon bir değer döndürür.",
    "ron": "Această funcție returnează o valoare când variabila nu este goală.",
    "bul": "Тази функция връща стойност, когато променливата не е празна.",
    "pol": "Ta funkcja zwraca wartość, gdy zmienna nie jest pusta.",
    "lit": "Ši funkcija grąžina reikšmę, kai kintamasis nėra tuščias.",
    "swe": "Den här funktionen returnerar ett värde när variabeln inte är tom.",
    "fra": "Cette fonction renvoie une valeur lorsque la variable n'est pas vide.",
}


class TestTableShape:
    def test_every_language_has_both_signals(self) -> None:
        # A language with only one of the two cannot be verified, so the
        # tables must not drift apart.
        assert set(ALPHABETS) == set(NATURAL_STOP_WORDS)
        assert set(supported_languages()) == set(ALPHABETS)

    def test_every_row_has_an_excerpt(self) -> None:
        assert set(EXCERPTS) == set(ALPHABETS)

    def test_has_language_data(self) -> None:
        assert has_language_data("ukr")
        assert not has_language_data("jpn")  # whitelisted, no data here
        assert not has_language_data("xx")


class TestAlphabetsAgainstRealText:
    @pytest.mark.parametrize("code", sorted(EXCERPTS))
    def test_own_language_has_no_foreign_letters(self, code: str) -> None:
        assert count_foreign_letters(EXCERPTS[code], [code]) == 0

    @pytest.mark.parametrize("code", sorted(EXCERPTS))
    def test_own_language_uses_its_own_stop_words(self, code: str) -> None:
        assert stop_word_share(EXCERPTS[code], [code]) > 0


class TestForeignLetters:
    def test_ukrainian_letters_are_foreign_to_russian(self) -> None:
        # ``і`` and ``я``-with-``і`` words: the pair that the encoding
        # check has to separate in practice.
        assert count_foreign_letters(EXCERPTS["ukr"], ["rus"]) > 0

    def test_russian_letters_are_foreign_to_ukrainian(self) -> None:
        # ``Э`` and ``ы`` have no place in Ukrainian.
        assert count_foreign_letters(EXCERPTS["rus"], ["ukr"]) > 0

    def test_kazakh_letters_are_foreign_to_ukrainian(self) -> None:
        # The measured tie: ``ғ``/``ү`` stand where ``є``/``ї`` belong when
        # cp1251 bytes are read as kz1048.
        assert count_foreign_letters("ғұрын үміт", ["ukr"]) == 4

    def test_latin_inside_cyrillic_prose_is_not_foreign(self) -> None:
        # A library name or a URL is not evidence of a bad decoding.
        assert count_foreign_letters("Читайте документацію wordpress.", ["ukr"]) == 0

    @pytest.mark.parametrize(
        ("code", "text"),
        [
            ("hrv", "Ova funkcija koristi biblioteku wxWidgets za prikaz."),
            ("srp", "Ова функција користи wxWidgets за приказ."),
            ("cnr", "Ova funkcija koristi wxWidgets i query za prikaz."),
            ("tur", "Bu fonksiyon numpy ve query yardımıyla veri okur."),
            ("pol", "Ta funkcja korzysta z biblioteki wxPython i query."),
            ("lit", "Ši funkcija naudoja wxWidgets biblioteką."),
        ],
    )
    def test_ascii_letters_absent_from_an_alphabet_are_still_not_foreign(
        self, code: str, text: str
    ) -> None:
        # None of these alphabets has ``q``/``w``/``x``/``y``, yet a library
        # name or a line of code carries them into perfectly correct prose.
        # Every single-byte encoding agrees on the ASCII range, so those
        # letters say nothing about the decoding either way.
        assert count_foreign_letters(text, [code]) == 0

    def test_the_check_still_bites_on_letters_that_do_carry_evidence(self) -> None:
        # Control for the exemption above: outside the ASCII range the
        # letter check keeps its teeth.
        assert count_foreign_letters("Ова функција користи ғұрын үміт.", ["srp"]) > 0

    def test_digits_marks_and_punctuation_are_not_letters(self) -> None:
        assert count_foreign_letters("Розділ 2: «функція» — 100%!", ["ukr"]) == 0

    def test_combining_acute_is_a_mark_not_a_letter(self) -> None:
        # Montenegrin ``с́`` is ``с`` plus U+0301; the mark must not be
        # counted, or every Montenegrin text would look mis-decoded.
        assert count_foreign_letters("с́utra", ["cnr"]) == 0

    def test_two_languages_widen_the_alphabet(self) -> None:
        mixed = "Эта змінна не пустая."
        assert count_foreign_letters(mixed, ["ukr"]) > 0
        assert count_foreign_letters(mixed, ["ukr", "rus"]) == 0

    def test_unknown_language_judges_nothing(self) -> None:
        assert count_foreign_letters(EXCERPTS["ukr"], ["xx"]) == 0
        assert alphabet_for(["xx"]) == frozenset()


class TestScriptOf:
    @pytest.mark.parametrize(
        ("char", "expected"),
        [
            ("а", "CYRILLIC"),
            ("a", "LATIN"),
            ("ї", "CYRILLIC"),
            ("ß", "LATIN"),
            ("ı", "LATIN"),
            ("1", None),
            (" ", None),
            ("—", None),
            ("́", None),  # COMBINING ACUTE ACCENT
        ],
    )
    def test_script_of(self, char: str, expected: str | None) -> None:
        assert script_of(char) == expected


class TestStopWordShare:
    def test_empty_text(self) -> None:
        assert stop_word_share("", ["ukr"]) == 0.0

    def test_no_words_of_the_language(self) -> None:
        assert stop_word_share("aaa bbb ccc", ["ukr"]) == 0.0

    def test_unknown_language_scores_nothing(self) -> None:
        assert stop_word_share(EXCERPTS["ukr"], ["xx"]) == 0.0


class TestAlphabetConstruction:
    def test_both_cases_are_present(self) -> None:
        assert "а" in ALPHABETS["ukr"] and "А" in ALPHABETS["ukr"]

    def test_turkish_dotted_capital_is_listed_explicitly(self) -> None:
        # ``'i'.upper()`` is ``'I'`` in Python, never ``'İ'``.
        assert "İ" in ALPHABETS["tur"]
        assert "ı" in ALPHABETS["tur"] and "I" in ALPHABETS["tur"]

    def test_sharp_s_does_not_leak_its_two_character_uppercase(self) -> None:
        assert "ß" in ALPHABETS["deu"]
        assert "SS" not in ALPHABETS["deu"]

    def test_serbian_carries_both_scripts(self) -> None:
        assert "ћ" in ALPHABETS["srp"] and "č" in ALPHABETS["srp"]

    def test_montenegrin_extends_serbian(self) -> None:
        assert ALPHABETS["cnr"] > ALPHABETS["srp"]
        assert "ś" in ALPHABETS["cnr"] and "ź" in ALPHABETS["cnr"]

    def test_romanian_accepts_both_comma_and_cedilla_forms(self) -> None:
        # Legacy Romanian files still use the cedilla letters.
        assert "ș" in ALPHABETS["ron"] and "ş" in ALPHABETS["ron"]
