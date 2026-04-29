"""Pytest fixtures for security layer unit tests.

Shared fixtures point at the on-disk attack-vector samples corpus
under ``tests/fixtures/security/``. Tests load specific samples
relative to :data:`fixture_root`; multi-line attack samples live
in the corpus while simple inline cases stay in test bodies for
readability.
"""

from pathlib import Path

import pytest

_FIXTURE_ROOT = Path(__file__).resolve().parent.parent.parent / "fixtures" / "security"


@pytest.fixture
def fixture_root() -> Path:
    """Root path of the security attack-vector samples corpus.

    Tests build specific paths via subdirectory lookup, e.g.::

        sample = fixture_root / "unicode_attacks" / "trojan_source.txt"
        text = sample.read_text(encoding="utf-8")
    """
    return _FIXTURE_ROOT
