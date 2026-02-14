"""Tests for prompt loading and formatting utilities."""

from pathlib import Path

import pytest
import yaml

from course_supporter.agents.prompt_loader import format_user_prompt, load_prompt


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
        """Loads YAML and returns dict with required keys."""
        data = load_prompt(valid_prompt_file)
        assert "system_prompt" in data
        assert "user_prompt_template" in data
        assert data["version"] == "v1"

    def test_load_missing_file(self, tmp_path: Path) -> None:
        """Raises FileNotFoundError for non-existent file."""
        with pytest.raises(FileNotFoundError):
            load_prompt(tmp_path / "nonexistent.yaml")

    def test_load_missing_system_prompt(self, tmp_path: Path) -> None:
        """Raises KeyError when system_prompt is missing."""
        path = tmp_path / "bad.yaml"
        path.write_text(yaml.dump({"user_prompt_template": "test"}))
        with pytest.raises(KeyError, match="system_prompt"):
            load_prompt(path)

    def test_load_missing_user_template(self, tmp_path: Path) -> None:
        """Raises KeyError when user_prompt_template is missing."""
        path = tmp_path / "bad.yaml"
        path.write_text(yaml.dump({"system_prompt": "test"}))
        with pytest.raises(KeyError, match="user_prompt_template"):
            load_prompt(path)

    def test_load_accepts_string_path(self, valid_prompt_file: Path) -> None:
        """Accepts str path in addition to Path objects."""
        data = load_prompt(str(valid_prompt_file))
        assert "system_prompt" in data


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


class TestPromptFileContent:
    def test_v1_prompt_loads_successfully(self) -> None:
        """The actual v1.yaml prompt file loads without errors."""
        data = load_prompt("prompts/architect/v1.yaml")
        assert "system_prompt" in data
        assert "user_prompt_template" in data
        assert "{context}" in data["user_prompt_template"]

    def test_v1_prompt_has_version(self) -> None:
        """The actual v1.yaml has version field."""
        data = load_prompt("prompts/architect/v1.yaml")
        assert data["version"] == "v1"

    def test_v1_prompt_describes_learning_goals(self) -> None:
        """The actual v1.yaml system prompt mentions learning goals."""
        data = load_prompt("prompts/architect/v1.yaml")
        system = data["system_prompt"]
        assert "learning_goal" in system
        assert "expected_knowledge" in system
        assert "expected_skills" in system
