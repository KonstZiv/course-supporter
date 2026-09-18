"""Reading the per-language phrase files.

One file per language, named by its ISO 639-3 code (``ukr.yaml``, ``eng.yaml``,
…), all in one directory. Each file is a flat mapping of phrase key to phrase,
and the key is the same in every language — that is what lets the assembler ask
for ``section.fixed`` and get the student's language back.

A phrase takes one of two shapes::

    section.fixed: Виправлено                 # plain string
    section.open:
      text: Odprte
      reviewed: true                          # a human went over this one

The plain string is what the translation script writes and what it may
overwrite on the next run; the record with ``reviewed: true`` is what a human
has been through, and the script leaves it alone. The source file carries plain
strings only: it is not a translation, so the mark has nothing to say there.

The dictionary does not change while the system runs — it is read at boot,
cached per directory, and the file on disk is the only way to change a phrase
(mentor-rebuild task 04, §2.13 of ``03-BINDING.md``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml

SOURCE_LANGUAGE: Final = "ukr"
"""The language the phrases are written in; every other file derives from it."""

FALLBACK_LANGUAGE: Final = "eng"
"""Read when the review has no language of its own (``language.py`` resolved none)."""

_SUFFIX: Final = ".yaml"

_cache: dict[Path, dict[str, dict[str, Phrase]]] = {}


@dataclass(frozen=True, slots=True)
class Phrase:
    """One phrase, and whether a human has reviewed this wording.

    ``reviewed`` is about translations: ``False`` means the translation script
    wrote it and may write it again. In the source file the question does not
    arise — nothing translates into the language the phrases were written in.
    """

    text: str
    reviewed: bool


def _phrase_from(raw: object, *, key: str, path: Path) -> Phrase:
    if isinstance(raw, str):
        return Phrase(text=raw, reviewed=False)
    if isinstance(raw, dict):
        text = raw.get("text")
        reviewed = raw.get("reviewed", False)
        if not isinstance(text, str) or not isinstance(reviewed, bool):
            msg = (
                f"Phrase '{key}' in '{path}' must carry a string 'text' and a "
                f"boolean 'reviewed'; got {raw!r}"
            )
            raise ValueError(msg)
        return Phrase(text=text, reviewed=reviewed)
    msg = (
        f"Phrase '{key}' in '{path}' must be a string or a record with 'text' "
        f"and 'reviewed'; got {type(raw).__name__}"
    )
    raise ValueError(msg)


def load_language_file(path: Path) -> dict[str, Phrase]:
    """Read one language file.

    Raises:
        FileNotFoundError: when the file does not exist.
        ValueError: when the YAML does not parse, the file is not a mapping of
            phrases, or any phrase has neither shape.
    """
    if not path.exists():
        raise FileNotFoundError(f"Phrase file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse phrase file '{path}': {exc}") from exc
    if not isinstance(raw, dict):
        msg = (
            f"Phrase file '{path}' must be a mapping of phrase key to phrase; "
            f"got {type(raw).__name__}"
        )
        raise ValueError(msg)
    return {
        str(key): _phrase_from(value, key=str(key), path=path)
        for key, value in raw.items()
    }


def _default_directory() -> Path:
    """Resolve the phrasebook directory from settings (lazy import).

    Settings are imported lazily for the same reason ``language.py`` does it:
    ``config.py`` loads at app boot, before this module is needed.
    """
    from course_supporter.config import get_settings

    return get_settings().phrasebook_dir


def load_phrasebook(
    directory: Path | None = None,
) -> Mapping[str, Mapping[str, Phrase]]:
    """Every language file of the directory, keyed by ISO 639-3 code.

    Cached per directory: the dictionary does not change while the system runs,
    so the first read is the only read. Passing a directory explicitly (tests,
    the translation script) re-reads it and refreshes that directory's entry.

    Raises:
        FileNotFoundError: when the directory does not exist.
        ValueError: from :func:`load_language_file` for a file that cannot be
            read as phrases.
    """
    path = directory if directory is not None else _default_directory()
    if directory is None and path in _cache:
        return _cache[path]
    if not path.is_dir():
        raise FileNotFoundError(f"Phrasebook directory not found: {path}")
    book = {
        file.stem: load_language_file(file) for file in sorted(path.glob(f"*{_SUFFIX}"))
    }
    _cache[path] = book
    return book


def phrases_for(code: str | None, directory: Path | None = None) -> Mapping[str, str]:
    """The phrases to write a review in, by phrase key.

    The language of the review when there is one; English when the resolver
    found none (``language.py::resolve_review_language`` returns ``None`` only
    when all three sources are empty or unusable); the source language if even
    English is missing, which a booted system cannot be in — the startup check
    refuses a directory whose files do not match the language list.

    There is deliberately no per-key fallback: the same startup check refuses a
    file that is missing a key of the source, so a key present in one language
    and absent in another cannot reach a running system.
    """
    book = load_phrasebook(directory)
    for candidate in (code, FALLBACK_LANGUAGE, SOURCE_LANGUAGE):
        if candidate is not None and candidate in book:
            return {key: phrase.text for key, phrase in book[candidate].items()}
    msg = (
        f"No phrases to fall back on: neither '{code}', '{FALLBACK_LANGUAGE}' nor "
        f"'{SOURCE_LANGUAGE}' is in the phrasebook"
    )
    raise ValueError(msg)
