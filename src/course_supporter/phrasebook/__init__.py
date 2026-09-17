"""Phrase templates per language — the words a review is assembled from.

The model writes the remarks and the answers; every label, section heading and
service phrase around them comes from here, in the student's language
(``03-BINDING.md`` §2.13). One file per language, one phrase key for all of
them, the Ukrainian file the source the rest derive from.

Interface:
    :func:`phrases_for` — the phrases to write a review in, by phrase key. This
    is what the assembler calls; it never opens a file itself.
    :func:`load_phrasebook` — every language file of a directory, cached.
    :func:`load_language_file` — one file, for the translation script.
    :func:`validate_phrasebook` — the startup check: the language set matches
    the allowed list both ways, and every language carries every key.

Replacing the store: the loader is the only place that knows the files are
YAML in a directory. A different store (a table, a bundle) replaces
``load_phrasebook`` and leaves :func:`phrases_for` and its callers untouched.
"""

from course_supporter.phrasebook.loader import (
    FALLBACK_LANGUAGE,
    SOURCE_LANGUAGE,
    Phrase,
    load_language_file,
    load_phrasebook,
    phrases_for,
)
from course_supporter.phrasebook.validation import validate_phrasebook

__all__ = [
    "FALLBACK_LANGUAGE",
    "SOURCE_LANGUAGE",
    "Phrase",
    "load_language_file",
    "load_phrasebook",
    "phrases_for",
    "validate_phrasebook",
]
