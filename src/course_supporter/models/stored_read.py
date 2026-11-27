"""Reading stored JSON with a strict model that forbids fields it does not know.

Purpose:
    The models of a review and of its explanation are strict on purpose:
    ``extra="forbid"`` makes a *writer* name every field it puts in, so a typo
    or a half-finished change cannot reach the database. That is right for
    writing and wrong for reading. A stored row is data that was written once
    and read many times, by code that may be older than the code that wrote
    it: a new release adds a field to every review it stores, production is
    rolled back, and the older code finds a field it has never heard of. The
    strict model then refuses a row whose data is whole, and the student sees
    an empty review, the webhook sends ``passed=False``, and the rule of the
    copy runs a new paid check.

    :func:`read_stored` is the one place that separates the two: it validates
    with the model as it is, and when — and only when — the one thing wrong
    is fields the model does not know, it reads the row without them.

Interface:
    :func:`read_stored` — ``model`` and the stored mapping in, the model out.
    Raises :class:`pydantic.ValidationError` for every defect other than
    unknown fields, so a reader that handles that exception today handles it
    the same way after.

    :func:`read_stored_with` — the same for a stored value that is not one
    model but a list of them, checked as a whole by a ``TypeAdapter`` (a
    task's criteria).

    :func:`problems_without_values` — what a reader writes to its log when a
    stored row does not read: the ``loc`` and the ``type`` of each error, and
    never the input. ``str(error)`` of pydantic holds the value of every field
    that failed, which for a review is the text of the review.

How the fields are found:
    By the ``loc`` of pydantic's ``extra_forbidden`` errors. For every shape
    in the review and explanation models — a nested model, ``Model | None``, a
    list or tuple of models — ``loc`` is the path to the field in the data
    itself (names and list indexes); a test walks each shape and checks it.
    A path that cannot be followed in the data is not guessed at: the row is
    read as it always was, strictly. The row is read without the fields it
    names only when it then validates whole: any other error — one that was
    there beside them, or one that only they hid — refuses the row with the
    error the model itself gives for it.

What is written down:
    One ``WARNING`` line per read that skipped fields: the model, the paths of
    the fields (names and indexes, never a value) and the submission when the
    reader knows it. A value of a stored review is the student's work and the
    Mentor's words about it; it does not belong in a log.

>>> from pydantic import BaseModel, ConfigDict
>>> class Point(BaseModel):
...     model_config = ConfigDict(extra="forbid")
...     x: int
>>> read_stored(Point, {"x": 1, "from_a_newer_release": "kept out of the log"})
Point(x=1)
>>> read_stored(Point, {"x": "one", "from_a_newer_release": 1})
Traceback (most recent call last):
    ...
pydantic_core._pydantic_core.ValidationError: ...
>>> Point.model_validate({"x": 1, "from_a_newer_release": 1})
Traceback (most recent call last):
    ...
pydantic_core._pydantic_core.ValidationError: ...
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import structlog
from pydantic import BaseModel, TypeAdapter, ValidationError

logger = structlog.get_logger(__name__)

_EXTRA_FIELD = "extra_forbidden"

type _Path = tuple[str | int, ...]


def read_stored[Stored: BaseModel](
    model: type[Stored],
    data: Mapping[str, Any],
    *,
    submission_id: uuid.UUID | None = None,
) -> Stored:
    """Read a stored mapping as ``model``, leaving out fields it does not know.

    Args:
        model: The strict model the data was written with.
        data: The stored mapping. Not changed: fields are left out of a copy.
        submission_id: The submission the data belongs to, for the log line,
            when the reader knows it.

    Returns:
        The model, built from ``data`` without the fields it forbids.

    Raises:
        pydantic.ValidationError: Anything wrong other than unknown fields —
            a missing field, a wrong type, a rule of the model broken —
            including a defect that only shows once the unknown fields are
            gone. The same error the model's own ``model_validate`` raises.
    """
    return _read(
        model.model_validate, data, name=model.__name__, submission_id=submission_id
    )


def read_stored_with[Stored](
    adapter: TypeAdapter[Stored],
    data: Mapping[str, Any] | Sequence[Any],
    *,
    name: str,
    submission_id: uuid.UUID | None = None,
) -> Stored:
    """:func:`read_stored` for a stored value an ``adapter`` checks as a whole.

    For a list of strict models with rules over the whole list (a task's
    criteria: how many, ids unique), which no single model holds.

    Args:
        adapter: The adapter the value was written through.
        data: The stored value. Not changed: fields are left out of a copy.
        name: What the value is, for the log line (``"criteria"``).
        submission_id: The submission the data belongs to, when the reader
            knows it.

    Raises:
        pydantic.ValidationError: As :func:`read_stored`.
    """
    return _read(adapter.validate_python, data, name=name, submission_id=submission_id)


def _read[Stored](
    validate: Callable[[Any], Stored],
    data: Mapping[str, Any] | Sequence[Any],
    *,
    name: str,
    submission_id: uuid.UUID | None,
) -> Stored:
    try:
        return validate(data)
    except ValidationError as error:
        found = _without_unknown_fields(data, error)
        if found is None:
            raise
        pruned, paths = found
        try:
            stored = validate(pruned)
        except ValidationError:
            if _only_unknown_fields(error):
                # A defect the unknown fields hid: the error of the copy.
                raise
            # A defect beside them: the error the model gives for the row.
            raise error from None
    log = logger.bind(model=name, fields=[_dotted(path) for path in paths])
    if submission_id is not None:
        log = log.bind(submission_id=str(submission_id))
    log.warning("stored_unknown_fields_ignored")
    return stored


def _only_unknown_fields(error: ValidationError) -> bool:
    """Whether every error of ``error`` is an unknown field."""
    return all(
        item["type"] == _EXTRA_FIELD
        for item in error.errors(
            include_url=False, include_context=False, include_input=False
        )
    )


def _without_unknown_fields(
    data: Mapping[str, Any] | Sequence[Any], error: ValidationError
) -> tuple[Any, list[_Path]] | None:
    """A copy of ``data`` without the fields ``error`` names, and their paths.

    ``None`` when ``error`` names no unknown field, or one is not at the path
    its ``loc`` says (the data does not lead to it): the row is an error as it
    is. Errors of any other kind are not looked at here — whether unknown
    fields are *all* that is wrong is for the caller to find out, by reading
    the copy: some errors are only consequences of the unknown ones (a list
    whose every item has one is also "too short"). The errors are read without
    their input, so no stored value is held here.
    """
    paths: list[_Path] = []
    for item in error.errors(
        include_url=False, include_context=False, include_input=False
    ):
        loc = item["loc"]
        if item["type"] != _EXTRA_FIELD:
            continue
        if not loc or not isinstance(loc[-1], str):
            return None
        paths.append(loc)
    if not paths:
        return None
    pruned: Any = copy.deepcopy(dict(data) if isinstance(data, Mapping) else list(data))
    for *parents, name in paths:
        node: Any = pruned
        try:
            for step in parents:
                node = node[step]
            del node[name]
        except (KeyError, IndexError, TypeError):
            return None
    return pruned, paths


def problems_without_values(error: ValidationError) -> list[dict[str, str]]:
    """What is wrong with a stored row: the place and the kind of each error.

    For a log line. The input of an error — the value that failed — is the
    student's work or the Mentor's words about it, so it is neither asked for
    nor written; ``msg`` and ``ctx`` are left out too, as a rule of a model
    may put a value into its message.

    >>> from pydantic import BaseModel
    >>> class Point(BaseModel):
    ...     x: int
    >>> try:
    ...     Point.model_validate({"x": "not a number"})
    ... except ValidationError as error:
    ...     problems_without_values(error)
    [{'loc': 'x', 'type': 'int_parsing'}]
    """
    return [
        {"loc": ".".join(map(str, item["loc"])) or "<root>", "type": item["type"]}
        for item in error.errors(
            include_url=False, include_context=False, include_input=False
        )
    ]


def error_text_without_values(exc: BaseException) -> str:
    """Short error string without failed field values for ``ValidationError``.

    For a ``ValidationError``, returns ``"ValidationError: "`` followed by
    ``"<loc> <type>"`` pairs from :func:`problems_without_values` separated by
    ``"; "`` — never the input values, which may be the student's work or the
    Mentor's words about it. For any other exception, returns ``str(exc)``.

    >>> from pydantic import BaseModel
    >>> class Point(BaseModel):
    ...     x: int
    >>> try:
    ...     Point.model_validate({"x": "secret-mentor-text"})
    ... except ValidationError as error:
    ...     text = error_text_without_values(error)
    >>> text
    'ValidationError: x int_parsing'
    >>> "secret-mentor-text" in text
    False
    >>> error_text_without_values(RuntimeError("boom"))
    'boom'
    """
    if isinstance(exc, ValidationError):
        parts = "; ".join(
            f"{p['loc']} {p['type']}" for p in problems_without_values(exc)
        )
        return f"ValidationError: {parts}"
    return str(exc)


def _dotted(path: _Path) -> str:
    """``new_remarks.0.item_id``: the names and indexes of a path, nothing else."""
    return ".".join(str(step) for step in path)
