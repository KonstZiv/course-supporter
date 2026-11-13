"""The quote fix of 2026-11-07 leaves two calibrated prompts byte for byte.

The fix of the student's problem 2 — a short whole line is a quote, a "met"
taken back after its quote fell is unconfirmed — is the code's alone: the
evaluation prompt and the explanation prompt were calibrated on live answers,
and the explanation already knows ``quote_not_found``. Their bytes are pinned
here, so a change to either is a decision made on purpose — with a new hash
in this file — and never a side effect.

The explanation prompt v3 (2026-11-13) is pinned beside v2 as it was written:
the stage reads v3 since, v2 stays as the calibrated history it is, and v3's
calibration is the live measurement of v2 against v3 before the merge.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

_PINNED = {
    "prompts/criteria_evaluation/v1.md": (
        "1067450153909de49254dfcd27d752b27d6753a142d04720149c845eb68d24a0"
    ),
    "prompts/review_explanation/v2.md": (
        "315afd0b1e35aa13e9bb133de1702cfa36f6cc05732d04c59a32c6f29a7bf0f3"
    ),
    "prompts/review_explanation/v3.md": (
        "6b1bca3ca00762c5beabe84ca4bc415f16b75630024c8fe6ff76357c362ca435"
    ),
}


@pytest.mark.parametrize(("path", "digest"), sorted(_PINNED.items()))
def test_the_prompt_is_byte_for_byte_the_calibrated_one(path: str, digest: str) -> None:
    data = (_REPO_ROOT / path).read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest, path
