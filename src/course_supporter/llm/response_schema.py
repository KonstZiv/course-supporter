"""Response schemas on the wire (task 09a).

Purpose:
    A stage that wants the provider itself to hold the form of its answer
    builds ONE canonical strict JSON Schema from its pydantic model
    (:func:`strict_json_schema`) and hands it to the router; each connector
    translates it into its vendor's wire form. The OpenAI-compatible shape is
    shared by two connectors (OpenAI-compatible and DashScope), so it lives here
    (:func:`openai_response_format`).

Interface:
    :func:`strict_json_schema` — pydantic model -> canonical strict schema.
    :func:`openai_response_format` — request -> ``response_format`` value or
    ``None`` when the request carries no schema mode.
    :func:`mentions_json` — whether a request's messages contain the word
    "json" (required by OpenAI-compatible JSON mode).

The canonical schema keeps only keywords that BOTH OpenAI strict mode and
Gemini ``response_json_schema`` accept; anything finer (string patterns,
lengths, exclusive bounds) is dropped. Nothing is lost by that: the stage's
own validator (``model_validate_json``) still checks the full model on every
answer, because a schema holds form and never content.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from course_supporter.llm.schemas import SchemaMode

if TYPE_CHECKING:
    from pydantic import BaseModel

    from course_supporter.llm.schemas import LLMRequest

# Keywords common to OpenAI strict mode and Gemini response_json_schema.
# ``const`` is translated to a one-value ``enum`` (Gemini has no ``const``).
_KEPT_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "anyOf",
        "enum",
        "description",
        "format",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
    }
)

_DEFS_PREFIX = "#/$defs/"
_SCHEMA_NAME_DISALLOWED = re.compile(r"[^a-zA-Z0-9_-]")
_JSON_WORD = re.compile("json", re.IGNORECASE)


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The canonical strict JSON Schema of ``model``.

    * ``$defs`` are inlined (Gemini forbids siblings next to ``$ref``, and
      pydantic puts a field's description there);
    * every object gets ``additionalProperties: false`` and lists ALL its
      properties in ``required`` (OpenAI strict mode demands both);
    * ``title`` and ``default`` are dropped.

    Nullability follows the model and nothing else: a field that admits
    ``None`` already carries ``anyOf [..., {"type": "null"}]`` from pydantic and
    keeps it; a field with a non-``None`` default becomes required WITHOUT
    null — the provider must then always write it.

    Raises:
        ValueError: for shapes strict mode cannot express — a recursive model,
            a free-form mapping (``dict[str, X]``), a tuple, an ``allOf`` of
            several schemas.
    """
    raw = model.model_json_schema()
    return _strict(raw, raw.get("$defs", {}), ())


def _strict(
    node: dict[str, Any], defs: dict[str, Any], stack: tuple[str, ...]
) -> dict[str, Any]:
    if "$ref" in node:
        name = str(node["$ref"]).removeprefix(_DEFS_PREFIX)
        if name in stack:
            msg = f"recursive model '{name}' cannot be inlined into a strict schema"
            raise ValueError(msg)
        # The field's own keys (its description) win over the definition's.
        merged = {**defs[name], **{k: v for k, v in node.items() if k != "$ref"}}
        return _strict(merged, defs, (*stack, name))

    all_of = node.get("allOf")
    if all_of is not None:
        if len(all_of) != 1:
            msg = "allOf of several schemas has no strict-mode form"
            raise ValueError(msg)
        rest = {k: v for k, v in node.items() if k != "allOf"}
        return _strict({**all_of[0], **rest}, defs, stack)

    if "prefixItems" in node:
        msg = "tuple (prefixItems) has no strict-mode form"
        raise ValueError(msg)
    extra = node.get("additionalProperties")
    if isinstance(extra, dict):
        msg = "free-form mapping (dict[str, X]) has no strict-mode form"
        raise ValueError(msg)

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "const":
            out["enum"] = [value]
        elif key == "oneOf":
            # Gemini reads oneOf as anyOf; OpenAI strict knows only anyOf.
            out["anyOf"] = [_strict(v, defs, stack) for v in value]
        elif key not in _KEPT_KEYWORDS:
            continue
        elif key == "properties":
            out[key] = {k: _strict(v, defs, stack) for k, v in value.items()}
        elif key == "items":
            out[key] = _strict(value, defs, stack)
        elif key == "anyOf":
            out[key] = [_strict(v, defs, stack) for v in value]
        else:
            out[key] = value

    if out.get("type") == "object" or "properties" in out:
        out["required"] = list(out.get("properties", {}))
        out["additionalProperties"] = False
    return out


def _schema_name(request: LLMRequest) -> str:
    # OpenAI's json_schema.name: ^[a-zA-Z0-9_-]{1,64}$. The stage name is the
    # natural one and already fits; anything else is made to.
    name = _SCHEMA_NAME_DISALLOWED.sub("_", request.action)[:64]
    return name or "response"


def openai_response_format(request: LLMRequest) -> dict[str, Any] | None:
    """``response_format`` in the OpenAI shape, or ``None`` for no schema mode.

    Shared by the OpenAI-compatible connector and DashScope (whose SDK puts
    the kwarg into the request's ``parameters``): ``STRICT`` sends the schema
    with ``strict: true``; ``JSON`` sends ``json_object`` and no schema.
    """
    if request.schema_mode is SchemaMode.STRICT:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": _schema_name(request),
                "strict": True,
                "schema": request.response_schema,
            },
        }
    if request.schema_mode is SchemaMode.JSON:
        return {"type": "json_object"}
    return None


def mentions_json(request: LLMRequest) -> bool:
    """Whether the system or user message contains "json", in any case."""
    messages = (request.system_prompt, request.prompt)
    return any(_JSON_WORD.search(text) for text in messages if text)
