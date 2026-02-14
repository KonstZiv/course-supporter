"""Prompt template loading and formatting utilities."""

from pathlib import Path

import yaml
from pydantic import BaseModel


class PromptData(BaseModel):
    """Validated prompt template loaded from YAML.

    Fields:
        version: Prompt version for A/B testing (e.g., "v1").
        system_prompt: System prompt text for the LLM.
        user_prompt_template: User prompt with {context} placeholder.
    """

    version: str = "unknown"
    system_prompt: str
    user_prompt_template: str


def load_prompt(path: str | Path) -> PromptData:
    """Load prompt template from YAML file.

    Args:
        path: Path to the YAML prompt file.

    Returns:
        Validated PromptData with system_prompt and user_prompt_template.

    Raises:
        FileNotFoundError: If the prompt file does not exist.
        ValidationError: If required keys are missing or invalid.
    """
    prompt_path = Path(path)
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

    with prompt_path.open() as f:
        data = yaml.safe_load(f)

    return PromptData.model_validate(data)


def format_user_prompt(template: str, context: str) -> str:
    """Format user prompt template with context.

    Args:
        template: Prompt template with {context} placeholder.
        context: Serialized CourseContext to inject.

    Returns:
        Formatted prompt string.
    """
    return template.format(context=context)
