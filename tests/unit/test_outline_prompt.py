"""Tests for outline prompt loading and formatting."""

from course_supporter.agents.prompt_loader import (
    PromptData,
    format_user_prompt,
    load_prompt,
)


class TestLoadOutlinePrompt:
    def test_loads_v1_outline(self) -> None:
        """load_prompt reads outline/v1.yaml and returns PromptData."""
        data = load_prompt("prompts/outline/v1.yaml")
        assert isinstance(data, PromptData)
        assert data.version == "v1_outline"
        assert data.system_prompt
        assert data.user_prompt_template

    def test_system_prompt_mentions_lossless(self) -> None:
        """System prompt emphasizes lossless restructuring."""
        data = load_prompt("prompts/outline/v1.yaml")
        assert "lossless" in data.system_prompt.lower()

    def test_system_prompt_mentions_summarize(self) -> None:
        """System prompt mentions summarization for navigation layer."""
        data = load_prompt("prompts/outline/v1.yaml")
        assert "summarize" in data.system_prompt.lower()

    def test_user_template_has_context_placeholder(self) -> None:
        """User prompt template contains {context} placeholder."""
        data = load_prompt("prompts/outline/v1.yaml")
        assert "{context}" in data.user_prompt_template


class TestFormatOutlinePrompt:
    def test_fills_context_placeholder(self) -> None:
        """format_user_prompt replaces {context} with provided content."""
        data = load_prompt("prompts/outline/v1.yaml")
        result = format_user_prompt(
            data.user_prompt_template,
            '{"source_type": "video", "chunks": []}',
        )
        assert "{context}" not in result
        assert '"source_type": "video"' in result
