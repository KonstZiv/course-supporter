"""The schema of an agent's answer, as it goes on the wire (mentor-rebuild 09b).

Purpose:
    An agent that hands the router the schema of its answer (task 09a) hands
    the strict schema of its pydantic model WITHOUT the model's descriptions.
    pydantic makes them of the docstrings, which are written for developers —
    task numbers, Sphinx roles — while the one source of instructions for the
    model is the agent's prompt (vision-side, 2026-10-02). Both stages of a
    text task's review — the evaluation of its criteria and the explanation of
    the verdicts — send their answers' schemas this way, so the rule lives
    here once.

Interface:
    :func:`wire_schema` — pydantic model -> the schema the router puts on the
    wire. :func:`without_descriptions` — the same cut on any schema node.

    The cut is the agents' and not ``llm/``'s: the router carries whatever
    schema a stage gives it, and what a stage chooses to tell its model is the
    stage's own business.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from course_supporter.llm.response_schema import strict_json_schema

if TYPE_CHECKING:
    from pydantic import BaseModel


def wire_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The strict schema of ``model`` with no ``description`` at any depth.

    >>> from pydantic import BaseModel
    >>> class Answer(BaseModel):
    ...     '''Developer's notes, task 09b.'''
    ...     verdict: str
    >>> wire_schema(Answer)
    {'properties': {'verdict': {'type': 'string'}}, 'required': ['verdict'], 'type': 'object', 'additionalProperties': False}
    """  # noqa: E501
    schema: dict[str, Any] = without_descriptions(strict_json_schema(model))
    return schema


def without_descriptions(node: Any) -> Any:
    """A copy of a schema node with every ``description`` keyword left out.

    Only the keyword: the names under ``properties`` are the answer's fields,
    and a field called ``description`` stays one.

    >>> without_descriptions(
    ...     {"description": "notes", "properties": {"description": {"type": "string"}}}
    ... )
    {'properties': {'description': {'type': 'string'}}}
    """
    if isinstance(node, list):
        return [without_descriptions(item) for item in node]
    if not isinstance(node, dict):
        return node
    return {
        key: (
            {name: without_descriptions(sub) for name, sub in value.items()}
            if key == "properties"
            else without_descriptions(value)
        )
        for key, value in node.items()
        if key != "description"
    }
