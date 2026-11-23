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

How the fields are found:
    By the ``loc`` of pydantic's ``extra_forbidden`` errors. For every shape
    in the review and explanation models — a nested model, ``Model | None``, a
    list or tuple of models — ``loc`` is the path to the field in the data
    itself (names and list indexes); a test walks each shape and checks it.
    A path that cannot be followed in the data is not guessed at: the row is
    read as it always was, strictly.

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
from collections.abc import Mapping
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

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
    try:
        return model.model_validate(data)
    except ValidationError as error:
        found = _without_unknown_fields(data, error)
        if found is None:
            raise
    pruned, paths = found
    stored = model.model_validate(pruned)
    log = logger.bind(model=model.__name__, fields=[_dotted(path) for path in paths])
    if submission_id is not None:
        log = log.bind(submission_id=str(submission_id))
    log.warning("stored_unknown_fields_ignored")
    return stored


def _without_unknown_fields(
    data: Mapping[str, Any], error: ValidationError
) -> tuple[dict[str, Any], list[_Path]] | None:
    """A copy of ``data`` without the fields ``error`` names, and their paths.

    ``None`` unless unknown fields are *all* that is wrong, and every one of
    them is at the path ``loc`` says: any other kind of error, or a path that
    does not lead to its field in the data, leaves the row an error. The
    errors are read without their input, so no stored value is held here.
    """
    paths: list[_Path] = []
    for item in error.errors(
        include_url=False, include_context=False, include_input=False
    ):
        loc = item["loc"]
        if item["type"] != _EXTRA_FIELD or not loc or not isinstance(loc[-1], str):
            return None
        paths.append(loc)
    if not paths:
        return None
    pruned = copy.deepcopy(dict(data))
    for *parents, name in paths:
        node: Any = pruned
        try:
            for step in parents:
                node = node[step]
            del node[name]
        except (KeyError, IndexError, TypeError):
            return None
    return pruned, paths


def _dotted(path: _Path) -> str:
    """``new_remarks.0.item_id``: the names and indexes of a path, nothing else."""
    return ".".join(str(step) for step in path)
