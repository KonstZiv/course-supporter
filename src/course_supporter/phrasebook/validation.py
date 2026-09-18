"""The startup check over the phrase files.

Three faults are worth refusing a boot for, and none of them is visible from a
single file: a language the system accepts but has no phrases for, a phrase key
that exists in one language and not in another, and a translation whose
placeholders no longer match the source's. All three would surface as a review
missing a word — or a number — in front of a student, at the moment a student
is reading it, which is exactly the class of fault the path-config and ladder
checks moved to boot time (``DD-SP-AO``, ``DD-SP-AP``).

The placeholder check is the one a machine translation actually trips: a model
asked for Arabic may translate ``{number}`` into a word, move it, or drop it,
and "слайд" without its number reads as a bug the student reports rather than
one we caught. :func:`placeholder_faults` is therefore also called by the
translation script on each language before it writes the file, so a broken
translation never reaches disk.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from pathlib import Path

from course_supporter.phrasebook.loader import SOURCE_LANGUAGE, Phrase, load_phrasebook

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


def placeholders(text: str) -> frozenset[str]:
    """The placeholder names in a phrase, as the assembler will look for them."""
    return frozenset(_PLACEHOLDER.findall(text))


def placeholder_faults(
    source: Mapping[str, Phrase],
    translation: Mapping[str, Phrase],
    *,
    code: str,
) -> list[str]:
    """Where a translation's placeholders differ from the source's, key by key.

    Only keys present on both sides are compared: a key the translation lacks
    is the completeness check's fault, and naming it twice would bury the
    difference this check is for.

    Returns:
        One line per key whose placeholder set differs — empty when the
        translation carries every placeholder through, and no others.
    """
    faults: list[str] = []
    for key in sorted(set(source) & set(translation)):
        wanted = placeholders(source[key].text)
        got = placeholders(translation[key].text)
        if wanted != got:
            faults.append(
                f"Language '{code}', phrase '{key}': placeholders "
                f"{sorted(got) or 'none'} instead of {sorted(wanted) or 'none'}"
            )
    return faults


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
      language carries a key the source does not have;
    * every translated phrase carries the source's placeholders, and only
      those: a machine translation that renames, moves out or drops
      ``{number}`` leaves the assembler with nothing to fill in.

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

    source = book.get(SOURCE_LANGUAGE, {})
    source_keys = set(source)
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
        errors.extend(placeholder_faults(source, book[code], code=code))

    if errors:
        listed = "\n  - ".join(errors)
        msg = f"Phrasebook '{directory}' cannot be used:\n  - {listed}"
        raise ValueError(msg)
