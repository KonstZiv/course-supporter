"""Tests for encoding recovery.

The corpus is the actual homework file that was refused in production on
2026-09-02 (``tests/fixtures/security/encodings/live_submission_cp1251.md``,
2318 bytes, sha256 ``445ebccf…bbdce``) -- and excerpts and re-encodings of
it. Synthetic strings are used only where the point is a boundary that no
real file demonstrates, such as the 64 KiB detector window.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from course_supporter.linguistics import count_foreign_letters
from course_supporter.security.charset_recovery import (
    MAX_FOREIGN_LETTERS_PER_1000,
    MIN_RECOVERABLE_BYTES,
    recover_text,
)

_FIXTURE = (
    Path(__file__).resolve().parent.parent.parent
    / "fixtures"
    / "security"
    / "encodings"
    / "live_submission_cp1251.md"
)

LIVE_BYTES = _FIXTURE.read_bytes()
LIVE_TEXT = LIVE_BYTES.decode("cp1251")
UKR = ["ukr"]


class TestTheFixtureItself:
    def test_the_bytes_are_the_ones_production_refused(self) -> None:
        # Everything below reads meaning into these exact bytes -- the tie
        # between cp1251, kz1048 and ptcp154 lives in thirteen of them. A
        # normalizing tool that appends a newline would leave every test
        # green while testing a file nobody ever submitted; end-of-file-fixer
        # did precisely that once, which is why it now skips this directory.
        assert len(LIVE_BYTES) == 2318
        assert hashlib.sha256(LIVE_BYTES).hexdigest() == (
            "445ebccf27a414c01c8a458054c37b1aba59d4948b7d17508eefe00a56cbbdce"
        )


class TestUtf8NeedsNoVerification:
    def test_utf8_is_its_own_proof(self) -> None:
        raw = LIVE_TEXT.encode("utf-8")
        result = recover_text(raw, languages=UKR)
        assert result.verified
        assert result.encoding == "utf-8"
        assert result.text == LIVE_TEXT

    def test_utf8_is_accepted_without_a_verifiable_language(self) -> None:
        # Valid UTF-8 decodes before the language question is ever asked.
        result = recover_text(b"Ceci est un texte.", languages=["jpn"])
        assert result.verified
        assert result.encoding == "utf-8"

    def test_utf8_shorter_than_the_threshold_is_still_accepted(self) -> None:
        result = recover_text(b"hi", languages=UKR)
        assert result.verified and result.text == "hi"

    def test_byte_order_mark_survives_for_the_screening_step(self) -> None:
        # DD-SP-E strips exactly one leading BOM further down the pipeline;
        # doing it here too would be a second place to keep in step.
        result = recover_text("﻿Привіт".encode(), languages=UKR)
        assert result.verified and result.text.startswith("﻿")


def portable(text: str, encoding: str) -> str:
    """``text`` with the characters that ``encoding`` cannot hold dropped.

    KOI8-U and ISO-8859-5 have no em dash, and the live file has several.
    Encoding with ``errors="replace"`` would put question marks into the
    fixture and then test recovery of a text nobody wrote; dropping the
    characters keeps every byte real. What survives is still the same
    prose, and still far above the length threshold.
    """
    out = []
    for char in text:
        try:
            char.encode(encoding)
        except UnicodeEncodeError:
            continue
        out.append(char)
    return "".join(out)


class TestRecoveryTable:
    @pytest.mark.parametrize(
        "encoding",
        ["cp1251", "koi8_u", "iso8859_5", "mac_cyrillic", "utf-16"],
    )
    def test_real_bytes_recover_to_the_original_text(self, encoding: str) -> None:
        expected = portable(LIVE_TEXT, encoding)
        raw = expected.encode(encoding)
        assert len(raw) >= MIN_RECOVERABLE_BYTES
        result = recover_text(raw, languages=UKR)
        assert result.verified, result.reason
        assert result.text == expected

    def test_utf8_with_a_byte_order_mark_round_trips(self) -> None:
        raw = LIVE_TEXT.encode("utf-8-sig")
        result = recover_text(raw, languages=UKR)
        assert result.verified and result.encoding == "utf-8"
        assert result.text.lstrip("\ufeff") == LIVE_TEXT

    def test_the_live_submission_recovers_as_cp1251(self) -> None:
        # The file production refused. Its bytes are the tie described in
        # the module docstring, so the encoding name is asserted too.
        result = recover_text(LIVE_BYTES, languages=UKR)
        assert result.verified, result.reason
        assert result.encoding == "cp1251"
        assert result.text == LIVE_TEXT
        assert result.reason is None


class TestTheTie:
    """The measured near-miss: three readings, identical library scores."""

    @pytest.mark.parametrize("encoding", ["kz1048", "ptcp154"])
    def test_rival_readings_carry_foreign_letters(self, encoding: str) -> None:
        # This is why the letter check exists: the rival texts are 99.4%
        # identical to the truth and differ only where Ukrainian letters
        # belong, so nothing else separates them.
        rival = LIVE_BYTES.decode(encoding)
        assert rival != LIVE_TEXT
        assert count_foreign_letters(rival, UKR) > 0
        assert count_foreign_letters(LIVE_TEXT, UKR) == 0

    def test_the_recovered_text_has_no_kazakh_letters(self) -> None:
        recovered = recover_text(LIVE_BYTES, languages=UKR).text
        assert not (set("ғүҝә") & set(recovered))

    def test_identical_texts_merge_into_one_candidate(self) -> None:
        # An excerpt with no ``ї``/``є`` reads the same under all three
        # encodings. Merging them keeps a naming difference from being
        # read as a disagreement about the text -- the answer is accepted,
        # and which of the three names it carries does not matter.
        plain = "".join(ch for ch in LIVE_TEXT if ch not in "їєЇЄ")[:600]
        raw = plain.encode("cp1251")
        assert raw.decode("kz1048") == plain
        result = recover_text(raw, languages=UKR)
        assert result.verified, result.reason
        assert result.text == plain


class TestRefusals:
    def test_too_short(self) -> None:
        raw = LIVE_TEXT[:14].encode("cp1251")
        result = recover_text(raw, languages=UKR)
        assert not result.verified
        assert result.reason == "too_short"

    def test_the_length_that_used_to_be_accepted_wrongly(self) -> None:
        # At 32 characters the rule accepted a mac_cyrillic reading of
        # cp1251 bytes -- a wrong text, silently. That measurement is what
        # set MIN_RECOVERABLE_BYTES; this locks the refusal.
        middle = LIVE_TEXT[len(LIVE_TEXT) // 2 :][:32]
        result = recover_text(middle.encode("cp1251"), languages=UKR)
        assert not result.verified
        assert result.reason == "too_short"

    def test_just_above_the_threshold_recovers(self) -> None:
        raw = LIVE_TEXT[:MIN_RECOVERABLE_BYTES].encode("cp1251")
        assert len(raw) >= MIN_RECOVERABLE_BYTES
        result = recover_text(raw, languages=UKR)
        assert result.verified, result.reason
        assert result.text == LIVE_TEXT[:MIN_RECOVERABLE_BYTES]

    def test_no_verification_for_language(self) -> None:
        # Whitelisted, but no alphabet and no stop words here: an honest
        # boundary rather than a silent acceptance.
        result = recover_text(LIVE_BYTES, languages=["jpn"])
        assert not result.verified
        assert result.reason == "no_verification_for_language"

    def test_empty_language_list(self) -> None:
        result = recover_text(LIVE_BYTES, languages=[])
        assert not result.verified
        assert result.reason == "no_verification_for_language"

    def test_text_in_another_language_of_the_same_script(self) -> None:
        # Russian prose on a Ukrainian course: the letters ``ы``/``э``/``ъ``
        # are foreign to the alphabet being verified against, so the file
        # is refused rather than reviewed as if it were Ukrainian.
        russian = (
            "Эта функция возвращает значение, если переменная не пустая. "
            "Мы проверяем краевые случаи и обрабатываем ошибки, чтобы "
            "результат был предсказуемым при любых входных данных. "
            "Ниже приведены примеры вызовов и ожидаемые значения. "
        ) * 3
        result = recover_text(russian.encode("cp1251"), languages=UKR)
        assert not result.verified
        assert result.reason == "foreign_letters"

    def test_the_same_text_is_recovered_when_russian_is_a_review_language(
        self,
    ) -> None:
        russian = (
            "Эта функция возвращает значение, если переменная не пустая. "
            "Мы проверяем краевые случаи и обрабатываем ошибки, чтобы "
            "результат был предсказуемым при любых входных данных. "
            "Ниже приведены примеры вызовов и ожидаемые значения. "
        ) * 3
        result = recover_text(russian.encode("cp1251"), languages=["ukr", "rus"])
        assert result.verified, result.reason
        assert result.text == russian


class TestTheDetectorWindow:
    def test_latin_ahead_of_one_cyrillic_byte(self) -> None:
        # The hole the old label gate left: libmagic inspects roughly the
        # first 64 KiB, called this ``us-ascii``, passed it, and the strict
        # decode behind the gate then raised UnicodeDecodeError with no
        # category attached. Recovery decides by decoding, so the size of
        # the head no longer matters -- the answer is a named refusal.
        filler = ("The quick brown fox jumps over the lazy dog. " * 1600).encode()
        raw = filler[: 64 * 1024] + b"\xd0"
        result = recover_text(raw, languages=UKR)
        assert not result.verified
        assert result.reason in {"foreign_letters", "no_stop_words", "ambiguous"}

    def test_the_same_bytes_as_utf8_are_accepted(self) -> None:
        filler = ("The quick brown fox jumps over the lazy dog. " * 1600).encode()
        raw = filler[: 64 * 1024] + "и".encode()
        result = recover_text(raw, languages=UKR)
        assert result.verified and result.encoding == "utf-8"


class TestCalibration:
    def test_constants_are_the_measured_ones(self) -> None:
        # A silent edit to either number changes which files reach the
        # Mentor; both were calibrated on the grid in the commit report.
        assert MIN_RECOVERABLE_BYTES == 240
        assert MAX_FOREIGN_LETTERS_PER_1000 == 1.0

    def test_tolerance_stays_below_a_wrong_readings_density(self) -> None:
        # The margin the tolerance depends on: the lowest density measured
        # for a wrong reading that actually differs in text, at or above
        # the length threshold, was 2.08 per thousand.
        rival = LIVE_BYTES.decode("kz1048")
        density = 1000 * count_foreign_letters(rival, UKR) / len(rival)
        assert density > MAX_FOREIGN_LETTERS_PER_1000
