"""Task 09a: the canonical strict schema (``strict_json_schema``).

The lock that matters: the helper's output is accepted by BOTH vendors, read
from the SDKs pinned in uv.lock rather than from memory —

* OpenAI: it is a fixed point of the SDK's own strict-mode normaliser
  (``openai.lib._pydantic._ensure_strict_json_schema``): nothing left for the
  SDK to add, inline or strip;
* Gemini: every keyword it uses is in the allow-list the google-genai SDK
  documents on ``GenerateContentConfig.response_json_schema``.
"""

from __future__ import annotations

import copy
import re
from enum import StrEnum
from typing import Any, Literal

import pytest
from google.genai import types
from openai.lib._pydantic import _ensure_strict_json_schema
from pydantic import BaseModel, ConfigDict, Field

from course_supporter.llm.response_schema import strict_json_schema


class _Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class _Point(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="criterion point id")
    met: bool


class _Answer(BaseModel):
    verdict: _Verdict
    kind: Literal["review"]
    score: int = Field(ge=0, le=10)
    summary: str = Field(default="", min_length=0, max_length=500)
    note: str | None = None
    points: list[_Point] = Field(description="one per point", min_length=1)
    main: _Point = Field(description="the deciding point")
    extra: _Point | None = None


def _gemini_allowed_keywords() -> frozenset[str]:
    doc = types.GenerateContentConfig.model_fields["response_json_schema"].description
    assert doc is not None
    found = frozenset(re.findall(r"-\s+`([^`]+)`", doc))
    assert {"type", "properties", "anyOf", "additionalProperties"} <= found
    return found | {"propertyOrdering"}


def _nodes(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Every schema node: the root and everything under properties/items/anyOf."""
    out = [schema]
    for sub in schema.get("properties", {}).values():
        out += _nodes(sub)
    if isinstance(schema.get("items"), dict):
        out += _nodes(schema["items"])
    for sub in schema.get("anyOf", []):
        out += _nodes(sub)
    return out


@pytest.fixture
def schema() -> dict[str, Any]:
    return strict_json_schema(_Answer)


class TestVendorsAccept:
    def test_openai_strict_normaliser_has_nothing_to_change(
        self, schema: dict[str, Any]
    ) -> None:
        normalised = _ensure_strict_json_schema(
            copy.deepcopy(schema), path=(), root=copy.deepcopy(schema)
        )
        assert normalised == schema

    def test_only_gemini_keywords(self, schema: dict[str, Any]) -> None:
        allowed = _gemini_allowed_keywords()
        used = {key for node in _nodes(schema) for key in node}
        assert used <= allowed, used - allowed


class TestShape:
    def test_no_refs_titles_or_defaults(self, schema: dict[str, Any]) -> None:
        for node in _nodes(schema):
            assert not {"$ref", "$defs", "title", "default"} & set(node)

    def test_every_object_is_closed_and_fully_required(
        self, schema: dict[str, Any]
    ) -> None:
        objects = [n for n in _nodes(schema) if n.get("type") == "object"]
        assert len(objects) == 4  # root, points item, main, extra variant
        for node in objects:
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])

    def test_null_only_where_the_model_admits_none(
        self, schema: dict[str, Any]
    ) -> None:
        props = schema["properties"]
        assert {"type": "null"} in props["note"]["anyOf"]
        assert {"type": "null"} in props["extra"]["anyOf"]
        # A non-None default makes the field required, never nullable.
        assert props["summary"] == {"type": "string"}
        assert "anyOf" not in props["score"]

    def test_inlined_definition_keeps_the_fields_description(
        self, schema: dict[str, Any]
    ) -> None:
        main = schema["properties"]["main"]
        assert main["description"] == "the deciding point"
        assert main["properties"]["id"] == {
            "type": "string",
            "description": "criterion point id",
        }

    def test_enums_and_literals(self, schema: dict[str, Any]) -> None:
        props = schema["properties"]
        assert props["verdict"] == {"type": "string", "enum": ["pass", "fail"]}
        assert props["kind"] == {"type": "string", "enum": ["review"]}

    def test_shared_bounds_kept(self, schema: dict[str, Any]) -> None:
        assert schema["properties"]["score"] == {
            "type": "integer",
            "minimum": 0,
            "maximum": 10,
        }
        assert schema["properties"]["points"]["minItems"] == 1


class _Mapping(BaseModel):
    values: dict[str, int]


class _Node(BaseModel):
    children: list[_Node]


class _Pair(BaseModel):
    pair: tuple[int, str]


class TestRefused:
    @pytest.mark.parametrize(
        ("model", "match"),
        [(_Mapping, "mapping"), (_Node, "recursive"), (_Pair, "tuple")],
    )
    def test_shapes_strict_mode_cannot_express(
        self, model: type[BaseModel], match: str
    ) -> None:
        with pytest.raises(ValueError, match=match):
            strict_json_schema(model)
