"""Tests for prompt loading and formatting utilities."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from course_supporter.agents.prompt_loader import (
    PromptData,
    format_user_prompt,
    load_prompt,
    load_split_prompt,
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

    def test_missing_kwarg_leaves_placeholder(self) -> None:
        """Missing kwarg leaves placeholder unreplaced."""
        template = "Structure:\n{existing_structure}\nMaterials:\n{context}"
        result = format_user_prompt(template, "data")
        assert "{existing_structure}" in result
        assert "data" in result

    def test_context_containing_placeholder_not_cross_substituted(self) -> None:
        """Context with {existing_structure} must not be replaced by kwargs."""
        template = "Structure:\n{existing_structure}\nMaterials:\n{context}"
        malicious_context = "json with {existing_structure} inside"
        result = format_user_prompt(
            template,
            malicious_context,
            existing_structure="Module 1",
        )
        assert "json with {existing_structure} inside" in result
        assert result.startswith("Structure:\nModule 1")


class TestLoadSplitPrompt:
    def test_load_combines_system_and_user(self, tmp_path: Path) -> None:
        """Loads system_prompt from one file and user_prompt_template from another."""
        sys_path = tmp_path / "system.yaml"
        sys_path.write_text(yaml.dump({"system_prompt": "You are an expert."}))
        usr_path = tmp_path / "user.yaml"
        usr_path.write_text(
            yaml.dump(
                {"version": "v2_leaf", "user_prompt_template": "Data:\n{context}"}
            )
        )

        data = load_split_prompt(sys_path, usr_path)
        assert data.system_prompt == "You are an expert."
        assert "{context}" in data.user_prompt_template
        assert data.version == "v2_leaf"

    def test_load_missing_system_file(self, tmp_path: Path) -> None:
        """Raises FileNotFoundError if system file missing."""
        usr_path = tmp_path / "user.yaml"
        usr_path.write_text(yaml.dump({"version": "v2", "user_prompt_template": "t"}))
        with pytest.raises(FileNotFoundError):
            load_split_prompt(tmp_path / "missing.yaml", usr_path)

    def test_load_missing_user_file(self, tmp_path: Path) -> None:
        """Raises FileNotFoundError if user file missing."""
        sys_path = tmp_path / "system.yaml"
        sys_path.write_text(yaml.dump({"system_prompt": "s"}))
        with pytest.raises(FileNotFoundError):
            load_split_prompt(sys_path, tmp_path / "missing.yaml")
