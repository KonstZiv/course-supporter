"""Tests for Stage 1 unicode hard-reject (KD14).

The test class :class:`TestZeroWidthRejected`,
:class:`TestBidiOverrideRejected`,
:class:`TestTagCharactersRejected`, and
:class:`TestControlCharactersRejected` together exercise all four
hostile-character classes per vision §KD14, ensuring each class
maps to the same :class:`ErrorCategory.SUSPICIOUS_UNICODE` while
the detail string distinguishes them for log aggregation.
"""

from pathlib import Path

import pytest

from course_supporter.security.exceptions import (
    ErrorCategory,
    SecurityRejectedError,
)
from course_supporter.security.normalization import nfkc_for_security
from course_supporter.security.unicode_check import check_text_unicode_safety

# ── Clean inputs pass ──────────────────────────────────────────────


class TestCleanInputsAccepted:
    def test_pure_ascii_prose(self) -> None:
        check_text_unicode_safety("Hello, world!")

    def test_ukrainian_text(self) -> None:
        check_text_unicode_safety("Привіт, як справи?")

    def test_ukrainian_multiline_prose(self) -> None:
        check_text_unicode_safety("Це домашнє завдання.\nПрошу перевірити.")

    def test_whitelisted_controls_allowed(self) -> None:
        check_text_unicode_safety("line1\nline2\r\nline3\twith tab")

    def test_emoji_allowed(self) -> None:
        check_text_unicode_safety("Great work! 👍🎓")

    def test_empty_string_allowed(self) -> None:
        check_text_unicode_safety("")

    def test_soft_hyphen_not_rejected(self) -> None:
        # SOFT HYPHEN U+00AD is Cf-category but NOT in the spec's
        # zero-width or bidi lists. Documents that we follow vision
        # explicitly rather than blanket Cf rejection.
        check_text_unicode_safety("co­operation")

    def test_lrm_rlm_not_rejected(self) -> None:
        # LRM U+200E and RLM U+200F are Cf bidi directionality
        # markers but NOT in the spec's bidi-override list (they do
        # not override; they hint).
        check_text_unicode_safety("text‎and‏more")


# ── Zero-width characters rejected ─────────────────────────────────


class TestZeroWidthRejected:
    @pytest.mark.parametrize(
        ("char", "name"),
        [
            ("​", "ZWSP"),
            ("‌", "ZWNJ"),
            ("‍", "ZWJ"),
            ("﻿", "BOM"),
            ("⁠", "WJ"),
        ],
    )
    def test_each_zero_width_char(self, char: str, name: str) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            check_text_unicode_safety(f"clean prefix{char}suffix")
        assert exc_info.value.category is ErrorCategory.SUSPICIOUS_UNICODE
        assert "zero_width" in exc_info.value.detail

    def test_zero_width_at_start(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            check_text_unicode_safety("​text")
        assert "index 0" in exc_info.value.detail

    def test_index_in_detail(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            check_text_unicode_safety("ab​cd")
        assert "index 2" in exc_info.value.detail

    def test_codepoint_in_detail(self) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            check_text_unicode_safety("​")
        assert "U+200B" in exc_info.value.detail


# ── Bidi override characters rejected ──────────────────────────────


class TestBidiOverrideRejected:
    @pytest.mark.parametrize(
        "char",
        [
            "‪",
            "‫",
            "‬",
            "‭",
            "‮",
            "⁦",
            "⁧",
            "⁨",
            "⁩",
        ],
    )
    def test_each_bidi_char(self, char: str) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            check_text_unicode_safety(f"prefix{char}suffix")
        assert exc_info.value.category is ErrorCategory.SUSPICIOUS_UNICODE
        assert "bidi_override" in exc_info.value.detail

    def test_trojan_source_inline_attack(self) -> None:
        # CVE-2021-42574-style payload (U+202E + U+202C).
        attack = "if (auth) { /* ‮ } { ‬ */"
        with pytest.raises(SecurityRejectedError):
            check_text_unicode_safety(attack)


# ── Tag characters rejected ────────────────────────────────────────


class TestTagCharactersRejected:
    @pytest.mark.parametrize(
        "codepoint",
        [0xE0000, 0xE0020, 0xE0041, 0xE007F],
    )
    def test_tag_block_codepoint(self, codepoint: int) -> None:
        text = f"safe text{chr(codepoint)}"
        with pytest.raises(SecurityRejectedError) as exc_info:
            check_text_unicode_safety(text)
        assert exc_info.value.category is ErrorCategory.SUSPICIOUS_UNICODE
        assert "tag_character" in exc_info.value.detail

    def test_steganography_payload(self) -> None:
        # Tag characters interleaved with prose form a steganography
        # channel encoding "ab" via U+E0061 / U+E0062.
        steg = chr(0xE0061) + chr(0xE0062)
        with pytest.raises(SecurityRejectedError):
            check_text_unicode_safety(f"hello{steg}")


# ── Disallowed control characters rejected ─────────────────────────


class TestControlCharactersRejected:
    @pytest.mark.parametrize(
        ("char", "name"),
        [
            ("\x00", "NUL"),
            ("\x07", "BEL"),
            ("\x08", "BS"),
            ("\x0b", "VT"),
            ("\x0c", "FF"),
            ("\x1b", "ESC"),
            ("\x7f", "DEL"),
            ("\x9b", "CSI"),
        ],
    )
    def test_each_disallowed_control(self, char: str, name: str) -> None:
        with pytest.raises(SecurityRejectedError) as exc_info:
            check_text_unicode_safety(f"text{char}more")
        assert exc_info.value.category is ErrorCategory.SUSPICIOUS_UNICODE
        assert "control_character" in exc_info.value.detail


# ── First-match early exit ─────────────────────────────────────────


class TestFirstMatchEarlyExit:
    def test_first_suspicious_char_reported(self) -> None:
        # Two suspicious chars: zero-width at index 2, bidi at 5.
        # Detail must report the first match, not the second.
        text = "ab​cd‮tail"
        with pytest.raises(SecurityRejectedError) as exc_info:
            check_text_unicode_safety(text)
        assert "index 2" in exc_info.value.detail
        assert "zero_width" in exc_info.value.detail
        assert "bidi_override" not in exc_info.value.detail


# ── Fixture-corpus end-to-end ──────────────────────────────────────


class TestAttackCorpus:
    """Multi-line attack samples loaded from the on-disk corpus."""

    def test_trojan_source_sample(self, fixture_root: Path) -> None:
        text = (fixture_root / "unicode_attacks" / "trojan_source.txt").read_text(
            encoding="utf-8"
        )
        with pytest.raises(SecurityRejectedError) as exc_info:
            check_text_unicode_safety(text)
        assert exc_info.value.category is ErrorCategory.SUSPICIOUS_UNICODE
        assert "bidi_override" in exc_info.value.detail

    def test_zero_width_injection_sample(self, fixture_root: Path) -> None:
        text = (
            fixture_root / "unicode_attacks" / "zero_width_injection.txt"
        ).read_text(encoding="utf-8")
        with pytest.raises(SecurityRejectedError) as exc_info:
            check_text_unicode_safety(text)
        assert "zero_width" in exc_info.value.detail

    def test_clean_ukrainian_sample(self, fixture_root: Path) -> None:
        text = (fixture_root / "clean" / "ukrainian_homework.txt").read_text(
            encoding="utf-8"
        )
        check_text_unicode_safety(text)


# ── NFKC bypass canonicalization ───────────────────────────────────


class TestNFKCBypassThenCheck:
    """Verifies the documented Stage 1 chain: NFKC then unicode check.

    Compatibility-encoded variants of suspicious characters do exist
    (e.g. encoded full-width forms). The Stage 1 contract is that
    callers run :func:`nfkc_for_security` first and then
    :func:`check_text_unicode_safety` on the result. These tests
    verify the chain catches what each step alone would miss.
    """

    def test_full_width_keyword_canonicalises_then_passes_unicode_check(
        self,
    ) -> None:
        # Full-width "ignore" canonicalises to ASCII and contains no
        # suspicious chars. Unicode check passes; regex pre-screen
        # in commit (c) is what catches the keyword.
        compat = "ｉｇｎｏｒｅ"
        canonical = nfkc_for_security(compat)
        check_text_unicode_safety(canonical)
        assert canonical == "ignore"

    def test_zero_width_caught_post_nfkc(self) -> None:
        # NFKC does not strip zero-width; unicode check then catches.
        text = "ig​nore"
        canonical = nfkc_for_security(text)
        with pytest.raises(SecurityRejectedError):
            check_text_unicode_safety(canonical)
