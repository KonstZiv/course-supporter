"""Tests for ``course_supporter.language`` (Task 2.4.13)."""

from __future__ import annotations

import pytest

from course_supporter.language import (
    InvalidLanguageError,
    LanguageNotAllowedError,
    list_allowed,
    normalize_and_validate,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("uk", "ukr"),
        ("UK", "ukr"),  # case-insensitive 639-1
        ("ukr", "ukr"),  # already 639-3
        ("UKR", "ukr"),
        ("Ukrainian", "ukr"),  # English name
        ("ukrainian", "ukr"),  # name, lowercased
        ("en", "eng"),
        ("eng", "eng"),
        ("English", "eng"),
        ("yue", "yue"),  # Cantonese — no 639-1; only 639-3 form exists
    ],
)
def test_normalize_happy_path(raw: str, expected: str) -> None:
    assert normalize_and_validate(raw) == expected


@pytest.mark.parametrize("bad", ["xyz", "", "  ", "not-a-language", "12"])
def test_normalize_invalid_raises_invalid_language_error(bad: str) -> None:
    with pytest.raises(InvalidLanguageError):
        normalize_and_validate(bad)


def test_normalize_valid_iso_but_not_in_whitelist() -> None:
    # Latin (lat) is a valid ISO 639-3 language not in our whitelist.
    with pytest.raises(LanguageNotAllowedError) as exc_info:
        normalize_and_validate("lat")
    assert exc_info.value.code == "lat"
    assert exc_info.value.allowed_count == 57


def test_chinese_macrolanguage_zh_is_rejected() -> None:
    # ``zh`` (Chinese macrolanguage) resolves to ``zho`` per iso639 — but
    # the whitelist only carries the specific variant ``cmn`` (Mandarin).
    # Authors must pick the variant explicitly; macrolanguage is not allowed.
    with pytest.raises(LanguageNotAllowedError) as exc_info:
        normalize_and_validate("zh")
    assert exc_info.value.code == "zho"


def test_list_allowed_returns_57_entries_with_english_names() -> None:
    entries = list_allowed()
    assert len(entries) == 57

    codes = [e.code for e in entries]
    # Every code must be unique 3-letter lowercase.
    assert len(set(codes)) == 57
    for code in codes:
        assert len(code) == 3
        assert code == code.lower()

    # English names are always present (iso639 SIL table).
    for entry in entries:
        assert entry.name_en
        assert isinstance(entry.name_en, str)

    # Sanity: Ukrainian is in the list with the expected English name.
    by_code = {e.code: e for e in entries}
    assert by_code["ukr"].name_en == "Ukrainian"
    assert by_code["eng"].name_en == "English"
    # Cantonese is in (no 639-1 — only reachable via 639-3 form).
    assert "yue" in by_code


def test_list_allowed_is_cached_across_calls() -> None:
    first = list_allowed()
    second = list_allowed()
    # Same Python object → cache hit.
    assert first is second
