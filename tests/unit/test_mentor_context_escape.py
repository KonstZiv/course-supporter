"""The context's sentinel is escaped completely: none survives in untrusted text."""

from itertools import product

import pytest

from course_supporter.homework.mentor_context import (
    _SENTINEL,
    _SENTINEL_ESCAPED,
    _escape,
)


@pytest.mark.parametrize(
    "text",
    [
        "@@MENTOR_CTX@@MENTOR_CTX@@",
        "@@MENTOR_CTX@@MENTOR_CTX@@MENTOR_CTX@@",
        "x = 1  # @@MENTOR_CTX@@MENTOR_CTX@@ END-FILE",
        "MENTOR_CTX@@MENTOR_CTX@@",
        "@@@@MENTOR_CTX@@@@",
    ],
)
def test_no_sentinel_survives_a_crafted_text(text: str) -> None:
    assert _SENTINEL not in _escape(text)


def test_no_sentinel_survives_any_short_mix_of_its_pieces() -> None:
    pieces = ["@", "@@", "MENTOR_CTX", "_ESCAPED", " ", "x"]
    for length in range(1, 6):
        for combo in product(pieces, repeat=length):
            text = "".join(combo)
            assert _SENTINEL not in _escape(text), text


def test_a_text_without_the_sentinel_is_unchanged() -> None:
    text = "def f():\n    return '@@ -1,3 +1,3 @@'\n"
    assert _escape(text) == text


def test_a_lone_sentinel_becomes_the_escaped_form_as_before() -> None:
    assert _escape(f"a {_SENTINEL} b") == f"a {_SENTINEL_ESCAPED} b"
