"""Markdown prompt loader for the KD16 stage router.

Reads multi-section markdown prompt files into a :class:`StagePrompt`
holding raw template strings, then renders them with Jinja2 on
demand.

Section format::

    ## System
    You are a helpful assistant for {{ subject }}.

    ## User
    {{ question }}

* Sections are demarcated by lines that start with ``## RoleName``
  at column 0 (a single ``##`` followed by whitespace and a token).
* Recognised roles: ``system``, ``user``, ``assistant``
  (case-insensitive).
* Unknown role headers and their content are dropped, but the
  parser emits a ``prompt_loader_unknown_role`` WARNING the first
  time it sees a given ``(prompt_ref, role)`` pair so typos like
  ``## Asssistant`` surface in observability instead of silently
  swallowing the section. Editorial sections like ``## Examples``
  produce one warning per process lifetime per file. The dedup
  state lives in module-local ``_warned_unknown_roles``; in
  multi-worker deployments (gunicorn with N workers) each worker
  warns independently on the first hit, so up to N warnings per
  editorial section per app instance is expected -- not a defect.

Parsing and rendering are split deliberately:

* :func:`load_prompt` returns a :class:`StagePrompt` with raw
  template strings (so call sites can mock loading without touching
  Jinja2 internals).
* :meth:`StagePrompt.render` returns a new :class:`StagePrompt` with
  Jinja2-rendered fields. ``None`` fields stay ``None``; empty
  strings stay empty.

Slot lock (task 11, decision 8). A prompt holds untrusted text in data slots
-- a tag alone on its line, ``<submission>`` … ``</submission>``. No value
rendered into a template may open or close one of THAT template's slots: every
``{{ … }}`` output is finalised through :func:`lock_slots`, which turns the
``<`` of such a tag into ``&lt;``. One place, so both roads and every stage
get it; the values themselves are never touched (only the rendered copy), and
JSON from ``tojson_unicode`` arrives with ``<`` already escaped, so it passes
through unchanged rather than being converted twice.

Coexists with the legacy :mod:`course_supporter.agents.prompt_loader`
(YAML-based, different package); that loader is retired together with the
legacy router — undated, tracked as DD-3.2.3-pre-A.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
from jinja2 import Environment, StrictUndefined

from course_supporter.llm.error_categories import InvalidPromptError
from course_supporter.llm.prompt_json import prompt_json

logger = structlog.get_logger()

_HEADER_PATTERN = re.compile(r"^##\s+(\S+)\s*$", re.MULTILINE)
_RECOGNISED_ROLES: frozenset[str] = frozenset({"system", "user", "assistant"})

# Dedup state for unknown-role warnings. Process-local set of
# ``(prompt_ref, role)`` pairs that have already produced a WARNING.
# Each Python process (e.g. each gunicorn worker) maintains its own
# set, so a multi-worker deployment will emit up to N warnings per
# editorial section -- acceptable observability cost vs. the typo-
# detection benefit. Cleared on process restart, which is fine
# because prompt files are static between deploys (KD16).
_warned_unknown_roles: set[tuple[str, str]] = set()


def _tojson_unicode(value: Any) -> str:
    """JSON for a prompt, by the rule of :func:`prompt_json`.

    Letters, the apostrophe and ``&`` stay as they are; only ``<`` and
    ``>`` are escaped. ``sort_keys=True`` and the default separators keep
    the shape Jinja2's own ``tojson`` gave these templates. Jinja2's
    ``tojson`` is untouched: it escapes every non-ASCII letter, and a test
    keeps it out of ``prompts/``. Autoescape is off, so a plain ``str`` is
    rendered as is.
    """
    return prompt_json(value, sort_keys=True)


# A data slot is a tag ALONE on its line: ``<submission>`` opening it and
# ``</submission>`` closing it. The instructions of a prompt name their slots
# in running text (``between the `<submission>` tags``); those mentions are not
# on a line of their own, so they are never taken for a slot.
_SLOT_OPEN = re.compile(r"^[ \t]*<([A-Za-z_][\w-]*)>[ \t]*$", re.MULTILINE)
_SLOT_CLOSE = re.compile(r"^[ \t]*</([A-Za-z_][\w-]*)>[ \t]*$", re.MULTILINE)


def slot_names(*templates: str | None) -> frozenset[str]:
    """The data slots of a prompt: tags opened AND closed alone on a line."""
    text = "\n".join(t for t in templates if t)
    opened = set(_SLOT_OPEN.findall(text))
    closed = set(_SLOT_CLOSE.findall(text))
    return frozenset(opened & closed)


def lock_slots(text: str, slots: frozenset[str]) -> str:
    """Neutralise every opening or closing tag of ``slots`` inside ``text``.

    ``</submission>`` becomes ``&lt;/submission>`` -- still readable, no longer
    a tag. Case and inner whitespace are matched too (``< / Submission >``),
    since a model reads those as the same tag.
    """
    if not slots or "<" not in text:
        return text
    return _slot_pattern(slots).sub(lambda m: "&lt;" + m.group(0)[1:], text)


@lru_cache(maxsize=64)
def _slot_pattern(slots: frozenset[str]) -> re.Pattern[str]:
    names = "|".join(re.escape(name) for name in sorted(slots))
    return re.compile(rf"<\s*/?\s*(?:{names})(?![\w-])", re.IGNORECASE)


def _finalizer(slots: frozenset[str]) -> Callable[[Any], Any]:
    def finalize(value: Any) -> Any:
        return lock_slots(value, slots) if isinstance(value, str) else value

    return finalize


# Same options as a bare ``Template(text, undefined=StrictUndefined)``,
# plus the opt-in filter above and the slot lock. ``from_string`` compiles
# each call's template anew, so nothing but the filter table and the
# finaliser is shared. Prompts are plain text for a model, not HTML, so
# autoescape stays off as before.
@lru_cache(maxsize=64)
def _jinja_env(slots: frozenset[str]) -> Environment:
    env = Environment(  # noqa: S701
        undefined=StrictUndefined, finalize=_finalizer(slots)
    )
    env.filters["tojson_unicode"] = _tojson_unicode
    return env


@dataclass(frozen=True, slots=True)
class StagePrompt:
    """Parsed markdown prompt for a single stage call.

    Attributes:
        system: System-role template (or rendered text), or ``None``
            if the source file has no ``## System`` section.
        user: User-role template, or ``None`` if absent.
        assistant: Assistant-role template (e.g. for few-shot
            seeding), or ``None`` if absent.

    Empty strings indicate a present-but-empty section; ``None``
    indicates the section header was absent in the source file.
    """

    system: str | None = None
    user: str | None = None
    assistant: str | None = None

    def render(self, **context: Any) -> StagePrompt:
        """Render Jinja2 placeholders in each section.

        Returns:
            A new :class:`StagePrompt` whose populated fields hold
            rendered text. ``None`` fields stay ``None``; empty
            strings stay empty.

        Every ``{{ … }}`` output is slot-locked against the slots of the
        whole prompt (all three sections): a value may not open or close
        any of them (module docstring).

        Raises:
            jinja2.UndefinedError: if a template references a
                variable absent from ``context`` (StrictUndefined).
        """
        env = _jinja_env(slot_names(self.system, self.user, self.assistant))
        return replace(
            self,
            system=_render(env, self.system, context),
            user=_render(env, self.user, context),
            assistant=_render(env, self.assistant, context),
        )

    def content_hash(self) -> str:
        """SHA-256 hex of the three model-facing sections.

        Call it on the TEMPLATE, as :func:`load_prompt` returns it, before
        :meth:`render`: the result then identifies the prompt version, which
        is what the call register stores next to ``prompt_ref``. A file path
        is not a version — prompt files are edited in place without a rename.

        Editorial sections the loader drops (e.g. ``## Examples``) never reach
        the model, so they do not change the hash; ``None`` (section absent)
        and ``""`` (section present but empty) hash differently.
        """
        canonical = json.dumps(
            {"system": self.system, "user": self.user, "assistant": self.assistant},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _render(env: Environment, text: str | None, context: dict[str, Any]) -> str | None:
    """Render a single section's template.

    Returns the input unchanged when there is nothing to render
    (``None`` or empty string) so trivial sections do not pay the
    Jinja2 round-trip cost.
    """
    if not text:
        return text
    return env.from_string(text).render(**context)


def load_prompt(
    prompt_ref: str,
    *,
    base_path: Path | None = None,
) -> StagePrompt:
    """Load and parse a markdown prompt file.

    Args:
        prompt_ref: Path to the prompt file, resolved relative to
            ``base_path``. Production wiring uses CWD-relative paths
            from :class:`StageConfig.prompt_ref`.
        base_path: Optional override for the resolution base. Tests
            point this at ``tmp_path``; production passes ``None``
            so :class:`Path` defaults to the current directory.

    Returns:
        :class:`StagePrompt` with raw template strings (NOT yet
        rendered). Sections absent from the file are returned as
        ``None``.

    Raises:
        FileNotFoundError: if the resolved path does not exist.
        InvalidPromptError: if the file has non-whitespace content
            before the first ``## RoleName`` header, or has no
            recognised role sections at all.
    """
    base = base_path if base_path is not None else Path()
    path = base / prompt_ref
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")

    raw = path.read_text(encoding="utf-8")
    sections = _split_sections(prompt_ref, raw)

    return StagePrompt(
        system=sections.get("system"),
        user=sections.get("user"),
        assistant=sections.get("assistant"),
    )


def _warn_unknown_role(prompt_ref: str, role: str) -> None:
    """Emit a WARNING for an unrecognised role header (deduplicated).

    Same ``(prompt_ref, role)`` pair only warns once per process to
    keep editorial sections like ``## Examples`` from spamming logs
    on every per-call ``load_prompt``.
    """
    key = (prompt_ref, role)
    if key in _warned_unknown_roles:
        return
    _warned_unknown_roles.add(key)
    logger.warning(
        "prompt_loader_unknown_role",
        prompt_ref=prompt_ref,
        unknown_role=role,
        recognised_roles=sorted(_RECOGNISED_ROLES),
    )


def _split_sections(prompt_ref: str, raw: str) -> dict[str, str]:
    """Split markdown into ``{role: body}`` for recognised roles.

    Unknown role headers are dropped. Whitespace before the first
    header is allowed; any non-whitespace prologue raises
    :class:`InvalidPromptError`.
    """
    matches = list(_HEADER_PATTERN.finditer(raw))

    first_start = matches[0].start() if matches else len(raw)
    if raw[:first_start].strip():
        raise InvalidPromptError(
            prompt_ref,
            "content before first '## RoleName' header is not allowed",
        )

    sections: dict[str, str] = {}
    for i, match in enumerate(matches):
        role = match.group(1).lower()
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[body_start:body_end].strip()
        if role in _RECOGNISED_ROLES:
            sections[role] = body
        else:
            _warn_unknown_role(prompt_ref, role)

    if not sections:
        raise InvalidPromptError(
            prompt_ref,
            "no recognised role sections (## System / ## User / ## Assistant)",
        )

    return sections
