"""The startup check over the phrase files.

Two faults are worth refusing a boot for, and neither is visible from a single
file: a language the system accepts but has no phrases for, and a phrase key
that exists in one language and not in another. Both would surface as a review
missing a word in front of a student, at the moment a student is reading it —
which is exactly the class of fault the path-config and ladder checks moved to
boot time (``DD-SP-AO``, ``DD-SP-AP``).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path

from course_supporter.phrasebook.loader import SOURCE_LANGUAGE, Phrase, load_phrasebook


def validate_phrasebook(
    directory: Path,
    allowed_codes: Collection[str],
) -> None:
    """Check what a single file's shape cannot, reporting every fault at once.

    * the source language has a file — without it there is nothing to compare
      against, and nothing to fall back on;
    * every allowed language has a file, and every file is an allowed language:
      the set is checked BOTH ways, because a language without phrases falls
      back silently to another language's words, and a file outside the list is
      one nobody will ever read — translated, reviewed and dead;
    * every key of the source is present in every other language, and no
      language carries a key the source does not have.

    Args:
        directory: Where the language files live.
        allowed_codes: The languages the system accepts —
            ``config/languages.yaml`` through ``language.get_language_registry``.

    Raises:
        FileNotFoundError: when the directory does not exist.
        ValueError: listing every fault found.
    """
    book: Mapping[str, Mapping[str, Phrase]] = load_phrasebook(directory)
    errors: list[str] = []

    if SOURCE_LANGUAGE not in book:
        errors.append(
            f"The source language '{SOURCE_LANGUAGE}' has no file in '{directory}': "
            f"there is nothing for the other languages to be compared against"
        )

    allowed = set(allowed_codes)
    present = set(book)
    for code in sorted(allowed - present):
        errors.append(
            f"Language '{code}' is allowed but has no phrase file in '{directory}': "
            f"a review in it would silently be written in another language's words"
        )
    for code in sorted(present - allowed):
        errors.append(
            f"Phrase file '{code}' is not an allowed language: nothing will ever "
            f"read it"
        )

    source_keys = set(book.get(SOURCE_LANGUAGE, {}))
    for code in sorted(present & allowed):
        if code == SOURCE_LANGUAGE:
            continue
        keys = set(book[code])
        for key in sorted(source_keys - keys):
            errors.append(f"Language '{code}' is missing the phrase '{key}'")
        for key in sorted(keys - source_keys):
            errors.append(
                f"Language '{code}' carries the phrase '{key}', which the source "
                f"'{SOURCE_LANGUAGE}' does not have"
            )

    if errors:
        listed = "\n  - ".join(errors)
        msg = f"Phrasebook '{directory}' cannot be used:\n  - {listed}"
        raise ValueError(msg)
