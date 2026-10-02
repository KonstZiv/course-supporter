"""The schema of an agent's answer on the wire (mentor-rebuild task 09b).

One cut for both stages of a text task's review: no ``description`` keyword
at any depth — the prompt is the one source of instructions for the model
(vision-side, 2026-10-02) — and a field that happens to be called
``description`` stays a field. Each agent's own test checks that its schema
is this cut of its answer.
"""

from __future__ import annotations

import doctest
from typing import Any

from pydantic import BaseModel

from course_supporter.agents import wire_schema as wire_schema_module
from course_supporter.agents.wire_schema import wire_schema, without_descriptions
from course_supporter.llm.response_schema import strict_json_schema


def _keywords(node: Any, found: set[str] | None = None) -> set[str]:
    """Every keyword a schema uses at any depth; the names of fields are not."""
    found = set() if found is None else found
    if isinstance(node, list):
        for item in node:
            _keywords(item, found)
    elif isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            for sub in value.values() if key == "properties" else [value]:
                _keywords(sub, found)
    return found


class _Inner(BaseModel):
    """Developer's notes on the inner object."""

    note: str


class _Outer(BaseModel):
    """Developer's notes, task 09b, :func:`something`."""

    inner: list[_Inner]
    other: _Inner | None


class TestTheCut:
    def test_no_description_is_left_at_any_depth(self) -> None:
        strict = strict_json_schema(_Outer)
        # The premise: pydantic does put the docstrings there.
        assert "description" in strict
        assert "description" in strict["properties"]["inner"]["items"]

        assert "description" not in _keywords(wire_schema(_Outer))

    def test_the_form_itself_is_kept(self) -> None:
        strict = strict_json_schema(_Outer)

        assert wire_schema(_Outer) == without_descriptions(strict)
        assert _keywords(wire_schema(_Outer)) == _keywords(strict) - {"description"}

    def test_a_field_named_description_stays_a_field(self) -> None:
        class _Note(BaseModel):
            """A docstring that must not reach the wire."""

            description: str

        stripped = wire_schema(_Note)

        assert stripped["properties"] == {"description": {"type": "string"}}
        assert stripped["required"] == ["description"]
        assert "description" not in _keywords(stripped)

    def test_the_input_is_not_changed(self) -> None:
        strict = strict_json_schema(_Outer)
        before = repr(strict)

        without_descriptions(strict)

        assert repr(strict) == before


def test_the_examples_in_the_docstrings_hold() -> None:
    """The gate does not collect doctests; this runs them."""
    result = doctest.testmod(wire_schema_module)

    assert result.attempted > 0
    assert result.failed == 0
