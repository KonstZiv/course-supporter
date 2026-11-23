"""The styles of the Mentor's joke on a hostile reply (task 12a, K4).

Purpose:
    A reply the safety stage marked hostile — an attempt to change the grade
    or the Mentor's instructions — is answered with a short, friendly joke
    about the attempt, never about the person, and one sentence of the rule:
    only the work is graded. The model never sees what the student wrote; it
    is shown the kind of the attempt, the item and a style of joke. The style
    is chosen by code, never by the model, from a small set the operator
    approves: ``config/reply_humor_styles.yaml``.

Interface:
    :class:`HumorStyle` — one style: its id, a description of it for the
    model, and an example of its spirit.
    :func:`load_humor_styles` — the file, read and checked; a fault in it is
    refused with every fault named at once.
    :func:`get_humor_styles` — the same, cached per process: the set does
    not change while the system runs, and an edit takes effect on the next
    start, like every configuration file of the path.
    :func:`choose_style` — the style of one reply.

The choice:
    The SHA-256 of the reply's comment id (its text, UTF-8), its first eight
    bytes as a big-endian number, modulo the number of styles, in the file's
    order. The same reply gets the same style in every process — Python's own
    ``hash`` of a string is salted per process, so it is not used — and a
    reply is answered once, so an edit of the set never changes what was
    already said.

Replacing:
    Another way of choosing is another :func:`choose_style`; the stage reads
    the style it returns and nothing about how it was chosen.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Final

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

HUMOR_STYLES_PATH: Final = Path("config/reply_humor_styles.yaml")
"""The file of the styles, from the working directory — as a prompt is read."""

_STRICT = ConfigDict(extra="forbid", frozen=True)

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class HumorStyle(BaseModel):
    """One style of the joke.

    Attributes:
        id: A short name in lower case (``odesa``); it reaches the model and
            the logs, never the student.
        description: What the style is, for the model, in English as every
            instruction to a model is.
        example: One joke in the style, in Ukrainian as the operator approved
            it: the model takes its spirit, never its words.
    """

    model_config = _STRICT

    id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    description: _Text
    example: _Text


class HumorStyles(BaseModel):
    """The whole file: at least one style, every id once."""

    model_config = _STRICT

    styles: tuple[HumorStyle, ...] = Field(min_length=1)

    @field_validator("styles")
    @classmethod
    def _ids_unique(cls, styles: tuple[HumorStyle, ...]) -> tuple[HumorStyle, ...]:
        ids = [style.id for style in styles]
        repeated = sorted({sid for sid in ids if ids.count(sid) > 1})
        if repeated:
            msg = f"style ids repeat: {repeated}"
            raise ValueError(msg)
        return styles


def load_humor_styles(path: Path = HUMOR_STYLES_PATH) -> tuple[HumorStyle, ...]:
    """Read the styles and check them.

    Raises:
        FileNotFoundError: The file does not exist.
        ValueError: The YAML does not parse, or a style is missing a field,
            carries an unknown one, has a malformed id, or repeats an id.
    """
    if not path.exists():
        raise FileNotFoundError(f"Humor styles file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse humor styles '{path}': {exc}") from exc
    try:
        return HumorStyles.model_validate(raw).styles
    except ValidationError as exc:
        raise ValueError(f"Invalid humor styles '{path}': {exc}") from exc


_cached: dict[Path, tuple[HumorStyle, ...]] = {}


def get_humor_styles(path: Path = HUMOR_STYLES_PATH) -> tuple[HumorStyle, ...]:
    """The styles, read once per process and path (see the module)."""
    if path not in _cached:
        _cached[path] = load_humor_styles(path)
    return _cached[path]


def choose_style(comment_id: str, styles: Sequence[HumorStyle]) -> HumorStyle:
    """The style of the reply ``comment_id`` (see "The choice" in the module).

    >>> styles = [
    ...     HumorStyle(id="a", description="d", example="e"),
    ...     HumorStyle(id="b", description="d", example="e"),
    ... ]
    >>> choose_style("s1", styles) == choose_style("s1", styles)
    True

    Raises:
        ValueError: ``styles`` is empty.
    """
    if not styles:
        msg = "no humor style to choose from"
        raise ValueError(msg)
    digest = hashlib.sha256(comment_id.encode("utf-8")).digest()
    return styles[int.from_bytes(digest[:8], "big") % len(styles)]
