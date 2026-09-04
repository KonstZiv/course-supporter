"""Language allowlist, normalization, and validation (Task 2.4.13).

Single point of language logic for the backend. Two consumers today:

* ``api.schemas`` — Pydantic field validators on ``RootNodeCreateRequest``
  and ``NodeUpdateRequest`` accept any standard description (639-1,
  639-3, or English name) and normalize to canonical 639-3.
* ``api.routes.config`` — ``GET /api/v1/config/languages`` returns the
  allowed set enriched with English names (and native names where
  ``iso639`` exposes them) for the UI selector.

The whitelist lives in ``config/languages.yaml``. The path is injected
from ``Settings.language_registry_path`` so tests can swap fixtures.

Loader pattern mirrors :mod:`course_supporter.api.upload_validation`
(Pydantic model + module-level cache + path-from-settings). Kept as a
flat top-level module per the cross-cutting convention used by
``config.py`` / ``errors.py`` / ``key_pool.py`` — promote to a package
only when a second module joins it.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import iso639
import yaml
from pydantic import BaseModel, Field


class LanguageEntry(BaseModel):
    """One allowed language for the UI selector and validation."""

    code: str = Field(description="Canonical ISO 639-3 three-letter code.")
    name_en: str = Field(description="English name (from iso639 SIL table).")
    name_native: str | None = Field(
        default=None,
        description=(
            "Native-script name when iso639 carries one; None otherwise. "
            "UI may fall back to ``name_en`` when missing."
        ),
    )


class LanguageRegistryConfig(BaseModel):
    """Top-level shape of ``config/languages.yaml``."""

    languages: list[str] = Field(
        description="Flat list of ISO 639-3 codes — the allowed whitelist."
    )


class InvalidLanguageError(ValueError):
    """Raised when input cannot be resolved to any known ISO language."""


class LanguageNotAllowedError(ValueError):
    """Raised when input resolves to a valid ISO language not on the whitelist."""

    def __init__(self, code: str, allowed_count: int) -> None:
        super().__init__(
            f"Language {code!r} is not in the allowed set "
            f"({allowed_count} languages — see config/languages.yaml)."
        )
        self.code = code
        self.allowed_count = allowed_count


_registry: LanguageRegistryConfig | None = None
_allowed_codes: frozenset[str] | None = None
_allowed_entries: list[LanguageEntry] | None = None


def load_language_registry(config_path: Path) -> LanguageRegistryConfig:
    """Load and validate the language whitelist from YAML.

    Raises:
        FileNotFoundError: when ``config_path`` does not exist.
        ValueError: when YAML parsing or Pydantic validation fails.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Language config not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(
            f"Failed to parse language config '{config_path}': {exc}"
        ) from exc
    return LanguageRegistryConfig.model_validate(raw)


def _default_config_path() -> Path:
    """Resolve the language config path from settings (lazy import).

    Settings are imported lazily to avoid a circular import: ``config.py``
    is loaded at app boot, before this module is needed.
    """
    from course_supporter.config import get_settings

    return get_settings().language_registry_path


def get_language_registry(config_path: Path | None = None) -> LanguageRegistryConfig:
    """Return the cached registry; load on first call (or when path overridden)."""
    global _registry, _allowed_codes, _allowed_entries
    if _registry is None or config_path is not None:
        path = config_path if config_path is not None else _default_config_path()
        _registry = load_language_registry(path)
        _allowed_codes = frozenset(_registry.languages)
        _allowed_entries = None  # rebuilt on first list_allowed() call
    return _registry


def _get_allowed_codes() -> frozenset[str]:
    if _allowed_codes is None:
        get_language_registry()
    assert _allowed_codes is not None  # populated by the loader above
    return _allowed_codes


def normalize_and_validate(raw: str) -> str:
    """Resolve any standard language description to its canonical 639-3 code.

    Accepts a 639-1 (two-letter) code, a 639-3 (three-letter) code, or an
    English name (e.g. ``"Ukrainian"``). Returns the canonical 639-3 form.

    Raises:
        InvalidLanguageError: when the input cannot be resolved to any
            known ISO language at all.
        LanguageNotAllowedError: when the input resolves to a valid ISO
            language that is not in the project's whitelist
            (``config/languages.yaml``).
    """
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidLanguageError(f"Empty or non-string language input: {raw!r}")

    candidate = raw.strip()
    lower = candidate.lower()
    # iso639's resolvers are case-sensitive in different ways depending on
    # the lookup (codes are lowercase, names are title-case) — try the
    # likely shapes in order and stop at the first hit. The ``iso639``
    # stubs are absent (see pyproject ``[tool.mypy.overrides]``), so
    # ``Language`` calls return ``Any``; the explicit annotation pins it.
    lang: iso639.Language | None = None
    with contextlib.suppress(iso639.LanguageNotFoundError):
        lang = iso639.Language.from_part3(lower)
    if lang is None:
        with contextlib.suppress(iso639.LanguageNotFoundError):
            lang = iso639.Language.from_part1(lower)
    if lang is None:
        with contextlib.suppress(iso639.LanguageNotFoundError):
            lang = iso639.Language.from_name(candidate)
    if lang is None:
        with contextlib.suppress(iso639.LanguageNotFoundError):
            lang = iso639.Language.from_name(candidate.title())

    if lang is None:
        raise InvalidLanguageError(
            f"Unknown ISO language: {raw!r} (not a 639-1 / 639-3 code or English name)"
        )

    # ``lang.part3`` is typed as ``Any`` (no iso639 stubs); SIL guarantees
    # a 3-letter lowercase string. ``str(...)`` pins the type without a
    # redundant cast and is a no-op on a real string.
    canonical = str(lang.part3)
    allowed = _get_allowed_codes()
    if canonical not in allowed:
        raise LanguageNotAllowedError(canonical, len(allowed))
    return canonical


def display_name(code: str) -> str:
    """Resolve a canonical ISO 639-3 code to its English display name.

    Thin wrapper over :func:`iso639.Language.from_part3`; the returned
    string is the SIL English name (e.g. ``"ukr"`` → ``"Ukrainian"``).

    Used by the Pass 2a render-context (task 2.4.14): the language pin
    inside the prompt is the human-readable English name, not the ISO
    code, because LLMs recognise ``"Ukrainian"`` reliably and the SIL
    639-3 three-letter codes much less so. Distinct from
    :func:`normalize_and_validate` (input → canonical code) and
    :func:`list_allowed` (whitelist enumeration for the UI).

    Precondition: ``code`` MUST be a valid 639-3 code from the project
    whitelist (guaranteed by task 2.4.13 — every rooted course persists
    its ``default_language`` through ``normalize_and_validate``).
    Passing an unknown code raises ``iso639.LanguageNotFoundError``;
    we deliberately do not catch it — feeding an invalid code here
    means the upstream invariant is broken and must be fixed there.
    """
    lang = iso639.Language.from_part3(code)
    return str(lang.name)


@dataclass(frozen=True, slots=True)
class ReviewLanguage:
    """The language a review will be written in, and where it came from.

    ``source`` exists so the choice is visible in the log and in
    acceptance: three sources feed one answer, and "which one won" is the
    only part a reader cannot reconstruct from the code afterwards.
    """

    code: str | None
    source: Literal["explicit", "preferred", "course", "none"]


def resolve_review_language(
    *,
    explicit: str | None,
    preferred: str | None,
    course: str | None,
) -> ReviewLanguage:
    """Pick the review language from the three sources, in one place.

    Order: what the student asked for on this submission, then what the
    student has stored as a standing preference, then the language of the
    course. Course criteria are deliberately outside this: they are cached
    per task and shared by every student, so the first submitter's
    preference must not decide how the criteria read for everyone after.

    Every input is normalized on the way in, so a caller may pass a 639-1
    code, a 639-3 code or an English name and still get the canonical
    639-3 form back. An input that does not resolve is skipped rather than
    raising -- a stale value in a column must not be able to stop a review
    that has two other sources to fall back on. Route input is validated
    at the door instead, where the caller can be told.

    Returns:
        The resolved code with the source that supplied it; ``code`` is
        ``None`` only when all three sources are empty or unusable.

    Before this existed the same question was answered in three places
    with three different answers: the review graph took the explicit value
    or the course, the sanity gate took the explicit value alone and got
    ``None`` whenever the student had not asked (which is always, today --
    no interface sends the field), and the criteria cache took the course.
    """
    for value, source in (
        (explicit, "explicit"),
        (preferred, "preferred"),
        (course, "course"),
    ):
        if not value:
            continue
        try:
            code = normalize_and_validate(value)
        except (InvalidLanguageError, LanguageNotAllowedError):
            continue
        return ReviewLanguage(code=code, source=source)  # type: ignore[arg-type]
    return ReviewLanguage(code=None, source="none")


def list_allowed() -> list[LanguageEntry]:
    """Return the allowed languages enriched with English (and native) names.

    The result is cached after the first call; reset by calling
    ``get_language_registry(path=...)`` with an explicit path (used by
    tests that swap fixtures).
    """
    global _allowed_entries
    if _allowed_entries is not None:
        return _allowed_entries
    registry = get_language_registry()
    entries: list[LanguageEntry] = []
    for code in registry.languages:
        lang = iso639.Language.from_part3(code)
        # iso639 does not always expose a native-script name; fall back
        # to None and let the UI render ``name_en``.
        native = getattr(lang, "native", None) or None
        entries.append(LanguageEntry(code=code, name_en=lang.name, name_native=native))
    _allowed_entries = entries
    return entries
