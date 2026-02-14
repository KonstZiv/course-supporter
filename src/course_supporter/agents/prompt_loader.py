"""Prompt template loading and formatting utilities."""

from pathlib import Path
from typing import Any

import yaml


def load_prompt(path: str | Path) -> dict[str, Any]:
    """Load prompt template from YAML file.

    Args:
        path: Path to the YAML prompt file.

    Returns:
        Dict with at least 'system_prompt' and 'user_prompt_template' keys.

    Raises:
        FileNotFoundError: If the prompt file does not exist.
        KeyError: If required keys are missing from YAML.
    """
    prompt_path = Path(path)
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")

    with prompt_path.open() as f:
        data = yaml.safe_load(f)

    required_keys = {"system_prompt", "user_prompt_template"}
    missing = required_keys - set(data.keys())
    if missing:
        raise KeyError(f"Missing required keys in prompt file: {missing}")

    return data  # type: ignore[no-any-return]


def format_user_prompt(template: str, context: str) -> str:
    """Format user prompt template with context.

    Args:
        template: Prompt template with {context} placeholder.
        context: Serialized CourseContext to inject.

    Returns:
        Formatted prompt string.
    """
    return template.format(context=context)
