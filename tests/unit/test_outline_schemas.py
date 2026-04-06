"""Tests for MaterialOutline Pydantic schemas."""

import pytest
from pydantic import ValidationError

from course_supporter.models.outline import (
    CodeSnippet,
    MaterialOutline,
    OutlineSection,
    PresenterInfo,
)


def _make_section(**overrides: object) -> dict[str, object]:
    """Build a minimal OutlineSection dict with optional overrides."""
    base: dict[str, object] = {
        "start_sec": 0.0,
        "end_sec": 120.0,
        "title": "Introduction",
        "narration": "Welcome to the course.",
        "screen_content": "Title slide with course name.",
        "code_snippets": [],
        "key_concepts": [],
    }
    base.update(overrides)
    return base


def _make_outline(**overrides: object) -> dict[str, object]:
    """Build a minimal MaterialOutline dict with optional overrides."""
    base: dict[str, object] = {
        "title": "Python Basics",
        "duration_sec": 960.0,
        "language": "uk",
        "source_type": "video",
        "presenter": {
            "description": "Senior developer",
            "style": "conversational",
            "delivery_notes": "Moderate pace, uses humor.",
        },
        "summary": "Introduction to Python fundamentals.",
        "topics": ["variables", "loops", "functions"],
        "tools": ["PyCharm", "Python 3.12"],
        "target_audience": "Beginner developers",
        "prerequisites": [],
        "sections": [_make_section()],
    }
    base.update(overrides)
    return base


class TestPresenterInfo:
    def test_valid(self) -> None:
        info = PresenterInfo(
            description="Expert Python dev",
            style="practical",
            delivery_notes="Fast pace.",
        )
        assert info.description == "Expert Python dev"

    def test_json_roundtrip(self) -> None:
        info = PresenterInfo(
            description="Lecturer",
            style="academic",
            delivery_notes="Slow, detailed.",
        )
        restored = PresenterInfo.model_validate_json(info.model_dump_json())
        assert restored == info


class TestCodeSnippet:
    def test_preserves_code_verbatim(self) -> None:
        """Code field must be preserved exactly, including whitespace."""
        raw_code = "def foo():\n    return 42  # magic"
        snippet = CodeSnippet(
            language="python",
            code=raw_code,
            context="Demonstrates return values.",
        )
        assert snippet.code == raw_code

    def test_json_roundtrip(self) -> None:
        snippet = CodeSnippet(language="js", code="const x = 1;", context="Variable.")
        restored = CodeSnippet.model_validate_json(snippet.model_dump_json())
        assert restored.code == snippet.code


class TestOutlineSection:
    def test_valid_section(self) -> None:
        section = OutlineSection(**_make_section())
        assert section.start_sec == 0.0
        assert section.end_sec == 120.0

    def test_end_must_be_greater_than_start(self) -> None:
        with pytest.raises(ValidationError, match=r"end_sec.*must be greater"):
            OutlineSection(**_make_section(start_sec=100.0, end_sec=50.0))

    def test_equal_times_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"end_sec.*must be greater"):
            OutlineSection(**_make_section(start_sec=100.0, end_sec=100.0))

    def test_empty_code_snippets_ok(self) -> None:
        section = OutlineSection(**_make_section(code_snippets=[]))
        assert section.code_snippets == []

    def test_with_code_snippets(self) -> None:
        snippets = [
            {"language": "python", "code": "x = 1", "context": "Variable assignment."}
        ]
        section = OutlineSection(**_make_section(code_snippets=snippets))
        assert len(section.code_snippets) == 1
        assert section.code_snippets[0].code == "x = 1"

    def test_empty_key_concepts_ok(self) -> None:
        section = OutlineSection(**_make_section(key_concepts=[]))
        assert section.key_concepts == []


class TestMaterialOutline:
    def test_valid_outline(self) -> None:
        outline = MaterialOutline(**_make_outline())
        assert outline.title == "Python Basics"
        assert len(outline.sections) == 1

    def test_json_roundtrip(self) -> None:
        outline = MaterialOutline(**_make_outline())
        json_str = outline.model_dump_json()
        restored = MaterialOutline.model_validate_json(json_str)
        assert restored.title == outline.title
        assert len(restored.sections) == len(outline.sections)
        assert restored.presenter == outline.presenter

    def test_empty_sections_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MaterialOutline(**_make_outline(sections=[]))

    def test_sections_must_be_ordered(self) -> None:
        sections = [
            _make_section(start_sec=200.0, end_sec=300.0, title="Second"),
            _make_section(start_sec=0.0, end_sec=120.0, title="First"),
        ]
        with pytest.raises(ValidationError, match="ordered by start_sec"):
            MaterialOutline(**_make_outline(sections=sections))

    def test_multiple_ordered_sections(self) -> None:
        sections = [
            _make_section(start_sec=0.0, end_sec=120.0, title="Intro"),
            _make_section(start_sec=120.0, end_sec=300.0, title="Variables"),
            _make_section(start_sec=300.0, end_sec=500.0, title="Loops"),
        ]
        outline = MaterialOutline(**_make_outline(sections=sections))
        assert len(outline.sections) == 3

    def test_empty_prerequisites_default(self) -> None:
        data = _make_outline()
        del data["prerequisites"]  # type: ignore[arg-type]
        outline = MaterialOutline(**data)
        assert outline.prerequisites == []
