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


def load_split_prompt(
    system_path: str | Path,
    user_path: str | Path,
) -> PromptData:
    """Load prompt from separate system and user YAML files.

    The system file must contain ``system_prompt``.
    The user file must contain ``version`` and ``user_prompt_template``.

    Args:
        system_path: Path to YAML with system_prompt.
        user_path: Path to YAML with user_prompt_template.

    Returns:
        Combined PromptData.

    Raises:
        FileNotFoundError: If either file does not exist.
    """
    sys_path = Path(system_path)
    usr_path = Path(user_path)
    for p in (sys_path, usr_path):
        if not p.exists():
            raise FileNotFoundError(f"Prompt file not found: {p}")

    with sys_path.open() as f:
        sys_data: dict[str, str] = yaml.safe_load(f)

    with usr_path.open() as f:
        usr_data: dict[str, str] = yaml.safe_load(f)

    return PromptData(
        version=usr_data.get("version", "unknown"),
        system_prompt=sys_data["system_prompt"],
        user_prompt_template=usr_data["user_prompt_template"],
    )


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
