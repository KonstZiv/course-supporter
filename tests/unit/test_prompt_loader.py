"""Tests for prompt loading and formatting utilities."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from course_supporter.agents.prompt_loader import (
    PromptData,
    format_user_prompt,
    load_prompt,
)


@pytest.fixture()
def valid_prompt_file(tmp_path: Path) -> Path:
    """Create a valid prompt YAML file."""
    data = {
        "version": "v1",
        "system_prompt": "You are a course architect.",
        "user_prompt_template": "Materials:\n{context}\nGenerate structure.",
    }
    path = tmp_path / "prompt.yaml"
    path.write_text(yaml.dump(data))
    return path


class TestLoadPrompt:
    def test_load_valid_prompt(self, valid_prompt_file: Path) -> None:
        """Loads YAML and returns PromptData with required fields."""
        data = load_prompt(valid_prompt_file)
        assert isinstance(data, PromptData)
        assert data.system_prompt == "You are a course architect."
        assert "Materials:" in data.user_prompt_template
        assert data.version == "v1"

    def test_load_missing_file(self, tmp_path: Path) -> None:
        """Raises FileNotFoundError for non-existent file."""
        with pytest.raises(FileNotFoundError):
            load_prompt(tmp_path / "nonexistent.yaml")

    def test_load_missing_system_prompt(self, tmp_path: Path) -> None:
        """Raises ValidationError when system_prompt is missing."""
        path = tmp_path / "bad.yaml"
        path.write_text(yaml.dump({"user_prompt_template": "test"}))
        with pytest.raises(ValidationError):
            load_prompt(path)

    def test_load_missing_user_template(self, tmp_path: Path) -> None:
        """Raises ValidationError when user_prompt_template is missing."""
        path = tmp_path / "bad.yaml"
        path.write_text(yaml.dump({"system_prompt": "test"}))
        with pytest.raises(ValidationError):
            load_prompt(path)

    def test_load_accepts_string_path(self, valid_prompt_file: Path) -> None:
        """Accepts str path in addition to Path objects."""
        data = load_prompt(str(valid_prompt_file))
        assert isinstance(data, PromptData)

    def test_load_default_version(self, tmp_path: Path) -> None:
        """Uses 'unknown' when version key is absent."""
        path = tmp_path / "no_version.yaml"
        path.write_text(
            yaml.dump({"system_prompt": "sys", "user_prompt_template": "usr {context}"})
        )
        data = load_prompt(path)
        assert data.version == "unknown"


class TestFormatUserPrompt:
    def test_format_injects_context(self) -> None:
        """Replaces {context} placeholder with actual context."""
        template = "Materials:\n{context}\nDone."
        result = format_user_prompt(template, "video transcript here")
        assert "video transcript here" in result
        assert "{context}" not in result

    def test_format_preserves_template_text(self) -> None:
        """Non-placeholder text is preserved."""
        template = "Analyze:\n{context}\nReturn JSON."
        result = format_user_prompt(template, "data")
        assert result.startswith("Analyze:")
        assert "Return JSON." in result

    def test_format_empty_context(self) -> None:
        """Works with empty context string."""
        template = "Context: {context}"
        result = format_user_prompt(template, "")
        assert result == "Context: "


class TestFormatUserPromptKwargs:
    def test_extra_kwargs_substituted(self) -> None:
        """Extra kwargs are substituted in the template."""
        template = "Structure:\n{existing_structure}\nMaterials:\n{context}"
        result = format_user_prompt(
            template, "video transcript", existing_structure="Module 1 > Lesson 1"
        )
        assert "Module 1 > Lesson 1" in result
        assert "video transcript" in result
        assert "{existing_structure}" not in result

    def test_missing_kwarg_raises_key_error(self) -> None:
        """Missing kwarg raises KeyError."""
        template = "Structure:\n{existing_structure}\nMaterials:\n{context}"
        with pytest.raises(KeyError):
            format_user_prompt(template, "data")


class TestPromptFileContent:
    def test_v1_prompt_loads_successfully(self) -> None:
        """The actual v1.yaml prompt file loads without errors."""
        data = load_prompt("prompts/architect/v1.yaml")
        assert isinstance(data, PromptData)
        assert "{context}" in data.user_prompt_template

    def test_v1_prompt_has_version(self) -> None:
        """The actual v1.yaml has version field."""
        data = load_prompt("prompts/architect/v1.yaml")
        assert data.version == "v1_free"

    def test_v1_prompt_describes_learning_goals(self) -> None:
        """The actual v1.yaml system prompt mentions learning goals."""
        data = load_prompt("prompts/architect/v1.yaml")
        assert "learning_goal" in data.system_prompt
        assert "expected_knowledge" in data.system_prompt
        assert "expected_skills" in data.system_prompt

    def test_v1_guided_prompt_loads(self) -> None:
        """The actual v1_guided.yaml loads without errors."""
        data = load_prompt("prompts/architect/v1_guided.yaml")
        assert isinstance(data, PromptData)
        assert data.version == "v1_guided"

    def test_v1_guided_has_existing_structure_placeholder(self) -> None:
        """The guided prompt template has {existing_structure} placeholder."""
        data = load_prompt("prompts/architect/v1_guided.yaml")
        assert "{existing_structure}" in data.user_prompt_template
        assert "{context}" in data.user_prompt_template

    def test_v1_guided_system_mentions_preserve(self) -> None:
        """The guided system prompt mentions preserving existing hierarchy."""
        data = load_prompt("prompts/architect/v1_guided.yaml")
        assert "existing" in data.system_prompt.lower()
        assert "preserve" in data.system_prompt.lower()
