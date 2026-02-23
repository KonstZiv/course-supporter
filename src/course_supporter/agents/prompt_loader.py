"""Prompt template loading and formatting utilities."""

import re
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


_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def format_user_prompt(template: str, context: str, **kwargs: str) -> str:
    """Format user prompt template with context and optional extras.

    Uses a single-pass regex substitution so that values already
    injected (e.g. *context* containing ``{existing_structure}``)
    are never re-scanned for further placeholders.

    Args:
        template: Prompt template with {context} placeholder and
            optional extra placeholders (e.g. {existing_structure}).
        context: Serialized CourseContext to inject.
        **kwargs: Additional template variables (e.g. existing_structure).

    Returns:
        Formatted prompt string.
    """
    replacements: dict[str, str] = {"context": context, **kwargs}

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return replacements.get(key, match.group(0))

    return _PLACEHOLDER_RE.sub(_replace, template)
