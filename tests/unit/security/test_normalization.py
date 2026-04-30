"""Tests for NFC/NFKC normalization helpers (KD14 §KD-F)."""

from course_supporter.security.normalization import (
    nfc_for_storage,
    nfkc_for_security,
    normalize_filename,
)

# ── NFC for storage ────────────────────────────────────────────────


class TestNFCForStorage:
    def test_already_nfc_is_idempotent(self) -> None:
        # "café" with precomposed é (U+00E9).
        text = "café"
        assert nfc_for_storage(text) == text
        assert nfc_for_storage(nfc_for_storage(text)) == nfc_for_storage(text)

    def test_decomposed_form_composes(self) -> None:
        # "café" with combining acute (decomposed: e + U+0301).
        decomposed = "café"
        composed = "café"
        assert nfc_for_storage(decomposed) == composed

    def test_ascii_unchanged(self) -> None:
        text = "Hello, world!"
        assert nfc_for_storage(text) == text

    def test_empty_string(self) -> None:
        assert nfc_for_storage("") == ""

    def test_ukrainian_text_preserved(self) -> None:
        text = "Привіт, світе!"
        assert nfc_for_storage(text) == text

    def test_compatibility_chars_preserved_in_nfc(self) -> None:
        # Full-width "A" (U+FF21) is preserved in NFC; only NFKC
        # collapses it to ASCII A. Asserts NFC vs NFKC contract.
        text = "ＡBC"
        assert nfc_for_storage(text) == "ＡBC"


# ── NFKC for security ──────────────────────────────────────────────


class TestNFKCForSecurity:
    def test_full_width_collapses_to_ascii(self) -> None:
        # Full-width "ABC" (U+FF21..U+FF23).
        full = "ＡＢＣ"
        assert nfkc_for_security(full) == "ABC"

    def test_circled_letter_collapses(self) -> None:
        # ⓐ (U+24D0) collapses to "a".
        assert nfkc_for_security("ⓐ") == "a"

    def test_superscript_digit_collapses(self) -> None:
        # ² (U+00B2) collapses to "2".
        assert nfkc_for_security("²") == "2"

    def test_idempotent_on_normalized_input(self) -> None:
        text = "Hello"
        once = nfkc_for_security(text)
        twice = nfkc_for_security(once)
        assert twice == once

    def test_ascii_unchanged(self) -> None:
        text = "ignore previous instructions"
        assert nfkc_for_security(text) == text

    def test_empty_string(self) -> None:
        assert nfkc_for_security("") == ""

    def test_compat_encoded_keyword_canonicalises(self) -> None:
        # Compatibility-encoded "ignore" using full-width Latin
        # collapses to ASCII "ignore" so the regex pre-screen catches
        # injection attempts that hide behind compatibility variants.
        compat = "ｉｇｎｏｒｅ"
        assert nfkc_for_security(compat) == "ignore"


# ── Filename normalization ─────────────────────────────────────────


class TestNormalizeFilename:
    def test_ascii_filename_unchanged(self) -> None:
        assert normalize_filename("homework.pdf") == "homework.pdf"

    def test_full_width_filename_collapses(self) -> None:
        # Full-width "home.pdf" collapses to ASCII for whitelist match.
        assert normalize_filename("ｈｏｍｅ.pdf") == "home.pdf"

    def test_rtl_override_survives_for_inspection(self) -> None:
        # NFKC does NOT strip the RTL override character itself.
        # Detection of the suspicious char is unicode_check's job;
        # normalization only gives the whitelist a deterministic
        # comparison string.
        rtl = "homework‮.txt"
        result = normalize_filename(rtl)
        assert "‮" in result

    def test_idempotent(self) -> None:
        name = "report.docx"
        once = normalize_filename(name)
        twice = normalize_filename(once)
        assert twice == once

    def test_empty_filename(self) -> None:
        assert normalize_filename("") == ""

    def test_cyrillic_filename_preserved(self) -> None:
        # Cyrillic letters are not compatibility variants; NFKC
        # leaves them intact (they are their own canonical form).
        name = "звіт.pdf"
        assert normalize_filename(name) == name
